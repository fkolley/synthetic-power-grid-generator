#!/usr/bin/env python3
"""
Synthetic Power Grid Generator - CIM XML Output

Generates a synthetic two-level transmission grid with German / European (ENTSO-E)
default parameters and exports CIM RDF/XML (CIM17 / CIM100 namespace) using
cim-graph. The file is verified by reading it back and solving an AC power flow
on the read-back model.

Usage:
    python synthetic_cim_generator.py --buses 40 --gens 8 --loads 30 --seed 42 --output grid.cim.xml
    python synthetic_cim_generator.py --buses 12 --backbone-buses 0      # single 110 kV level

Model contents:
    GeographicalRegion -> SubGeographicalRegion -> Substation (one per 110 kV site)
        -> VoltageLevel (110 kV, plus 380 kV at backbone sites)
            -> ConnectivityNode, SynchronousMachine, EnergyConsumer, LinearShuntCompensator
        -> PowerTransformer (380/110 kV) -> PowerTransformerEnd x2
    SubGeographicalRegion -> Line -> ACLineSegment
    SynchronousMachine -> GeneratingUnit (P limits), RegulatingControl (voltage setpoint)
    Terminal -> OperationalLimitSet -> CurrentLimit (PATL thermal rating)

Network design:
    * 110 kV regional grid: random substation sites, minimum spanning tree plus
      nearest-neighbour corridors; line length follows distance.
    * 380 kV backbone: a subset of sites spread over the area, meshed the same way,
      each coupled to the 110 kV grid by 300 MVA 380/110 kV transformers (at least
      two per site for N-1, more if the site's supply area needs it).
    * Large units connect to 380 kV, smaller units to 110 kV; loads are at 110 kV.
      Dispatch equals load plus a loss allowance, shared in proportion to capacity.
    * Per-bus shunt compensation: capacitors for load Q, reactors for line charging.

German / European defaults:
    * 50 Hz; 380 kV and 110 kV nominal voltages.
    * Overhead lines with Al/St 265/35 conductors (DIN EN 50182 264-AL1/34-ST1A):
      single conductor at 110 kV, 2-bundle at 220 kV, 4-bundle at 380 kV.
    * Operating voltage ranges from ENTSO-E SO GL (Reg. 2017/1485, Annex II, Continental
      Europe): 110-300 kV 0.90-1.118 pu; 300-400 kV 360-420 kV.

Conventions:
    * Units: the CIM17 profile uses unprefixed SI units (V, W, VAr, VA, A, ohm, S, m).
      All values are written as typed quantities, e.g.
      <cim:BaseVoltage.nominalVoltage rdf:datatype="...#UnitSymbol.volt">380000.0</...>
    * Sign: RotatingMachine.p/q and EnergyConsumer.p/q use the load sign convention
      (positive = power flowing out of the network), so generator output is negative.
      GeneratingUnit P limits and SynchronousMachine Q limits use generator convention.
    * Transformer series impedance is referred to the high-voltage end (end 1).
    * Slack: the largest machine (on the backbone if there is one) has
      referencePriority=1, all others 0.
    * Reproducibility: the same arguments give the same model and mRIDs. Only the
      md:FullModel header (export UUID and timestamp) differs between exports.
"""

import argparse
import math
import os
import random
import sys
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

# The profile must be selected before cim-graph is imported.
CIM_PROFILE = "cimprofiles.cim17.cim17v40"
os.environ["CIMG_CIM_PROFILE"] = CIM_PROFILE
os.environ["CIMG_USE_UNITS"] = "true"

import networkx as nx
import numpy as np
import cimprofiles.cim17.cim17v40 as cim
from cimprofiles.units import CIMUnit
from cimgraph import utils
from cimgraph.databases import XMLFile
from cimgraph.models import GraphModel, NodeBreakerModel


# ---------------------------------------------------------------------------
# German / European default parameters
# ---------------------------------------------------------------------------

FREQUENCY_HZ = 50.0
DEFAULT_VOLTAGE_KV = 110.0
DEFAULT_BACKBONE_KV = 380.0


@dataclass(frozen=True)
class LineType:
    name: str
    r_ohm_per_km: float
    x_ohm_per_km: float
    c_nf_per_km: float
    i_th_a: float          # continuous thermal rating (PATL)


# Typical overhead-line data for Al/St 265/35 conductors (Ith 680 A per conductor).
LINE_TYPES = {
    110.0: LineType("1x Al/St 265/35", 0.109, 0.39, 9.5, 680.0),
    220.0: LineType("2x Al/St 265/35", 0.055, 0.31, 11.5, 1360.0),
    380.0: LineType("4x Al/St 265/35", 0.027, 0.26, 13.8, 2720.0),
}
LINE_PARAM_SPREAD = 0.10       # +-10 % per line for geometry / conductor temperature


@dataclass(frozen=True)
class TransformerType:
    rated_s_mva: float
    uk_percent: float      # short-circuit voltage
    ur_percent: float      # resistive part (copper losses / rated power)


TRANSFORMER_TYPES = {
    (380.0, 110.0): TransformerType(300.0, 12.0, 0.30),
    (380.0, 220.0): TransformerType(600.0, 12.0, 0.25),
    (220.0, 110.0): TransformerType(200.0, 12.0, 0.35),
}
MIN_TRANSFORMERS_PER_SITE = 2  # N-1
TRANSFORMER_MARGIN = 1.4       # installed MVA / supply-area net demand


def line_type(voltage_kv: float) -> LineType:
    return LINE_TYPES[min(LINE_TYPES, key=lambda kv: abs(kv - voltage_kv))]


def transformer_type(hv_kv: float, lv_kv: float) -> TransformerType:
    return TRANSFORMER_TYPES[min(TRANSFORMER_TYPES,
                                 key=lambda k: abs(k[0] - hv_kv) + abs(k[1] - lv_kv))]


