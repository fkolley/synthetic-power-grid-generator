#!/usr/bin/env python3
"""
Synthetic Power Grid Generator - CIM XML Output

Generates a synthetic single-voltage transmission grid and exports CIM RDF/XML
(CIM17 / CIM100 namespace) using cim-graph, then verifies the file by reading it
back and solving an AC power flow on the read-back model.

Usage:
    python synthetic_cim_generator.py --buses 10 --gens 3 --loads 7 --seed 42 --output grid.cim.xml

Model contents:
    GeographicalRegion -> SubGeographicalRegion -> Substation (one per bus)
        -> VoltageLevel -> ConnectivityNode, SynchronousMachine, EnergyConsumer,
                           LinearShuntCompensator
    SubGeographicalRegion -> Line -> ACLineSegment
    SynchronousMachine -> GeneratingUnit (P limits), RegulatingControl (voltage setpoint)

Network design:
    * Geographic topology: random substation sites, minimum spanning tree plus
      nearest-neighbour corridors; line length follows distance.
    * Generators spread over the network (farthest-point sampling); dispatch equals
      load plus a loss allowance, shared in proportion to capacity.
    * Per-bus shunt compensation: capacitors for load Q, reactors for line charging.
    * Bus spacing and load size scale with --voltage.

Conventions:
    * Units: the CIM17 profile uses unprefixed SI units (V, W, VAr, VA, ohm, S, m).
      All values are written as typed quantities, e.g.
      <cim:BaseVoltage.nominalVoltage rdf:datatype="...#UnitSymbol.volt">138000.0</...>
    * Sign: RotatingMachine.p/q and EnergyConsumer.p/q use the load sign convention
      (positive = power flowing out of the network), so generator output is negative.
      GeneratingUnit P limits and SynchronousMachine Q limits use generator convention.
    * Slack: the largest machine has referencePriority=1, all others 0.
    * Reproducibility: the same --seed gives the same model and mRIDs. Only the
      md:FullModel header (export UUID and timestamp) differs between exports.
"""

import argparse
import math
import os
import random
import sys
import uuid
from typing import Dict, List, Tuple

# The profile must be selected before cim-graph is imported.
CIM_PROFILE = "cimprofiles.cim17.cim17v40"
os.environ["CIMG_CIM_PROFILE"] = CIM_PROFILE
os.environ["CIMG_USE_UNITS"] = "true"

import networkx as nx
import cimprofiles.cim17.cim17v40 as cim
from cimprofiles.units import CIMUnit
from cimgraph import utils
from cimgraph.databases import XMLFile
from cimgraph.models import GraphModel, NodeBreakerModel

FREQUENCY_HZ = 60.0
LOSS_ALLOWANCE = 0.03          # dispatch = total load * (1 + LOSS_ALLOWANCE); the slack covers the rest
FLEET_LOADING = (0.5, 0.8)     # allowed total dispatch / total max P of the generator fleet
RATED_POWER_FACTOR = 0.85
MIN_P_FRACTION = 0.2           # GeneratingUnit.minOperatingP as a fraction of maxOperatingP
SLACK_VM_PU = 1.02
PV_VM_PU = (1.00, 1.04)
VOLTAGE_LIMITS_PU = (0.90, 1.10)
BUS_SPACING_KM_138 = 12.0          # substation spacing at 138 kV, scaled linearly with voltage
AVG_DEGREE = 2.8                   # lines per bus x 2 / buses (real grids: ~2.4-2.8)
LOAD_MW_RANGE = (5.0, 40.0)        # per-load active power at 138 kV, scaled by load_scale()
# Local reactive compensation per bus (LinearShuntCompensator), as in real grids:
CAP_COMPENSATION = 0.8             # capacitor covers this share of the bus's load Q
REACTOR_COMPENSATION = 0.75        # reactor absorbs this share of the line charging at the bus
MIN_SHUNT_MVAR = 1.0               # smaller net compensation is not modelled


class IdFactory:
    """Deterministic UUIDv4 mRIDs from a seed, independent of the electrical RNG."""

    def __init__(self, seed: int):
        self._rng = random.Random(f"mrid-{seed}")

    def __call__(self) -> str:
        return str(uuid.UUID(int=self._rng.getrandbits(128), version=4))


def bus_spacing_km(voltage_kv: float) -> float:
    """Typical distance between neighbouring substations (~12 km at 138 kV, ~30 km at 345 kV)."""
    return BUS_SPACING_KM_138 * voltage_kv / 138.0