def voltage_limits_pu(voltage_kv: float) -> Tuple[float, float]:
    """ENTSO-E SO GL operating ranges (Continental Europe)."""
    if voltage_kv > 300:
        return 360.0 / voltage_kv, 420.0 / voltage_kv
    return 0.90, 1.118


# Network sizing
SPACING_KM_110 = 12.0          # distance between neighbouring 110 kV substations
AVG_DEGREE = 2.8               # 2 x lines / buses, 110 kV grid
BACKBONE_AVG_DEGREE = 2.8      # 2 x lines / buses, 380 kV grid
BUSES_PER_BACKBONE_SITE = 8    # default --backbone-buses = buses / this
ROUTING_FACTOR = 1.15          # line length / straight-line distance

# Loads and generation
LOAD_MW_RANGE = (5.0, 45.0)    # per 110 kV substation (2-3 x 40 MVA 110/20 kV transformers)
LOAD_Q_RATIO = (0.1, 0.4)      # Q / P
LOSS_ALLOWANCE = 0.03          # dispatch = total load * (1 + LOSS_ALLOWANCE); the slack covers the rest
FLEET_LOADING = (0.5, 0.8)     # allowed total dispatch / total max P of the generator fleet
BACKBONE_GEN_SHARE = 0.5       # share of --gens connected to the backbone
UNIT_MVA = {"backbone": (400.0, 1100.0), "regional": (20.0, 150.0)}  # relative sizes before scaling
RATED_POWER_FACTOR = 0.85
MIN_P_FRACTION = 0.2           # GeneratingUnit.minOperatingP as a fraction of maxOperatingP
GEN_VM_PU = {"backbone": (1.02, 1.05), "regional": (1.01, 1.04)}

# Local reactive compensation per bus (LinearShuntCompensator), as in real grids
CAP_COMPENSATION = 0.8         # capacitor covers this share of the bus's load Q
REACTOR_COMPENSATION = 0.75    # reactor absorbs this share of the line charging at the bus
MIN_SHUNT_MVAR = 1.0           # smaller net compensation is not modelled


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------

class IdFactory:
    """Deterministic UUIDv4 mRIDs from a seed, independent of the electrical RNG."""

    def __init__(self, seed: int):
        self._rng = random.Random(f"mrid-{seed}")

    def __call__(self) -> str:
        return str(uuid.UUID(int=self._rng.getrandbits(128), version=4))


def default_backbone_buses(n_buses: int) -> int:
    n = round(n_buses / BUSES_PER_BACKBONE_SITE)
    return n if n >= 2 else 0


def random_sites(n: int, spacing_km: float, rng: random.Random) -> List[Tuple[float, float]]:
    side = spacing_km * math.sqrt(n) * 2.0
    return [(rng.uniform(0, side), rng.uniform(0, side)) for _ in range(n)]


def build_corridors(pos: Sequence[Tuple[float, float]], avg_degree: float) -> nx.Graph:
    """Minimum spanning tree for connectivity, then the shortest remaining
    nearest-neighbour corridors until the target average degree is reached (real
    grids connect mostly to nearby buses). Edge attribute 'length_km' is the routed
    line length."""
    n = len(pos)

    def dist(a, b):
        return math.dist(pos[a], pos[b])

    G = nx.Graph()
    G.add_nodes_from(range(n))
    if n < 2:
        return G
    complete = nx.Graph()
    complete.add_weighted_edges_from((a, b, dist(a, b)) for a in range(n) for b in range(a + 1, n))
    G.add_edges_from(nx.minimum_spanning_tree(complete).edges())

    k = 4  # candidate corridors: each bus to its k nearest neighbours
    candidates = set()
    for a in range(n):
        for b in sorted((b for b in range(n) if b != a), key=lambda b: dist(a, b))[:k]:
            candidates.add((min(a, b), max(a, b)))
    existing = {tuple(sorted(e)) for e in G.edges()}
    target_edges = max(n - 1, round(avg_degree * n / 2))
    for a, b in sorted(candidates - existing, key=lambda e: dist(*e)):
        if G.number_of_edges() >= target_edges:
            break
        G.add_edge(a, b)

    for a, b in G.edges():
        G.edges[a, b]["length_km"] = max(2.0, dist(a, b) * ROUTING_FACTOR)
    return G


def spread_sites(pos: Sequence[Tuple[float, float]], n: int, rng: random.Random) -> List[int]:
    """Pick n sites spread over the area (randomised farthest-point sampling)."""
    chosen = [rng.randrange(len(pos))]
    while len(chosen) < n:
        remaining = [i for i in range(len(pos)) if i not in chosen]
        far = {i: min(math.dist(pos[i], pos[c]) for c in chosen) for i in remaining}
        best = max(far.values())
        chosen.append(rng.choice(sorted(i for i in remaining if far[i] >= 0.8 * best)))
    return chosen


def place_generators(G: nx.Graph, n_gens: int, rng: random.Random) -> List[int]:
    """Spread generators over a network: randomised farthest-point sampling on hop
    distance, so every bus has a generator within a few lines."""
    if n_gens == 0:
        return []
    hops = dict(nx.all_pairs_shortest_path_length(G))
    chosen = [rng.choice(sorted(G.nodes))]
    while len(chosen) < n_gens:
        remaining = [b for b in sorted(G.nodes) if b not in chosen]
        far = {b: min(hops[b][c] for c in chosen) for b in remaining}
        best = max(far.values())
        chosen.append(rng.choice([b for b in remaining if far[b] >= best - 1]))
    return chosen