def load_scale(voltage_kv: float) -> float:
    """Loads grow with voltage level so the network is neither overloaded nor dominated by line charging."""
    return (voltage_kv / 138.0) ** 1.5


def generate_topology(n_buses: int, rng: random.Random, voltage_kv: float,
                      avg_degree: float = None, routing_factor: float = 1.15) -> nx.Graph:
    """Geographic topology: random substation sites, a minimum spanning tree for
    connectivity, then the shortest remaining nearest-neighbour corridors until the
    target average degree is reached (as real grids connect mostly to nearby buses).
    Edge attribute 'length_km' is the routed line length."""
    avg_degree = avg_degree or AVG_DEGREE
    side = bus_spacing_km(voltage_kv) * math.sqrt(n_buses) * 2.0
    pos = [(rng.uniform(0, side), rng.uniform(0, side)) for _ in range(n_buses)]

    def dist(a, b):
        return math.dist(pos[a], pos[b])

    complete = nx.Graph()
    complete.add_weighted_edges_from(
        (a, b, dist(a, b)) for a in range(n_buses) for b in range(a + 1, n_buses))
    G = nx.minimum_spanning_tree(complete)

    k = 4  # candidate corridors: each bus to its k nearest neighbours
    candidates = set()
    for a in range(n_buses):
        for b in sorted((b for b in range(n_buses) if b != a), key=lambda b: dist(a, b))[:k]:
            candidates.add((min(a, b), max(a, b)))
    target_edges = max(n_buses - 1, round(avg_degree * n_buses / 2))
    for a, b in sorted(candidates - set(map(tuple, map(sorted, G.edges()))), key=lambda e: dist(*e)):
        if G.number_of_edges() >= target_edges:
            break
        G.add_edge(a, b)

    for a, b in G.edges():
        G.edges[a, b]["length_km"] = max(2.0, dist(a, b) * routing_factor)
    return G


def place_generators(G: nx.Graph, n_gens: int, rng: random.Random) -> List[int]:
    """Spread generators over the network: randomised farthest-point sampling on
    hop distance, so every load has a generator within a few lines."""
    hops = dict(nx.all_pairs_shortest_path_length(G))
    chosen = [rng.randrange(G.number_of_nodes())]
    while len(chosen) < n_gens:
        remaining = [b for b in G.nodes if b not in chosen]
        far = {b: min(hops[b][c] for c in chosen) for b in remaining}
        best = max(far.values())
        chosen.append(rng.choice(sorted(b for b in remaining if far[b] >= best - 1)))
    return chosen


def realistic_line_params(length_km: float, voltage_kv: float, rng: random.Random) -> Tuple[float, float, float]:
    """Typical overhead-line parameters. Returns total (r [ohm], x [ohm], bch [S])."""
    if voltage_kv >= 300:
        r_per_km = rng.uniform(0.02, 0.08)
        x_per_km = rng.uniform(0.25, 0.40)
        b_per_km = rng.uniform(3e-6, 8e-6)
    else:
        r_per_km = rng.uniform(0.05, 0.15)
        x_per_km = rng.uniform(0.30, 0.50)
        b_per_km = rng.uniform(2e-6, 5e-6)
    return round(r_per_km * length_km, 4), round(x_per_km * length_km, 4), round(b_per_km * length_km, 7)


def validate_sizes(n_buses: int, n_gens: int, n_loads: int) -> None:
    if n_buses < 2:
        raise ValueError("need at least 2 buses")
    if not 1 <= n_gens <= n_buses:
        raise ValueError(f"--gens must be between 1 and the number of buses ({n_buses})")
    if not 1 <= n_loads <= n_buses:
        raise ValueError(f"--loads must be between 1 and the number of buses ({n_buses})")