def line_params(length_km: float, voltage_kv: float, rng: random.Random) -> Tuple[float, float, float]:
    """Total (r [ohm], x [ohm], bch [S]) for a line of the level's standard type."""
    t = line_type(voltage_kv)

    def spread():
        return rng.uniform(1 - LINE_PARAM_SPREAD, 1 + LINE_PARAM_SPREAD)

    r = t.r_ohm_per_km * spread() * length_km
    x = t.x_ohm_per_km * spread() * length_km
    bch = 2 * math.pi * FREQUENCY_HZ * t.c_nf_per_km * 1e-9 * spread() * length_km
    return round(r, 4), round(x, 4), round(bch, 9)


def validate_sizes(n_buses: int, n_gens: int, n_loads: int, n_backbone: int,
                   voltage_kv: float, backbone_kv: float) -> None:
    if n_buses < 2:
        raise ValueError("need at least 2 buses")
    if not 0 <= n_backbone <= n_buses:
        raise ValueError(f"--backbone-buses must be between 0 and the number of buses ({n_buses})")
    if n_backbone and backbone_kv <= voltage_kv:
        raise ValueError("--backbone-voltage must be higher than --voltage")
    if not 1 <= n_gens <= n_buses + n_backbone:
        raise ValueError(f"--gens must be between 1 and the number of buses ({n_buses + n_backbone})")
    if not 1 <= n_loads <= n_buses:
        raise ValueError(f"--loads must be between 1 and the number of buses ({n_buses})")


# ---------------------------------------------------------------------------
# Network planning (pure data, no CIM objects yet)
# ---------------------------------------------------------------------------

@dataclass
class Branch:
    """A line corridor or a transformer site; 'circuits' parallel identical units."""
    kind: str                  # "line" or "trafo"
    u: int                     # from bus (HV bus for transformers)
    v: int                     # to bus
    x_pu: float                # per-circuit series reactance on S_BASE_MVA
    rating_mva: float          # per-circuit thermal rating
    max_circuits: int
    circuits: int = 1
    length_km: float = 0.0
    r_ohm: float = 0.0         # per circuit (lines)
    x_ohm: float = 0.0
    bch_s: float = 0.0


@dataclass
class Gen:
    bus: int
    tier: str                  # "backbone" or "regional"
    rated_s: float
    max_p: float
    p: float                   # dispatch, generator convention
    q0: float                  # initial Q guess, generator convention
    vm_pu: float
    slack: bool = False


S_BASE_MVA = 100.0
DC_LOADING_TARGET = 0.7        # reinforce until DC flows are below this share of the rating
MAX_LINE_CIRCUITS = 4
MAX_TRANSFORMERS_PER_SITE = 8


def dc_flows(n_bus: int, branches: List[Branch], injection_mw: Sequence[float], slack_bus: int) -> List[float]:
    """DC power flow. Returns the total MW flow on each branch (all circuits)."""
    B = np.zeros((n_bus, n_bus))
    for br in branches:
        y = br.circuits / br.x_pu
        B[br.u, br.u] += y
        B[br.v, br.v] += y
        B[br.u, br.v] -= y
        B[br.v, br.u] -= y
    keep = [b for b in range(n_bus) if b != slack_bus]
    theta = np.zeros(n_bus)
    p = np.asarray(injection_mw, dtype=float) / S_BASE_MVA
    theta[keep] = np.linalg.solve(B[np.ix_(keep, keep)], p[keep])
    return [(theta[br.u] - theta[br.v]) * br.circuits / br.x_pu * S_BASE_MVA for br in branches]


def reinforce(n_bus: int, branches: List[Branch], injection_mw: Sequence[float], slack_bus: int) -> int:
    """Add parallel circuits / transformers until no branch exceeds DC_LOADING_TARGET.
    Returns the number of units added."""
    added = 0
    for _ in range(50):
        flows = dc_flows(n_bus, branches, injection_mw, slack_bus)
        changed = False
        for br, f in zip(branches, flows):
            if abs(f) > DC_LOADING_TARGET * br.rating_mva * br.circuits and br.circuits < br.max_circuits:
                br.circuits += 1
                added += 1
                changed = True
        if not changed:
            break
    return added


def create_synthetic_cim(
    n_buses: int = 40,
    n_gens: int = 8,
    n_loads: int = 30,
    voltage_kv: float = DEFAULT_VOLTAGE_KV,
    backbone_kv: float = DEFAULT_BACKBONE_KV,
    n_backbone: Optional[int] = None,
    seed: int = 42,
) -> GraphModel:
    """Build a GraphModel of a synthetic grid.

    n_buses is the number of lower-level (110 kV) buses, one per substation.
    n_backbone of those substations also get a backbone (380 kV) bus; None picks
    a default from n_buses, 0 builds a single-level grid.
    """
    if n_backbone is None:
        n_backbone = default_backbone_buses(n_buses)
    validate_sizes(n_buses, n_gens, n_loads, n_backbone, voltage_kv, backbone_kv)
    rng = random.Random(seed)

    # --- Geography: 110 kV sites, backbone sites, corridors ----------------------
    pos = random_sites(n_buses, SPACING_KM_110 * voltage_kv / 110.0, rng)
    G_lv = build_corridors(pos, AVG_DEGREE)
    bb_sites = spread_sites(pos, n_backbone, rng) if n_backbone else []
    G_bb = nx.relabel_nodes(build_corridors([pos[s] for s in bb_sites], BACKBONE_AVG_DEGREE),
                            {j: n_buses + j for j in range(n_backbone)})
    # Global bus index: 0..n_buses-1 lower level, n_buses.. backbone
    bus_kv = [voltage_kv] * n_buses + [backbone_kv] * n_backbone
    bus_site = list(range(n_buses)) + bb_sites
    n_total = n_buses + n_backbone

    # --- Lines ---------------------------------------------------------------------
    branches: List[Branch] = []
    for G in (G_lv, G_bb):
        for u, v in sorted(tuple(sorted(e)) for e in G.edges()):
            kv = bus_kv[u]
            length_km = G.edges[u, v]["length_km"]
            r, x, bch = line_params(length_km, kv, rng)
            branches.append(Branch(
                "line", u, v, x_pu=x / (kv**2 / S_BASE_MVA),
                rating_mva=math.sqrt(3) * kv * line_type(kv).i_th_a / 1000,
                max_circuits=MAX_LINE_CIRCUITS, length_km=length_km, r_ohm=r, x_ohm=x, bch_s=bch))

    # --- Bus assignment ------------------------------------------------------------
    n_bb_gens = 0
    if n_backbone:
        n_bb_gens = min(n_backbone, max(1, round(n_gens * BACKBONE_GEN_SHARE)))
    n_lv_gens = n_gens - n_bb_gens
    if n_lv_gens > n_buses:
        n_bb_gens, n_lv_gens = n_gens - n_buses, n_buses
    gen_buses = place_generators(G_bb, n_bb_gens, rng) + place_generators(G_lv, n_lv_gens, rng)
    # Loads at 110 kV; prefer buses without a generator, fall back to generator buses.
    lv_gen_buses = [b for b in gen_buses if b < n_buses]
    free = [i for i in range(n_buses) if i not in lv_gen_buses]
    rng.shuffle(free)
    load_buses = (free + rng.sample(lv_gen_buses, len(lv_gen_buses)))[:n_loads]

    # --- Loads ---------------------------------------------------------------------
    load_scale = (voltage_kv / 110.0) ** 1.5
    loads: List[Tuple[int, float, float]] = []
    for bus_i in load_buses:
        p_mw = round(rng.uniform(*LOAD_MW_RANGE) * load_scale, 2)
        loads.append((bus_i, p_mw, round(p_mw * rng.uniform(*LOAD_Q_RATIO), 2)))
    total_load_p = sum(p for _, p, _ in loads)
    total_load_q = sum(q for _, _, q in loads)

    # --- Generators: random relative sizes, scaled so fleet loading is realistic ---
    target_gen_p = total_load_p * (1 + LOSS_ALLOWANCE)
    tiers = ["backbone" if b >= n_buses else "regional" for b in gen_buses]
    rated_s = [rng.uniform(*UNIT_MVA[t]) for t in tiers]
    loading = target_gen_p / (sum(rated_s) * RATED_POWER_FACTOR)
    clamped = min(max(loading, FLEET_LOADING[0]), FLEET_LOADING[1])
    rated_s = [round(s * loading / clamped, 2) for s in rated_s]
    fleet_max_p = sum(s * RATED_POWER_FACTOR for s in rated_s)
    candidates = [k for k, t in enumerate(tiers) if t == "backbone"] or list(range(n_gens))
    slack_idx = max(candidates, key=lambda k: rated_s[k])
    gens: List[Gen] = []
    for idx, (b, tier, s) in enumerate(zip(gen_buses, tiers, rated_s)):
        share = s * RATED_POWER_FACTOR / fleet_max_p
        lo, hi = GEN_VM_PU[tier]
        vm = round((lo + hi) / 2, 3) if idx == slack_idx else round(rng.uniform(lo, hi), 3)
        gens.append(Gen(b, tier, s, s * RATED_POWER_FACTOR, round(target_gen_p * share, 2),
                        round(total_load_q * share, 2), vm, slack=idx == slack_idx))

    # --- Transformers: initial count from the net demand of each site's supply area
    injection = [0.0] * n_total
    for g in gens:
        injection[g.bus] += g.p
    for b, p, _ in loads:
        injection[b] -= p
    if n_backbone:
        t = transformer_type(backbone_kv, voltage_kv)
        net_demand = [0.0] * n_backbone
        for i in range(n_buses):
            j = min(range(n_backbone), key=lambda j: math.dist(pos[i], pos[bb_sites[j]]))
            net_demand[j] -= injection[i]
        x_pu = t.uk_percent / 100 * S_BASE_MVA / t.rated_s_mva
        for j, site in enumerate(bb_sites):
            count = max(MIN_TRANSFORMERS_PER_SITE,
                        math.ceil(abs(net_demand[j]) * TRANSFORMER_MARGIN / t.rated_s_mva))
            branches.append(Branch("trafo", n_buses + j, site, x_pu=x_pu, rating_mva=t.rated_s_mva,
                                   max_circuits=MAX_TRANSFORMERS_PER_SITE, circuits=count))

    # --- Reinforcement: parallel circuits / transformers where DC flows exceed ratings
    slack_bus = gens[slack_idx].bus
    reinforce(n_total, branches, injection, slack_bus)

    return emit_cim(seed, voltage_kv, backbone_kv, n_buses, bus_kv, bus_site, branches, loads, gens)


# ---------------------------------------------------------------------------
# CIM emission
# ---------------------------------------------------------------------------