def create_synthetic_cim(
    n_buses: int = 10,
    n_gens: int = 2,
    n_loads: int = 6,
    voltage_kv: float = 138.0,
    seed: int = 42,
) -> GraphModel:
    """Build a GraphModel of a synthetic grid."""
    validate_sizes(n_buses, n_gens, n_loads)
    rng = random.Random(seed)
    new_id = IdFactory(seed)
    graph: Dict = {}

    def create(cls, name: str):
        obj = utils.create_object(cls, new_id(), graph)
        obj.name = name
        return obj

    # --- Containers and base voltage -------------------------------------------
    bv = create(cim.BaseVoltage, f"{voltage_kv:g}kV")
    bv.nominalVoltage = cim.Voltage(voltage_kv, "kV")

    region = create(cim.GeographicalRegion, "SyntheticRegion")
    subregion = create(cim.SubGeographicalRegion, "SyntheticSubRegion")
    subregion.Region = region

    voltage_levels: List = []
    cns: List = []
    for i in range(n_buses):
        sub = create(cim.Substation, f"Sub_{i:03d}")
        sub.Region = subregion
        vl = create(cim.VoltageLevel, f"VL_{i:03d}_{voltage_kv:g}kV")
        vl.Substation = sub
        vl.BaseVoltage = bv
        vl.lowVoltageLimit = cim.Voltage(VOLTAGE_LIMITS_PU[0] * voltage_kv, "kV")
        vl.highVoltageLimit = cim.Voltage(VOLTAGE_LIMITS_PU[1] * voltage_kv, "kV")
        cn = create(cim.ConnectivityNode, f"Bus_{i:03d}")
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

    # --- Lines -----------------------------------------------------------------
    G = generate_topology(n_buses, rng, voltage_kv)
    bus_charging_mvar = [0.0] * n_buses   # half of each line's charging at each end
    bus_load_q_mvar = [0.0] * n_buses
    for u, v in sorted(tuple(sorted(e)) for e in G.edges()):
        length_km = G.edges[u, v]["length_km"]
        r, x, bch = realistic_line_params(length_km, voltage_kv, rng)
        name = f"Line_{u:03d}_{v:03d}"
        line_container = create(cim.Line, name)
        line_container.Region = subregion
        seg = create(cim.ACLineSegment, f"{name}_ACLS")
        seg.EquipmentContainer = line_container
        seg.BaseVoltage = bv
        seg.length = cim.Length(round(length_km, 2), "km")
        seg.r = cim.Resistance(r, "ohm")
        seg.x = cim.Reactance(x, "ohm")
        seg.bch = cim.Susceptance(bch, "S")
        connect(seg, u, 1)
        connect(seg, v, 2)
        for b in (u, v):
            bus_charging_mvar[b] += bch * voltage_kv**2 / 2

    # --- Bus assignment ----------------------------------------------------------
    gen_buses = place_generators(G, n_gens, rng)
    # Prefer buses without a generator for loads; fall back to generator buses if needed.
    free = [i for i in range(n_buses) if i not in gen_buses]
    rng.shuffle(free)
    load_buses = (free + rng.sample(gen_buses, len(gen_buses)))[:n_loads]

    # --- Loads (EnergyConsumer, load sign convention) ---------------------------
    load_pq = []
    for idx, bus_i in enumerate(load_buses):
        p_mw = round(rng.uniform(*LOAD_MW_RANGE) * load_scale(voltage_kv), 2)
        q_mvar = round(p_mw * rng.uniform(0.1, 0.4), 2)
        load_pq.append((p_mw, q_mvar))
        bus_load_q_mvar[bus_i] += q_mvar
        load = create(cim.EnergyConsumer, f"Load_{idx:02d}")
        load.EquipmentContainer = voltage_levels[bus_i]
        load.BaseVoltage = bv
        load.p = cim.ActivePower(p_mw, "MW")
        load.q = cim.ReactivePower(q_mvar, "MVAr")
        connect(load, bus_i)

    # --- Shunt compensation: capacitors at load buses, reactors against line charging
    for bus_i in range(n_buses):
        q_mvar = CAP_COMPENSATION * bus_load_q_mvar[bus_i] - REACTOR_COMPENSATION * bus_charging_mvar[bus_i]
        if abs(q_mvar) < MIN_SHUNT_MVAR:
            continue
        kind = "Cap" if q_mvar > 0 else "Reactor"
        shunt = create(cim.LinearShuntCompensator, f"{kind}_{bus_i:03d}")
        shunt.EquipmentContainer = voltage_levels[bus_i]
        shunt.BaseVoltage = bv
        shunt.nomU = cim.Voltage(voltage_kv, "kV")
        shunt.bPerSection = cim.Susceptance(round(q_mvar / voltage_kv**2, 9), "S")  # b > 0: capacitive
        shunt.gPerSection = cim.Conductance(0.0, "S")
        shunt.maximumSections = 1
        shunt.normalSections = 1
        shunt.sections = 1.0
        shunt.grounded = True
        connect(shunt, bus_i)

    total_load_p = sum(p for p, _ in load_pq)
    total_load_q = sum(q for _, q in load_pq)
    target_gen_p = total_load_p * (1 + LOSS_ALLOWANCE)

    # --- Generator sizing: random relative sizes, scaled so fleet loading is realistic
    rated_s = [rng.uniform(50.0, 300.0) for _ in gen_buses]
    loading = target_gen_p / (sum(rated_s) * RATED_POWER_FACTOR)
    clamped = min(max(loading, FLEET_LOADING[0]), FLEET_LOADING[1])
    rated_s = [round(s * loading / clamped, 2) for s in rated_s]
    max_p = [s * RATED_POWER_FACTOR for s in rated_s]
    fleet_max_p = sum(max_p)
    slack_idx = max(range(n_gens), key=lambda k: rated_s[k])

    # --- Generators (SynchronousMachine + GeneratingUnit + RegulatingControl) --
    for idx, bus_i in enumerate(gen_buses):
        share = max_p[idx] / fleet_max_p
        p_mw = round(target_gen_p * share, 2)
        q_mvar = round(total_load_q * share, 2)          # initial guess, solved by power flow
        vm_pu = SLACK_VM_PU if idx == slack_idx else round(rng.uniform(*PV_VM_PU), 3)
        name = f"Gen_{idx:02d}"
        vl = voltage_levels[bus_i]

        unit = create(cim.GeneratingUnit, f"{name}_GU")
        unit.EquipmentContainer = vl
        unit.maxOperatingP = cim.ActivePower(round(max_p[idx], 2), "MW")
        unit.minOperatingP = cim.ActivePower(round(MIN_P_FRACTION * max_p[idx], 2), "MW")
        unit.nominalP = cim.ActivePower(round(max_p[idx], 2), "MW")
        unit.initialP = cim.ActivePower(p_mw, "MW")

        gen = create(cim.SynchronousMachine, name)
        gen.EquipmentContainer = vl
        gen.BaseVoltage = bv
        gen.GeneratingUnit = unit
        gen.type = cim.SynchronousMachineKind.generator
        gen.operatingMode = cim.SynchronousMachineOperatingMode.generator
        gen.referencePriority = 1 if idx == slack_idx else 0
        gen.ratedS = cim.ApparentPower(rated_s[idx], "MVA")
        gen.ratedU = cim.Voltage(voltage_kv, "kV")
        gen.ratedPowerFactor = RATED_POWER_FACTOR
        q_cap = rated_s[idx] * math.sqrt(1 - RATED_POWER_FACTOR**2)
        gen.maxQ = cim.ReactivePower(round(q_cap, 2), "MVAr")
        gen.minQ = cim.ReactivePower(round(-0.5 * q_cap, 2), "MVAr")
        gen.p = cim.ActivePower(-p_mw, "MW")      # load sign convention
        gen.q = cim.ReactivePower(-q_mvar, "MVAr")
        term = connect(gen, bus_i)

        rc = create(cim.RegulatingControl, f"{name}_RC")
        rc.mode = cim.RegulatingControlModeKind.voltage
        rc.Terminal = term
        rc.enabled = True
        rc.targetValue = round(vm_pu * voltage_kv, 3)
        rc.targetValueUnitMultiplier = cim.UnitMultiplier.k
        rc.targetDeadband = 0.5
        gen.RegulatingControl = rc
        gen.controlEnabled = True

    return GraphModel(container=None, connection=None, graph=graph)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _objs(model: GraphModel, cls) -> list:
    return list(model.graph.get(cls, {}).values())