def emit_cim(seed: int, voltage_kv: float, backbone_kv: float, n_buses: int,
             bus_kv: List[float], bus_site: List[int], branches: List[Branch],
             loads: List[Tuple[int, float, float]], gens: List[Gen]) -> GraphModel:
    new_id = IdFactory(seed)
    graph: Dict = {}
    n_total = len(bus_kv)

    def create(cls, name: str):
        obj = utils.create_object(cls, new_id(), graph)
        obj.name = name
        return obj

    # --- Containers, base voltages, buses -----------------------------------------
    region = create(cim.GeographicalRegion, "SyntheticRegion")
    subregion = create(cim.SubGeographicalRegion, "SyntheticSubRegion")
    subregion.Region = region

    base_voltages = {}
    for kv in sorted(set(bus_kv)):
        bv = create(cim.BaseVoltage, f"{kv:g}kV")
        bv.nominalVoltage = cim.Voltage(kv, "kV")
        base_voltages[kv] = bv

    substations = []
    for i in range(n_buses):
        sub = create(cim.Substation, f"Sub_{i:03d}")
        sub.Region = subregion
        substations.append(sub)

    voltage_levels: List = []
    cns: List = []
    for b in range(n_total):
        kv, site = bus_kv[b], bus_site[b]
        lo, hi = voltage_limits_pu(kv)
        vl = create(cim.VoltageLevel, f"VL_{site:03d}_{kv:g}kV")
        vl.Substation = substations[site]
        vl.BaseVoltage = base_voltages[kv]
        vl.lowVoltageLimit = cim.Voltage(round(lo * kv, 3), "kV")
        vl.highVoltageLimit = cim.Voltage(round(hi * kv, 3), "kV")
        cn = create(cim.ConnectivityNode, f"Bus_{kv:g}kV_{site:03d}")
        cn.ConnectivityNodeContainer = vl
        voltage_levels.append(vl)
        cns.append(cn)

    def connect(equipment, bus_idx: int, seq: int = 1):
        term = create(cim.Terminal, f"{equipment.name}_T{seq}")
        term.sequenceNumber = seq
        term.connected = True
        term.ConnectivityNode = cns[bus_idx]
        term.ConductingEquipment = equipment
        return term

    patl = create(cim.OperationalLimitType, "PATL")
    patl.isInfiniteDuration = True
    patl.direction = cim.OperationalLimitDirectionKind.absoluteValue

    def current_limit(term, amps: float):
        ols = create(cim.OperationalLimitSet, f"{term.name}_Limits")
        ols.Terminal = term
        lim = create(cim.CurrentLimit, f"{term.name}_PATL")
        lim.OperationalLimitSet = ols
        lim.OperationalLimitType = patl
        lim.value = cim.CurrentFlow(round(amps, 1), "A")
        lim.normalValue = cim.CurrentFlow(round(amps, 1), "A")

    # --- Lines: one Line container per corridor, one ACLineSegment per circuit -----
    bus_charging_mvar = [0.0] * n_total   # half of each circuit's charging at each end
    for br in (b for b in branches if b.kind == "line"):
        kv = bus_kv[br.u]
        lt = line_type(kv)
        name = f"Line_{kv:g}kV_{bus_site[br.u]:03d}_{bus_site[br.v]:03d}"
        line_container = create(cim.Line, name)
        line_container.Region = subregion
        for c in range(1, br.circuits + 1):
            seg = create(cim.ACLineSegment, f"{name}_C{c}")
            seg.description = f"{lt.name}, circuit {c} of {br.circuits}"
            seg.EquipmentContainer = line_container
            seg.BaseVoltage = base_voltages[kv]
            seg.length = cim.Length(round(br.length_km, 2), "km")
            seg.r = cim.Resistance(br.r_ohm, "ohm")
            seg.x = cim.Reactance(br.x_ohm, "ohm")
            seg.bch = cim.Susceptance(br.bch_s, "S")
            for seq, b in ((1, br.u), (2, br.v)):
                current_limit(connect(seg, b, seq), lt.i_th_a)
                bus_charging_mvar[b] += br.bch_s * kv**2 / 2

    # --- Loads (EnergyConsumer, load sign convention) ---------------------------
    bus_load_q = [0.0] * n_total
    for idx, (bus_i, p_mw, q_mvar) in enumerate(loads):
        bus_load_q[bus_i] += q_mvar
        load = create(cim.EnergyConsumer, f"Load_{idx:02d}")
        load.EquipmentContainer = voltage_levels[bus_i]
        load.BaseVoltage = base_voltages[bus_kv[bus_i]]
        load.p = cim.ActivePower(p_mw, "MW")
        load.q = cim.ReactivePower(q_mvar, "MVAr")
        connect(load, bus_i)

    # --- Shunt compensation: capacitors at load buses, reactors against line charging
    for bus_i in range(n_total):
        q_mvar = CAP_COMPENSATION * bus_load_q[bus_i] - REACTOR_COMPENSATION * bus_charging_mvar[bus_i]
        if abs(q_mvar) < MIN_SHUNT_MVAR:
            continue
        kv = bus_kv[bus_i]
        kind = "Cap" if q_mvar > 0 else "Reactor"
        shunt = create(cim.LinearShuntCompensator, f"{kind}_{kv:g}kV_{bus_site[bus_i]:03d}")
        shunt.EquipmentContainer = voltage_levels[bus_i]
        shunt.BaseVoltage = base_voltages[kv]
        shunt.nomU = cim.Voltage(kv, "kV")
        shunt.bPerSection = cim.Susceptance(round(q_mvar / kv**2, 9), "S")  # b > 0: capacitive
        shunt.gPerSection = cim.Conductance(0.0, "S")
        shunt.maximumSections = 1
        shunt.normalSections = 1
        shunt.sections = 1.0
        shunt.grounded = True
        connect(shunt, bus_i)

    # --- Generators (SynchronousMachine + GeneratingUnit + RegulatingControl) --
    for idx, g in enumerate(gens):
        kv = bus_kv[g.bus]
        name = f"Gen_{kv:g}kV_{idx:02d}"
        vl = voltage_levels[g.bus]

        unit = create(cim.GeneratingUnit, f"{name}_GU")
        unit.EquipmentContainer = vl
        unit.maxOperatingP = cim.ActivePower(round(g.max_p, 2), "MW")
        unit.minOperatingP = cim.ActivePower(round(MIN_P_FRACTION * g.max_p, 2), "MW")
        unit.nominalP = cim.ActivePower(round(g.max_p, 2), "MW")
        unit.initialP = cim.ActivePower(g.p, "MW")

        gen = create(cim.SynchronousMachine, name)
        gen.EquipmentContainer = vl
        gen.BaseVoltage = base_voltages[kv]
        gen.GeneratingUnit = unit
        gen.type = cim.SynchronousMachineKind.generator
        gen.operatingMode = cim.SynchronousMachineOperatingMode.generator
        gen.referencePriority = 1 if g.slack else 0
        gen.ratedS = cim.ApparentPower(g.rated_s, "MVA")
        gen.ratedU = cim.Voltage(kv, "kV")
        gen.ratedPowerFactor = RATED_POWER_FACTOR
        q_cap = g.rated_s * math.sqrt(1 - RATED_POWER_FACTOR**2)
        gen.maxQ = cim.ReactivePower(round(q_cap, 2), "MVAr")
        gen.minQ = cim.ReactivePower(round(-0.5 * q_cap, 2), "MVAr")
        gen.p = cim.ActivePower(-g.p, "MW")      # load sign convention
        gen.q = cim.ReactivePower(-g.q0, "MVAr")
        term = connect(gen, g.bus)

        rc = create(cim.RegulatingControl, f"{name}_RC")
        rc.mode = cim.RegulatingControlModeKind.voltage
        rc.Terminal = term
        rc.enabled = True
        rc.targetValue = round(g.vm_pu * kv, 3)
        rc.targetValueUnitMultiplier = cim.UnitMultiplier.k
        rc.targetDeadband = 0.5
        gen.RegulatingControl = rc
        gen.controlEnabled = True

    # --- Transformers ----------------------------------------------------------------
    if any(b.kind == "trafo" for b in branches):
        t = transformer_type(backbone_kv, voltage_kv)
        z_base = backbone_kv**2 / t.rated_s_mva
        r_ohm = t.ur_percent / 100 * z_base
        x_ohm = math.sqrt((t.uk_percent / 100 * z_base) ** 2 - r_ohm**2)
    for br in (b for b in branches if b.kind == "trafo"):
        site = bus_site[br.v]
        for k in range(br.circuits):
            pt = create(cim.PowerTransformer, f"TR_{site:03d}_{k + 1}")
            pt.EquipmentContainer = substations[site]
            pt.vectorGroup = "YNyn0"
            for end_no, bus_i in ((1, br.u), (2, br.v)):
                kv = bus_kv[bus_i]
                end = create(cim.PowerTransformerEnd, f"{pt.name}_End{end_no}")
                end.PowerTransformer = pt
                end.endNumber = end_no
                end.BaseVoltage = base_voltages[kv]
                end.ratedS = cim.ApparentPower(t.rated_s_mva, "MVA")
                end.ratedU = cim.Voltage(kv, "kV")
                end.r = cim.Resistance(round(r_ohm if end_no == 1 else 0.0, 4), "ohm")
                end.x = cim.Reactance(round(x_ohm if end_no == 1 else 0.0, 4), "ohm")
                end.g = cim.Conductance(0.0, "S")
                end.b = cim.Susceptance(0.0, "S")
                end.connectionKind = cim.WindingConnection.Yn
                end.phaseAngleClock = 0
                term = connect(pt, bus_i, end_no)
                end.Terminal = term
                current_limit(term, t.rated_s_mva / (math.sqrt(3) * kv) * 1000)

    return GraphModel(container=None, connection=None, graph=graph)

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _objs(model: GraphModel, cls) -> list:
    return list(model.graph.get(cls, {}).values())


def _terminals_by_equipment(model: GraphModel) -> Dict:
    out: Dict = {}
    for t in _objs(model, cim.Terminal):
        if t.ConductingEquipment is not None:
            out.setdefault(t.ConductingEquipment.identifier, []).append(t)
    for ts in out.values():
        ts.sort(key=lambda t: t.sequenceNumber or 0)
    return out


def _current_limits(model: GraphModel) -> Dict:
    """Terminal identifier -> PATL current limit [A]."""
    limits = {}
    for lim in _objs(model, cim.CurrentLimit):
        ols = lim.OperationalLimitSet
        if ols is not None and ols.Terminal is not None and lim.value is not None:
            limits[ols.Terminal.identifier] = lim.value.to("A")
    return limits


def _transformer_ends(model: GraphModel) -> Dict:
    ends: Dict = {}
    for e in _objs(model, cim.PowerTransformerEnd):
        if e.PowerTransformer is not None:
            ends.setdefault(e.PowerTransformer.identifier, []).append(e)
    for es in ends.values():
        es.sort(key=lambda e: e.endNumber or 0)
    return ends


def check_model(model: GraphModel) -> List[str]:
    """Structural and electrical sanity checks. Returns a list of problems."""
    problems: List[str] = []
    terms_by_eq = _terminals_by_equipment(model)
    limits = _current_limits(model)
    G = nx.Graph()
    for cn in _objs(model, cim.ConnectivityNode):
        G.add_node(cn.identifier)
        if cn.ConnectivityNodeContainer is None:
            problems.append(f"{cn.name}: no ConnectivityNodeContainer")
    for t in _objs(model, cim.Terminal):
        if t.ConnectivityNode is None or t.ConductingEquipment is None:
            problems.append(f"{t.name}: dangling terminal")

    for seg in _objs(model, cim.ACLineSegment):
        ts = terms_by_eq.get(seg.identifier, [])
        if len(ts) != 2:
            problems.append(f"{seg.name}: {len(ts)} terminals, expected 2")
            continue
        G.add_edge(ts[0].ConnectivityNode.identifier, ts[1].ConnectivityNode.identifier)
        for attr in ("r", "x", "bch", "length"):
            if not isinstance(getattr(seg, attr), CIMUnit):
                problems.append(f"{seg.name}.{attr}: missing or untyped")
        if not all(t.identifier in limits for t in ts):
            problems.append(f"{seg.name}: missing current limit")

    ends_by_pt = _transformer_ends(model)
    for pt in _objs(model, cim.PowerTransformer):
        ends = ends_by_pt.get(pt.identifier, [])
        if len(ends) != 2 or any(e.Terminal is None for e in ends):
            problems.append(f"{pt.name}: expected 2 ends with terminals")
            continue
        vls = [e.Terminal.ConnectivityNode.ConnectivityNodeContainer for e in ends]
        if vls[0] is vls[1] or vls[0].Substation is not vls[1].Substation:
            problems.append(f"{pt.name}: ends must be in different voltage levels of one substation")
        if pt.EquipmentContainer is None:
            problems.append(f"{pt.name}: no EquipmentContainer")
        G.add_edge(*(e.Terminal.ConnectivityNode.identifier for e in ends))

    for cls in (cim.SynchronousMachine, cim.EnergyConsumer, cim.LinearShuntCompensator, cim.ACLineSegment):
        for eq in _objs(model, cls):
            if eq.EquipmentContainer is None:
                problems.append(f"{eq.name}: no EquipmentContainer")
            if cls is not cim.ACLineSegment and len(terms_by_eq.get(eq.identifier, [])) != 1:
                problems.append(f"{eq.name}: expected 1 terminal")

    if G.number_of_nodes() and not nx.is_connected(G):
        problems.append("network is not connected")

    gens = _objs(model, cim.SynchronousMachine)
    slacks = [g for g in gens if (g.referencePriority or 0) > 0]
    if len(slacks) != 1:
        problems.append(f"{len(slacks)} slack machines (referencePriority > 0), expected 1")
    for g in gens:
        if not isinstance(g.type, cim.SynchronousMachineKind):
            problems.append(f"{g.name}.type is not a SynchronousMachineKind enum")
        if g.GeneratingUnit is None or g.RegulatingControl is None:
            problems.append(f"{g.name}: missing GeneratingUnit or RegulatingControl")
            continue
        p_out = -g.p.to("MW")
        if not g.GeneratingUnit.minOperatingP.to("MW") <= p_out <= g.GeneratingUnit.maxOperatingP.to("MW"):
            problems.append(f"{g.name}: dispatch {p_out:.1f} MW outside unit limits")
    return problems