def check_model(model: GraphModel) -> List[str]:
    """Structural and electrical sanity checks. Returns a list of problems."""
    problems: List[str] = []
    terms_by_eq: Dict = {}
    G = nx.Graph()
    for cn in _objs(model, cim.ConnectivityNode):
        G.add_node(cn.identifier)
        if cn.ConnectivityNodeContainer is None:
            problems.append(f"{cn.name}: no ConnectivityNodeContainer")
    for t in _objs(model, cim.Terminal):
        if t.ConnectivityNode is None or t.ConductingEquipment is None:
            problems.append(f"{t.name}: dangling terminal")
            continue
        terms_by_eq.setdefault(t.ConductingEquipment.identifier, []).append(t)

    for seg in _objs(model, cim.ACLineSegment):
        ts = terms_by_eq.get(seg.identifier, [])
        if len(ts) != 2:
            problems.append(f"{seg.name}: {len(ts)} terminals, expected 2")
        else:
            G.add_edge(ts[0].ConnectivityNode.identifier, ts[1].ConnectivityNode.identifier)
        for attr in ("r", "x", "bch", "length"):
            if not isinstance(getattr(seg, attr), CIMUnit):
                problems.append(f"{seg.name}.{attr}: missing or untyped")

    for cls in (cim.ACLineSegment, cim.SynchronousMachine, cim.EnergyConsumer, cim.LinearShuntCompensator):
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