def run_power_flow(model: GraphModel) -> Tuple[bool, List[str]]:
    """Convert the read-back CIM model to pandapower and solve an AC power flow.
    Checks bus voltages against each VoltageLevel's limits and branch loading
    against the PATL current limits / transformer ratings."""
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import pandapower as pp
    except ImportError:
        return True, ["skipped (pandapower not installed)"]

    net = pp.create_empty_network(f_hz=FREQUENCY_HZ)
    bus_of: Dict = {}
    vlim: Dict = {}
    for cn in _objs(model, cim.ConnectivityNode):
        vl = cn.ConnectivityNodeContainer
        vn_kv = vl.BaseVoltage.nominalVoltage.to("kV")
        b = pp.create_bus(net, vn_kv=vn_kv, name=cn.name)
        bus_of[cn.identifier] = b
        vlim[b] = (vl.lowVoltageLimit.to("kV") / vn_kv, vl.highVoltageLimit.to("kV") / vn_kv)

    terms_by_eq = _terminals_by_equipment(model)
    limits = _current_limits(model)

    def bus(eq, i=0):
        return bus_of[terms_by_eq[eq.identifier][i].ConnectivityNode.identifier]

    for seg in _objs(model, cim.ACLineSegment):
        km = seg.length.to("km")
        i_max = min(limits.get(t.identifier, math.inf) for t in terms_by_eq[seg.identifier])
        pp.create_line_from_parameters(
            net, bus(seg, 0), bus(seg, 1), length_km=km, name=seg.name,
            r_ohm_per_km=float(seg.r) / km, x_ohm_per_km=float(seg.x) / km,
            c_nf_per_km=float(seg.bch) / (2 * math.pi * net.f_hz) / km * 1e9,
            max_i_ka=i_max / 1000 if math.isfinite(i_max) else 99.0)
    for pt, ends in ((pt, _transformer_ends(model)[pt.identifier]) for pt in _objs(model, cim.PowerTransformer)):
        hv, lv = ends
        s = hv.ratedS.to("MVA")
        u_hv = hv.ratedU.to("kV")
        z_base = u_hv**2 / s
        r = float(hv.r) + float(lv.r) * (u_hv / lv.ratedU.to("kV")) ** 2
        x = float(hv.x) + float(lv.x) * (u_hv / lv.ratedU.to("kV")) ** 2
        pp.create_transformer_from_parameters(
            net, bus_of[hv.Terminal.ConnectivityNode.identifier], bus_of[lv.Terminal.ConnectivityNode.identifier],
            sn_mva=s, vn_hv_kv=u_hv, vn_lv_kv=lv.ratedU.to("kV"),
            vkr_percent=r / z_base * 100, vk_percent=math.hypot(r, x) / z_base * 100,
            pfe_kw=0.0, i0_percent=0.0, name=pt.name)
    for load in _objs(model, cim.EnergyConsumer):
        pp.create_load(net, bus(load), p_mw=load.p.to("MW"), q_mvar=load.q.to("MVAr"), name=load.name)
    for sh in _objs(model, cim.LinearShuntCompensator):
        vn = sh.nomU.to("kV")
        # pandapower: q_mvar > 0 absorbs; CIM: b > 0 is capacitive (injects)
        pp.create_shunt(net, bus(sh), q_mvar=-float(sh.bPerSection) * vn**2, p_mw=float(sh.gPerSection) * vn**2,
                        vn_kv=vn, step=int(sh.sections), max_step=sh.maximumSections, name=sh.name)
    for g in _objs(model, cim.SynchronousMachine):
        b = bus(g)
        pp.create_gen(net, b, p_mw=-g.p.to("MW"), name=g.name,
                      vm_pu=g.RegulatingControl.targetValue / net.bus.vn_kv.at[b],
                      min_q_mvar=g.minQ.to("MVAr"), max_q_mvar=g.maxQ.to("MVAr"),
                      slack=(g.referencePriority or 0) > 0)

    try:
        pp.runpp(net, algorithm="nr", enforce_q_lims=True, numba=False)
    except Exception as e:  # pandapower raises LoadflowNotConverged
        return False, [f"did not converge ({type(e).__name__})"]

    ok = True
    slack_p = net.res_gen[net.gen.slack].p_mw.iloc[0]
    losses = net.res_line.pl_mw.sum() + (net.res_trafo.pl_mw.sum() if len(net.trafo) else 0.0)
    lines = [f"converged; slack P={slack_p:.1f} MW, losses={losses:.2f} MW"]
    for kv in sorted(net.bus.vn_kv.unique()):
        idx = net.bus.index[net.bus.vn_kv == kv]
        vm = net.res_bus.vm_pu[idx]
        bad = [b for b in idx if not vlim[b][0] <= net.res_bus.vm_pu.at[b] <= vlim[b][1]]
        ok &= not bad
        lines.append(f"{kv:g} kV: V=[{vm.min():.3f}, {vm.max():.3f}] pu "
                     f"({vm.min() * kv:.1f}-{vm.max() * kv:.1f} kV)"
                     + (f", {len(bad)} bus(es) OUTSIDE limits" if bad else ""))
    for label, res in (("lines", net.res_line), ("transformers", net.res_trafo)):
        if len(res):
            over = int((res.loading_percent > 100).sum())
            ok &= over == 0
            lines.append(f"{label}: max loading {res.loading_percent.max():.0f} %"
                         + (f", {over} OVERLOADED" if over else ""))
    return ok, lines


def verify(filename: str, expected_counts: Dict[str, int]) -> bool:
    """Read the file back with cim-graph and check it object by object."""
    ok = True
    model = NodeBreakerModel(container=None, connection=XMLFile(filename=filename))
    counts = {cls.__name__: len(objs) for cls, objs in model.graph.items() if objs}
    if counts == expected_counts:
        print(f"  Round-trip: OK ({sum(counts.values())} objects read back, counts match)")
    else:
        ok = False
        print(f"  Round-trip: MISMATCH\n    written: {expected_counts}\n    read:    {counts}")

    problems = check_model(model)
    if problems:
        ok = False
        print(f"  Model checks: {len(problems)} problem(s)")
        for p in problems:
            print(f"    - {p}")
    else:
        print("  Model checks: OK (connected, terminals/ends wired, containers, limits, 1 slack, P within limits)")

    gen_p = sum(-g.p.to("MW") for g in _objs(model, cim.SynchronousMachine))
    load_p = sum(l.p.to("MW") for l in _objs(model, cim.EnergyConsumer))
    print(f"  Balance: generation {gen_p:.1f} MW, load {load_p:.1f} MW")

    pf_ok, pf_lines = run_power_flow(model)
    print(f"  AC power flow: {pf_lines[0]}")
    for line in pf_lines[1:]:
        print(f"    {line}")
    return ok and pf_ok


def main():
    parser = argparse.ArgumentParser(description="Synthetic CIM power grid generator (German/European defaults)")
    parser.add_argument("--buses", type=int, default=40, help="Number of lower-level (110 kV) buses / substations")
    parser.add_argument("--gens", type=int, default=8, help="Number of generators")
    parser.add_argument("--loads", type=int, default=30, help="Number of loads (at lower-level buses)")
    parser.add_argument("--voltage", type=float, default=DEFAULT_VOLTAGE_KV, help="Lower-level nominal voltage (kV)")
    parser.add_argument("--backbone-voltage", type=float, default=DEFAULT_BACKBONE_KV,
                        help="Backbone nominal voltage (kV)")
    parser.add_argument("--backbone-buses", type=int, default=None,
                        help=f"Substations with a backbone bus (default: buses/{BUSES_PER_BACKBONE_SITE}, "
                             f"0 for a single-level grid)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--output", type=str, default="synthetic_grid.cim.xml", help="Output CIM XML path")
    args = parser.parse_args()

    n_backbone = default_backbone_buses(args.buses) if args.backbone_buses is None else args.backbone_buses
    try:
        validate_sizes(args.buses, args.gens, args.loads, n_backbone, args.voltage, args.backbone_voltage)
    except ValueError as e:
        parser.error(str(e))

    levels = f"{args.voltage:g} kV"
    if n_backbone:
        levels = f"{args.backbone_voltage:g}/{args.voltage:g} kV, {n_backbone} backbone buses"
    print(f"Generating synthetic grid: {args.buses} buses, {args.gens} gens, {args.loads} loads "
          f"@ {levels}, {FREQUENCY_HZ:g} Hz (seed={args.seed})")
    network = create_synthetic_cim(
        n_buses=args.buses,
        n_gens=args.gens,
        n_loads=args.loads,
        voltage_kv=args.voltage,
        backbone_kv=args.backbone_voltage,
        n_backbone=n_backbone,
        seed=args.seed,
    )

    utils.write_xml(network, args.output)
    counts = {cls.__name__: len(objs) for cls, objs in network.graph.items()}
    print(f"Wrote CIM RDF/XML → {args.output} ({os.path.getsize(args.output)} bytes)")
    print("Object counts:", counts)

    print("Verification:")
    ok = verify(args.output, counts)
    print("Done." if ok else "Verification FAILED.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