def run_power_flow(model: GraphModel) -> Tuple[bool, str]:
    """Convert the read-back CIM model to pandapower and solve an AC power flow."""
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import pandapower as pp
    except ImportError:
        return True, "skipped (pandapower not installed)"

    net = pp.create_empty_network(f_hz=FREQUENCY_HZ)
    bus_of: Dict = {}
    for cn in _objs(model, cim.ConnectivityNode):
        vn_kv = cn.ConnectivityNodeContainer.BaseVoltage.nominalVoltage.to("kV")
        bus_of[cn.identifier] = pp.create_bus(net, vn_kv=vn_kv, name=cn.name)

    terms_by_eq: Dict = {}
    for t in _objs(model, cim.Terminal):
        terms_by_eq.setdefault(t.ConductingEquipment.identifier, []).append(t)

    def bus(eq, i=0):
        ts = sorted(terms_by_eq[eq.identifier], key=lambda t: t.sequenceNumber)
        return bus_of[ts[i].ConnectivityNode.identifier]

    for seg in _objs(model, cim.ACLineSegment):
        km = seg.length.to("km")
        pp.create_line_from_parameters(
            net, bus(seg, 0), bus(seg, 1), length_km=km, name=seg.name,
            r_ohm_per_km=float(seg.r) / km, x_ohm_per_km=float(seg.x) / km,
            c_nf_per_km=float(seg.bch) / (2 * math.pi * FREQUENCY_HZ) / km * 1e9,
            max_i_ka=1.0)
    for load in _objs(model, cim.EnergyConsumer):
        pp.create_load(net, bus(load), p_mw=load.p.to("MW"), q_mvar=load.q.to("MVAr"), name=load.name)
    for sh in _objs(model, cim.LinearShuntCompensator):
        b = bus(sh)
        vn = sh.nomU.to("kV")
        # pandapower: q_mvar > 0 absorbs; CIM: b > 0 is capacitive (injects)
        pp.create_shunt(net, b, q_mvar=-float(sh.bPerSection) * vn**2, p_mw=float(sh.gPerSection) * vn**2,
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
        return False, f"did not converge ({type(e).__name__})"

    slack_row = net.res_gen[net.gen.slack]
    losses = net.res_line.pl_mw.sum()
    vmin, vmax = net.res_bus.vm_pu.min(), net.res_bus.vm_pu.max()
    in_band = VOLTAGE_LIMITS_PU[0] <= vmin and vmax <= VOLTAGE_LIMITS_PU[1]
    summary = (f"converged; slack P={slack_row.p_mw.iloc[0]:.1f} MW, losses={losses:.2f} MW, "
               f"V=[{vmin:.3f}, {vmax:.3f}] pu" + ("" if in_band else " (OUTSIDE voltage limits)"))
    return in_band, summary


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
        print("  Model checks: OK (connected, terminals wired, containers, 1 slack, P within limits)")

    gen_p = sum(-g.p.to("MW") for g in _objs(model, cim.SynchronousMachine))
    load_p = sum(l.p.to("MW") for l in _objs(model, cim.EnergyConsumer))
    print(f"  Balance: generation {gen_p:.1f} MW, load {load_p:.1f} MW")

    pf_ok, pf_msg = run_power_flow(model)
    print(f"  AC power flow: {pf_msg}")
    return ok and pf_ok


def main():
    parser = argparse.ArgumentParser(description="Synthetic CIM power grid generator")
    parser.add_argument("--buses", type=int, default=12, help="Number of buses/connectivity nodes")
    parser.add_argument("--gens", type=int, default=3, help="Number of generators")
    parser.add_argument("--loads", type=int, default=8, help="Number of loads")
    parser.add_argument("--voltage", type=float, default=138.0, help="Nominal voltage (kV)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--output", type=str, default="synthetic_grid.cim.xml", help="Output CIM XML path")
    args = parser.parse_args()

    try:
        validate_sizes(args.buses, args.gens, args.loads)
    except ValueError as e:
        parser.error(str(e))

    print(f"Generating synthetic grid: {args.buses} buses, {args.gens} gens, {args.loads} loads "
          f"@ {args.voltage:g} kV (seed={args.seed})")
    network = create_synthetic_cim(
        n_buses=args.buses,
        n_gens=args.gens,
        n_loads=args.loads,
        voltage_kv=args.voltage,
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
