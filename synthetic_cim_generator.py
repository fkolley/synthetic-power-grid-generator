#!/usr/bin/env python3
"""
Synthetic Power Grid Generator - CIM / CGMES output

Generates a synthetic two-level transmission grid with German / European (ENTSO-E)
default parameters and exports it as
  * CIM17 RDF/XML (CIM100 namespace, typed SI units) through cim-graph, and
  * CGMES 2.4.15 (EQ, TP, SSH, SV, GL, DL profiles in one zip),
both with geographic coordinates (GL) and a schematic diagram layout (DL), so the
grid can be drawn by map- and diagram-based viewers. The SV profile holds the
solved AC power flow. Both outputs are verified: the CIM17 file is read back with
cim-graph and solved with pandapower; the CGMES zip is imported and solved with
PowSyBl (pypowsybl), the reference open-source CGMES importer.

Usage:
    python synthetic_cim_generator.py                       # 40 substations, 380/110 kV
    python synthetic_cim_generator.py --buses 200 --gens 30 --seed 7
    python synthetic_cim_generator.py --buses 12 --backbone-buses 0 --format cim17

Model contents:
    GeographicalRegion -> SubGeographicalRegion -> Substation (one per 110 kV site)
        -> VoltageLevel (110 kV, plus 380 kV at backbone sites)
            -> ConnectivityNode, SynchronousMachine, EnergyConsumer, LinearShuntCompensator
        -> PowerTransformer (380/110 kV) -> PowerTransformerEnd x2
    SubGeographicalRegion -> Line -> ACLineSegment (one per circuit)
    SynchronousMachine -> ThermalGeneratingUnit (P limits), RegulatingControl (voltage setpoint)
    Terminal -> OperationalLimitSet -> CurrentLimit (PATL thermal rating)
    TP: TopologicalNode per ConnectivityNode; SV: SvVoltage, SvPowerFlow, TopologicalIsland
    GL: Location + PositionPoint (WGS84) for substations and line routes
    DL: Diagram with DiagramObjects for buses, lines, transformers, machines, loads, shunts

Network design (details: docs/topology.md, decisions: docs/topology-decisions.md):
    * Substation sites cluster around towns (Thomas cluster process) with a rural
      background; urban substations carry more load.
    * 380 kV backbone: a subset of sites spread over the area, meshed, each coupled to
      the 110 kV grid by 300 MVA 380/110 kV transformers (at least two per site).
    * 110 kV: separate grid groups (Netzgruppen) of one or two 380/110 kV supply
      points each; within a group a spanning tree, closed into rings of 5-20
      substations (no triangles, no crossings, rings through supply points preferred),
      with short stubs (Stichleitungen) of at most two substations.
    * All corridors are double-circuit lines (two systems per tower).
    * Every 110 kV substation has a load by default; stub ends always do.
    * Large units connect to 380 kV, smaller units to 110 kV. Dispatch equals load
      plus a loss allowance, shared in proportion to capacity.
    * DC-power-flow reinforcement adds circuits / transformers until single circuits
      stay below 70 % of their rating and multi-circuit corridors survive the loss
      of one circuit at <= 90 %.
    * Per-bus shunt compensation: capacitors for load Q, reactors for line charging.

German / European defaults:
    * 50 Hz; 380 kV and 110 kV nominal voltages; sites placed around central Germany.
    * Overhead lines with Al/St 265/35 conductors (DIN EN 50182 264-AL1/34-ST1A):
      single conductor at 110 kV, 2-bundle at 220 kV, 4-bundle at 380 kV.
    * Operating voltage ranges from ENTSO-E SO GL (Reg. 2017/1485, Annex II, Continental
      Europe): 110-300 kV 0.90-1.118 pu; 300-400 kV 360-420 kV.

Conventions:
    * Sign: RotatingMachine.p/q, EnergyConsumer.p/q and SvPowerFlow use the load sign
      convention (positive = out of the bus into the equipment), so generator output
      is negative. GeneratingUnit P limits and machine Q limits use generator convention.
    * Transformer series impedance is referred to the high-voltage end (end 1).
    * Slack: the largest machine (on the backbone if there is one) has
      referencePriority=1, all others 0.
    * Reproducibility: the same arguments give the same model and mRIDs. Only the
      file headers' creation timestamp (and, for CIM17, the export UUID) differ.
"""

import argparse
import datetime
import math
import os
import random
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import networkx as nx
import numpy as np

from cim_records import (CIM17_ONLY, DL, EQ, GL, SSH, SV, TP, Enum, Record, RecordSet, Ref,
                         to_cimgraph, write_cgmes, write_cim17)
import grid_verification as verification


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


# Network sizing and topology (see docs/topology.md and docs/topology-decisions.md)
SPACING_KM_110 = 12.0          # typical distance between neighbouring 110 kV substations
BACKBONE_AVG_DEGREE = 2.8      # 2 x corridors / buses, 380 kV (meshed transmission grid)
BUSES_PER_BACKBONE_SITE = 8    # default --backbone-buses = buses / this
ROUTING_FACTOR = 1.15          # line length / straight-line distance

# Site placement: substations cluster around towns (Thomas cluster process) plus a rural background
URBAN_SHARE = 0.6              # share of substations drawn around town centres
SITES_PER_TOWN = 15            # number of town centres = buses / this
TOWN_SIGMA = 1.6               # town radius in units of SPACING_KM_110 (scaled with town weight)
MIN_SITE_SEPARATION = 0.35     # minimum distance between substations, in units of SPACING_KM_110
LOAD_FACTOR = {"urban": 1.3, "rural": 0.75}   # load of urban / rural substations relative to LOAD_MW_RANGE

# 110 kV: ring-and-stub grid groups (Netzgruppen) fed from 380/110 kV supply points
AVG_DEGREE_110 = 2.4           # target 2 x corridors / substations within a group (corridors, not circuits)
RING_SIZE = (5, 20)            # substations per ring created by a ring-closing corridor
PREFERRED_MIN_RING = 6         # rings of 5 are allowed but penalised
RING_LINK_MAX = 3.0            # max ring-closing corridor length / median tree corridor length
STUB_LINK_MAX = 3.5            # same for corridors that close an overly long stub
MAX_STUB_DEPTH = 2             # substations a stub (Stichleitung) may hang off the ring / supply point
STUB_TIP_PENALTY = 0.35        # ring-closing score factor for corridors ending at a stub end
LINE_CLEARANCE = 0.3           # a new corridor may not pass closer to another substation (x SPACING_KM_110)
CIRCUITS_PER_CORRIDOR = 2      # double-circuit overhead lines (two systems per tower) as built in Germany

# Loads and generation
LOAD_MW_RANGE = (5.0, 45.0)    # per 110 kV substation (2-3 x 40 MVA 110/20 kV transformers)
LOAD_Q_RATIO = (0.1, 0.4)      # Q / P
LOSS_ALLOWANCE = 0.03          # dispatch = total load * (1 + LOSS_ALLOWANCE); the slack covers the rest
FLEET_LOADING = (0.5, 0.8)     # allowed total dispatch / total max P of the generator fleet
BACKBONE_GEN_SHARE = 0.5       # share of --gens connected to the backbone
UNIT_MVA = {"backbone": (400.0, 1100.0), "regional": (20.0, 150.0)}  # relative sizes before scaling
RATED_POWER_FACTOR = 0.85
MIN_P_FRACTION = 0.2           # GeneratingUnit.minOperatingP as a fraction of maxOperatingP
GEN_VM_PU = {"backbone": (1.03, 1.045), "regional": (1.02, 1.035)}  # coordinated setpoints, no Q circulation

# Local reactive compensation per bus (LinearShuntCompensator), as in real grids
CAP_COMPENSATION = 0.8         # capacitor covers this share of the bus's load Q
REACTOR_COMPENSATION = 0.75    # reactor absorbs this share of the line charging at the bus
MIN_SHUNT_MVAR = 1.0           # smaller net compensation is not modelled


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------

def default_backbone_buses(n_buses: int) -> int:
    n = round(n_buses / BUSES_PER_BACKBONE_SITE)
    return n if n >= 2 else 0


def clustered_sites(n: int, spacing_km: float, rng: random.Random) -> Tuple[List[Tuple[float, float]], List[str]]:
    """Substation sites: a Thomas cluster process around town centres (town sizes
    Pareto-distributed) plus a uniform rural background, with a minimum separation.
    Returns the positions [km] and "urban" / "rural" per site."""
    side = spacing_km * math.sqrt(n) * 2.0
    margin = 0.1 * side
    n_towns = max(1, round(n / SITES_PER_TOWN))
    towns = [(rng.uniform(margin, side - margin), rng.uniform(margin, side - margin), rng.paretovariate(1.5))
             for _ in range(n_towns)]
    mean_w = sum(t[2] for t in towns) / n_towns
    min_sep = MIN_SITE_SEPARATION * spacing_km
    pos: List[Tuple[float, float]] = []
    kind: List[str] = []
    cell = max(min_sep, 1e-6)
    buckets: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}

    def separated(p) -> bool:
        cx, cy = int(p[0] // cell), int(p[1] // cell)
        return all(math.dist(p, q) >= min_sep
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1) for q in buckets.get((cx + dx, cy + dy), ()))

    while len(pos) < n:
        for _attempt in range(60):
            if rng.random() < URBAN_SHARE:
                cx, cy, w = rng.choices(towns, weights=[t[2] for t in towns])[0]
                sigma = spacing_km * TOWN_SIGMA * min(2.5, math.sqrt(w / mean_w))
                p, k = (rng.gauss(cx, sigma), rng.gauss(cy, sigma)), "urban"
            else:
                p, k = (rng.uniform(0, side), rng.uniform(0, side)), "rural"
            p = (min(side, max(0.0, p[0])), min(side, max(0.0, p[1])))
            if separated(p):
                break
        pos.append(p)
        kind.append(k)
        buckets.setdefault((int(p[0] // cell), int(p[1] // cell)), []).append(p)
    return pos, kind


def build_backbone_mesh(pos: Sequence[Tuple[float, float]], avg_degree: float) -> nx.Graph:
    """380 kV: meshed transmission grid. Minimum spanning tree, then the shortest
    remaining nearest-neighbour corridors up to the target degree, and no substation
    with a single corridor. Edge attribute 'length_km' is the routed line length."""
    n = len(pos)

    def dist(a, b):
        return math.dist(pos[a], pos[b])

    G = nx.Graph()
    G.add_nodes_from(range(n))
    if n < 2:
        return G
    G.add_edges_from(_mst_edges(list(range(n)), pos))
    k = 4  # candidate corridors: each site to its k nearest neighbours
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
    if n >= 3:
        for a in range(n):
            if G.degree(a) < 2:
                b = min((b for b in range(n) if b != a and not G.has_edge(a, b)), key=lambda b: dist(a, b))
                G.add_edge(a, b)
    for a, b in G.edges():
        G.edges[a, b]["length_km"] = max(2.0, dist(a, b) * ROUTING_FACTOR)
    return G


def _mst_edges(nodes: Sequence[int], pos) -> List[Tuple[int, int]]:
    complete = nx.Graph()
    complete.add_nodes_from(nodes)
    complete.add_weighted_edges_from((a, b, math.dist(pos[a], pos[b]))
                                     for i, a in enumerate(nodes) for b in nodes[i + 1:])
    return list(nx.minimum_spanning_tree(complete).edges())


def _segments_cross(p1, p2, p3, p4) -> bool:
    """Proper intersection of segments p1-p2 and p3-p4 (shared end points do not count)."""
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return (v > 1e-9) - (v < -1e-9)
    if p1 in (p3, p4) or p2 in (p3, p4):
        return False
    return orient(p1, p2, p3) * orient(p1, p2, p4) < 0 and orient(p3, p4, p1) * orient(p3, p4, p2) < 0


def _point_segment_distance(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy or 1e-12)))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def supply_groups(pos, bb_sites: List[int], bb_mesh: nx.Graph) -> List[List[int]]:
    """Group the 380/110 kV supply points into grid groups (Netzgruppen) of one or two:
    neighbouring supply points in the 380 kV grid are paired, shortest pairs first."""
    if not bb_sites:
        return [[]]
    pairs = sorted(bb_mesh.edges(), key=lambda e: math.dist(pos[bb_sites[e[0]]], pos[bb_sites[e[1]]]))
    paired, groups = set(), []
    for a, b in pairs:
        if a not in paired and b not in paired:
            paired.update((a, b))
            groups.append([bb_sites[a], bb_sites[b]])
    groups += [[bb_sites[j]] for j in range(len(bb_sites)) if j not in paired]
    return groups


def build_regional_grid(pos, groups: List[List[int]], leaf_budget: int, spacing_km: float,
                        stats: Optional[Dict] = None) -> Tuple[nx.Graph, List[int]]:
    """110 kV ring-and-stub topology, one grid group per entry in `groups`.

    Every substation joins the group of its nearest supply point. Within each group:
    a Euclidean minimum spanning tree, then ring-closing corridors chosen greedily
    (rings of RING_SIZE substations, high detour ratio, short, no crossings, not past
    other substations, rings through a supply point preferred) up to AVG_DEGREE_110;
    stubs longer than MAX_STUB_DEPTH are closed into rings. Finally, if there are more
    stub ends than loads, the deepest stub ends are closed as well. Groups are not
    connected to each other at 110 kV. Returns the graph and the group of each node."""
    n = len(pos)
    supply_of = {s: gi for gi, g in enumerate(groups) for s in g}
    supplies = [s for g in groups for s in g]
    group_of = [supply_of[min(supplies, key=lambda s: math.dist(pos[i], pos[s]))] if supplies else 0
                for i in range(n)]
    members = [[i for i in range(n) if group_of[i] == gi] for gi in range(len(groups))]

    G = nx.Graph()
    G.add_nodes_from(range(n))
    tree = [e for nodes in members for e in _mst_edges(nodes, pos)]
    tree_lengths = sorted(math.dist(pos[a], pos[b]) for a, b in tree) or [spacing_km]
    l_med = tree_lengths[len(tree_lengths) // 2]
    clearance = LINE_CLEARANCE * spacing_km
    dist = lambda a, b: math.dist(pos[a], pos[b])  # noqa: E731

    # Spatial index (grid buckets) so crossing / clearance checks only look at nearby
    # corridors and substations: two segments can only cross if their bounding boxes overlap.
    cell = max(l_med, clearance, 1e-6)
    node_cells: Dict[Tuple[int, int], List[int]] = {}
    edge_cells: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
    for i, p in enumerate(pos):
        node_cells.setdefault((int(p[0] // cell), int(p[1] // cell)), []).append(i)

    def cells(p, q, pad=0.0):
        x0, x1 = sorted((p[0], q[0]))
        y0, y1 = sorted((p[1], q[1]))
        return [(cx, cy) for cx in range(int((x0 - pad) // cell), int((x1 + pad) // cell) + 1)
                for cy in range(int((y0 - pad) // cell), int((y1 + pad) // cell) + 1)]

    def add(u, v, kind):
        G.add_edge(u, v, kind=kind)
        for c in cells(pos[u], pos[v]):
            edge_cells.setdefault(c, []).append((u, v))

    for a, b in tree:
        add(a, b, "tree")

    def geometry_ok(u, v) -> bool:
        pu, pv = pos[u], pos[v]
        near = cells(pu, pv, clearance)
        for c in near:
            for a, b in edge_cells.get(c, ()):
                if _segments_cross(pu, pv, pos[a], pos[b]):
                    return False
        return all(_point_segment_distance(pos[w], pu, pv) >= clearance
                   for c in near for w in node_cells.get(c, ()) if w not in (u, v))

    def close(nodes, sup, ring_size, link_max, sources=None, bonus_supply=True):
        """Best ring-closing corridor for the given sources (all nodes if None), or None."""
        H = G.subgraph(nodes)
        best, best_score = None, 0.0
        for u in (sources if sources is not None else nodes):
            lengths, paths = nx.single_source_dijkstra(H, u, weight=lambda a, b, d: dist(a, b))
            hops = nx.single_source_shortest_path_length(H, u)
            for v in nodes:
                if v == u or G.has_edge(u, v) or v not in paths:
                    continue
                d = dist(u, v)
                if d > link_max * l_med:
                    continue
                ring = hops[v] + 1                   # substations on the smallest ring the corridor creates
                if not ring_size[0] <= ring <= ring_size[1]:
                    continue
                score = (lengths[v] / max(d, 1e-6)) / (d / l_med)     # detour ratio, prefer short corridors
                if ring < PREFERRED_MIN_RING:
                    score *= 0.6
                if bonus_supply and sup and any(s in paths[v] for s in sup):
                    score *= 1.5                     # rings start and end at a supply point
                if sources is None and (H.degree(u) == 1 or H.degree(v) == 1):
                    score *= STUB_TIP_PENALTY        # keep stubs: rings through inner substations
                if score > best_score and geometry_ok(u, v):
                    best, best_score = (u, v), score
        return best

    def stub_depths(nodes, sup):
        H = G.subgraph(nodes)
        bridges = set(map(frozenset, nx.bridges(H))) if H.number_of_edges() else set()
        core = {x for e in H.edges() if frozenset(e) not in bridges for x in e} | set(sup)
        if not core:
            core = {max(nodes, key=H.degree)}
        return {v: d for v, d in nx.multi_source_dijkstra_path_length(H, core, weight=lambda *_: 1).items()}

    added = {"ring": 0, "stub": 0, "leaf": 0}
    for gi, nodes in enumerate(members):
        sup = groups[gi]
        if len(nodes) < RING_SIZE[0]:
            continue
        target = max(len(nodes) - 1, round(AVG_DEGREE_110 * len(nodes) / 2))
        while G.subgraph(nodes).number_of_edges() < target:
            e = close(nodes, sup, RING_SIZE, RING_LINK_MAX)
            if e is None:
                break
            add(*e, "ring")
            added["ring"] += 1
        # Stubs may hang at most MAX_STUB_DEPTH substations off the ring / supply point
        for _ in range(len(nodes)):
            depth = stub_depths(nodes, sup)
            deep = [v for v, d in depth.items() if d > MAX_STUB_DEPTH and G.degree(v) == 1]
            if not deep:
                break
            tip = max(deep, key=lambda v: depth[v])
            e = close(nodes, sup, (4, RING_SIZE[1] + 5), STUB_LINK_MAX, sources=[tip], bonus_supply=False)
            if e is None:
                break
            add(*e, "ring")
            added["stub"] += 1

    # Every stub end needs a load: close the deepest stub ends if there are not enough loads
    for _ in range(n):
        leaves = [v for v in range(n) if G.degree(v) == 1 and v not in supply_of]
        if len(leaves) <= leaf_budget:
            break
        gi_depth = {}
        for v in leaves:
            gi = group_of[v]
            if gi not in gi_depth:
                gi_depth[gi] = stub_depths(members[gi], groups[gi])
        tip = max(leaves, key=lambda v: gi_depth[group_of[v]].get(v, 0))
        nodes = members[group_of[tip]]
        e = (close(nodes, groups[group_of[tip]], (4, n), STUB_LINK_MAX, sources=[tip], bonus_supply=False)
             or close(nodes, groups[group_of[tip]], (3, n), float("inf"), sources=[tip], bonus_supply=False))
        if e is None:
            break
        add(*e, "ring")
        added["leaf"] += 1

    for a, b in G.edges():
        G.edges[a, b]["length_km"] = max(2.0, dist(a, b) * ROUTING_FACTOR)
    if stats is not None:
        stats.update(added)
    return G, group_of


def topology_metrics(G: nx.Graph, group_of: Sequence[int], supplies: Sequence[int]) -> Dict:
    """Structural statistics of the 110 kV corridor graph (corridors, not circuits)."""
    n = G.number_of_nodes()
    deg = [d for _, d in G.degree()]
    cycles = sorted(len(c) for comp in nx.connected_components(G) if len(comp) > 2
                    for c in nx.minimum_cycle_basis(G.subgraph(comp)))
    lengths = sorted(d["length_km"] for *_, d in G.edges(data=True)) or [0.0]
    supply = set(supplies)
    leaves = [v for v in G.nodes if G.degree(v) == 1 and v not in supply]
    return {
        "groups": len(set(group_of)),
        "corridors": G.number_of_edges(),
        "avg_degree": sum(deg) / n if n else 0.0,
        "leaf_share": len(leaves) / n if n else 0.0,
        "triangles": sum(nx.triangles(G).values()) // 3,
        "clustering": nx.average_clustering(G) if n else 0.0,
        "rings": len(cycles),
        "ring_size": (cycles[0], cycles[len(cycles) // 2], cycles[-1]) if cycles else None,
        "length_km": (lengths[len(lengths) // 2], lengths[int(0.95 * (len(lengths) - 1))]),
    }


def spread_sites(pos: Sequence[Tuple[float, float]], n: int, rng: random.Random) -> List[int]:
    """Pick n sites spread over the area (randomised farthest-point sampling)."""
    chosen = [rng.randrange(len(pos))]
    taken = {chosen[0]}
    far = [math.dist(p, pos[chosen[0]]) for p in pos]       # distance to the nearest chosen site
    while len(chosen) < n:
        remaining = [i for i in range(len(pos)) if i not in taken]
        best = max(far[i] for i in remaining)
        c = rng.choice([i for i in remaining if far[i] >= 0.8 * best])
        chosen.append(c)
        taken.add(c)
        far = [min(f, math.dist(p, pos[c])) for f, p in zip(far, pos)]
    return chosen


def place_generators(G: nx.Graph, n_gens: int, rng: random.Random,
                     candidates: Optional[List[int]] = None) -> List[int]:
    """Spread generators over a network: randomised farthest-point sampling on hop
    distance, so every bus has a generator within a few lines. Only `candidates`
    (default: all nodes) can receive a generator."""
    if n_gens == 0:
        return []
    pool = sorted(candidates if candidates is not None else G.nodes)
    chosen = [rng.choice(pool)]
    taken = {chosen[0]}
    far = dict(nx.single_source_shortest_path_length(G, chosen[0]))   # hops to the nearest chosen node
    while len(chosen) < n_gens:
        remaining = [b for b in pool if b not in taken]
        best = max(far.get(b, math.inf) for b in remaining)
        c = rng.choice([b for b in remaining if far.get(b, math.inf) >= best - 1])
        chosen.append(c)
        taken.add(c)
        for b, d in nx.single_source_shortest_path_length(G, c).items():
            if d < far.get(b, math.inf):
                far[b] = d
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
DC_LOADING_TARGET = 0.7        # single circuit: reinforce until the DC flow is below this share of the rating
N1_CORRIDOR_LOADING = 0.9      # several circuits: after losing one, the others carry at most this share
MAX_LINE_CIRCUITS = 4
MAX_TRANSFORMERS_PER_SITE = 8


def dc_flows(n_bus: int, branches: List[Branch], injection_mw: Sequence[float], slack_bus: int) -> List[float]:
    """DC power flow (sparse). Returns the total MW flow on each branch (all circuits)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import spsolve
    rows, cols, vals = [], [], []
    for br in branches:
        y = br.circuits / br.x_pu
        rows += [br.u, br.v, br.u, br.v]
        cols += [br.u, br.v, br.v, br.u]
        vals += [y, y, -y, -y]
    B = coo_matrix((vals, (rows, cols)), shape=(n_bus, n_bus)).tocsr()
    keep = np.array([b for b in range(n_bus) if b != slack_bus])
    theta = np.zeros(n_bus)
    p = np.asarray(injection_mw, dtype=float) / S_BASE_MVA
    theta[keep] = spsolve(B[keep][:, keep].tocsc(), p[keep])
    return [(theta[br.u] - theta[br.v]) * br.circuits / br.x_pu * S_BASE_MVA for br in branches]


def reinforce(n_bus: int, branches: List[Branch], injection_mw: Sequence[float], slack_bus: int) -> int:
    """Add parallel circuits / transformers until every branch is within its planning
    limit. Single circuits: DC flow at most DC_LOADING_TARGET of the rating. Several
    parallel circuits or transformers: if one of them fails, the others still carry
    the flow at no more than N1_CORRIDOR_LOADING of their rating (N-1 within the
    corridor). Returns the number of units added."""
    def limit(br: Branch) -> float:
        if br.circuits >= 2:
            return N1_CORRIDOR_LOADING * br.rating_mva * (br.circuits - 1)
        return DC_LOADING_TARGET * br.rating_mva

    added = 0
    for _ in range(50):
        flows = dc_flows(n_bus, branches, injection_mw, slack_bus)
        changed = False
        for br, f in zip(branches, flows):
            if abs(f) > limit(br) and br.circuits < br.max_circuits:
                br.circuits += 1
                added += 1
                changed = True
        if not changed:
            break
    return added


@dataclass
class Plan:
    """A planned network: everything needed to emit CIM, no CIM objects yet."""
    seed: int
    voltage_kv: float
    backbone_kv: float
    n_buses: int               # lower-level buses (one per substation)
    bus_kv: List[float]        # per global bus index
    bus_site: List[int]        # substation of each bus
    pos: List[Tuple[float, float]]   # substation coordinates [km]
    branches: List[Branch]
    loads: List[Tuple[int, float, float]]
    gens: List[Gen]
    group_of: List[int] = None       # 110 kV grid group of each substation
    metrics: Dict = None             # 110 kV topology statistics (topology_metrics)


def plan_network(
    n_buses: int = 40,
    n_gens: int = 8,
    n_loads: Optional[int] = None,
    voltage_kv: float = DEFAULT_VOLTAGE_KV,
    backbone_kv: float = DEFAULT_BACKBONE_KV,
    n_backbone: Optional[int] = None,
    seed: int = 42,
) -> Plan:
    """Plan a synthetic grid.

    n_buses is the number of lower-level (110 kV) buses, one per substation.
    n_backbone of those substations also get a backbone (380 kV) bus; None picks
    a default from n_buses, 0 builds a single-level grid. n_loads None puts a load
    on every lower-level substation.
    """
    if n_backbone is None:
        n_backbone = default_backbone_buses(n_buses)
    if n_loads is None:
        n_loads = n_buses
    validate_sizes(n_buses, n_gens, n_loads, n_backbone, voltage_kv, backbone_kv)
    rng = random.Random(seed)
    spacing = SPACING_KM_110 * voltage_kv / 110.0

    # --- Geography: clustered sites, 380 kV mesh, 110 kV ring-and-stub grid groups ---
    pos, site_kind = clustered_sites(n_buses, spacing, rng)
    bb_sites = spread_sites(pos, n_backbone, rng) if n_backbone else []
    bb_mesh = build_backbone_mesh([pos[s] for s in bb_sites], BACKBONE_AVG_DEGREE)
    G_bb = nx.relabel_nodes(bb_mesh, {j: n_buses + j for j in range(n_backbone)})
    groups = supply_groups(pos, bb_sites, bb_mesh)
    G_lv, group_of = build_regional_grid(pos, groups, leaf_budget=n_loads, spacing_km=spacing)
    metrics = topology_metrics(G_lv, group_of, bb_sites)
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
                max_circuits=MAX_LINE_CIRCUITS, circuits=CIRCUITS_PER_CORRIDOR,
                length_km=length_km, r_ohm=r, x_ohm=x, bch_s=bch))

    # --- Bus assignment ------------------------------------------------------------
    n_bb_gens = 0
    if n_backbone:
        n_bb_gens = min(n_backbone, max(1, round(n_gens * BACKBONE_GEN_SHARE)))
    n_lv_gens = n_gens - n_bb_gens
    if n_lv_gens > n_buses:
        n_bb_gens, n_lv_gens = n_gens - n_buses, n_buses
    # Hop distances for spreading regional units over the whole system: the 110 kV grid
    # groups are only connected through the transformers and the 380 kV grid
    G_all = nx.union(G_lv, G_bb)
    G_all.add_edges_from((n_buses + j, s) for j, s in enumerate(bb_sites))
    gen_buses = (place_generators(G_bb, n_bb_gens, rng)
                 + place_generators(G_all, n_lv_gens, rng, candidates=list(range(n_buses))))
    # Loads at 110 kV: stub ends first (a stub always ends at a load), then substations
    # without a generator, then generator substations.
    lv_gen_buses = [b for b in gen_buses if b < n_buses]
    supply = set(bb_sites)
    leaves = [i for i in range(n_buses) if G_lv.degree(i) == 1 and i not in supply]
    others = [i for i in range(n_buses) if i not in leaves and i not in lv_gen_buses]
    rng.shuffle(leaves)
    rng.shuffle(others)
    rest = [i for i in lv_gen_buses if i not in leaves]
    load_buses = (leaves + others + rng.sample(rest, len(rest)))[:n_loads]

    # --- Loads ---------------------------------------------------------------------
    load_scale = (voltage_kv / 110.0) ** 1.5
    loads: List[Tuple[int, float, float]] = []
    for bus_i in load_buses:
        p_mw = round(rng.uniform(*LOAD_MW_RANGE) * load_scale * LOAD_FACTOR[site_kind[bus_i]], 2)
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
            # each substation is supplied by the nearest supply point of its own grid group
            site = min(groups[group_of[i]], key=lambda s: math.dist(pos[i], pos[s]))
            net_demand[bb_sites.index(site)] -= injection[i]
        x_pu = t.uk_percent / 100 * S_BASE_MVA / t.rated_s_mva
        for j, site in enumerate(bb_sites):
            count = max(MIN_TRANSFORMERS_PER_SITE,
                        math.ceil(abs(net_demand[j]) * TRANSFORMER_MARGIN / t.rated_s_mva))
            branches.append(Branch("trafo", n_buses + j, site, x_pu=x_pu, rating_mva=t.rated_s_mva,
                                   max_circuits=MAX_TRANSFORMERS_PER_SITE, circuits=count))

    # --- Reinforcement: parallel circuits / transformers where DC flows exceed ratings
    reinforce(n_total, branches, injection, gens[slack_idx].bus)

    return Plan(seed, voltage_kv, backbone_kv, n_buses, bus_kv, bus_site, pos, branches, loads, gens,
                group_of=group_of, metrics=metrics)


# ---------------------------------------------------------------------------
# CIM records (EQ, TP, SSH, GL, DL; SV added after the power flow)
# ---------------------------------------------------------------------------

GEO_ORIGIN = (51.0, 10.0)      # latitude, longitude of the area centre (central Germany)
KM_PER_DEG_LAT = 111.32
DL_PX_PER_KM = 10.0            # diagram scale
DL_MARGIN = 50.0
DL_BACKBONE_OFFSET = (40.0, -40.0)   # backbone bus drawn up-right of its 110 kV bus
DL_BUSBAR_HALF_WIDTH = 15.0


@dataclass
class Built:
    """Records plus the handles needed to add the SV profile later."""
    records: RecordSet
    tn: List[Record]                    # TopologicalNode per bus
    cn: List[Record]                    # ConnectivityNode per bus
    vl: List[Record]                    # VoltageLevel per bus
    terminals: List[Record]
    shunts: List[Record]
    slack_bus: int


def build_records(plan: Plan) -> Built:
    rs = RecordSet(plan.seed)
    new = rs.new
    n_total = len(plan.bus_kv)
    bus_kv, bus_site = plan.bus_kv, plan.bus_site

    # --- Coordinates -----------------------------------------------------------------
    xs = [p[0] for p in plan.pos]
    ys = [p[1] for p in plan.pos]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    top = max(ys)

    def lat_lon(site: int) -> Tuple[float, float]:
        x, y = plan.pos[site]
        lat = GEO_ORIGIN[0] + (y - cy) / KM_PER_DEG_LAT
        lon = GEO_ORIGIN[1] + (x - cx) / (KM_PER_DEG_LAT * math.cos(math.radians(GEO_ORIGIN[0])))
        return round(lat, 6), round(lon, 6)

    def bus_xy(b: int) -> Tuple[float, float]:
        """Diagram position (screen coordinates, y down, north up)."""
        x, y = plan.pos[bus_site[b]]
        px, py = DL_MARGIN + (x - min(xs)) * DL_PX_PER_KM, DL_MARGIN + (top - y) * DL_PX_PER_KM
        if b >= plan.n_buses:
            px, py = px + DL_BACKBONE_OFFSET[0], py + DL_BACKBONE_OFFSET[1]
        return round(px, 1), round(py, 1)

    # --- Containers, base voltages, buses, topological nodes ---------------------
    region = new("GeographicalRegion", EQ, "SyntheticRegion")
    subregion = new("SubGeographicalRegion", EQ, "SyntheticSubRegion").set("Region", Ref(region.id))

    base_voltages = {}
    for kv in sorted(set(bus_kv)):
        base_voltages[kv] = new("BaseVoltage", EQ, f"{kv:g}kV").set("nominalVoltage", kv)

    substations = [new("Substation", EQ, f"Sub_{i:03d}").set("Region", Ref(subregion.id))
                   for i in range(plan.n_buses)]

    voltage_levels, cns, tns = [], [], []
    for b in range(n_total):
        kv, site = bus_kv[b], bus_site[b]
        lo, hi = voltage_limits_pu(kv)
        bv = Ref(base_voltages[kv].id)
        vl = (new("VoltageLevel", EQ, f"VL_{site:03d}_{kv:g}kV")
              .set("Substation", Ref(substations[site].id)).set("BaseVoltage", bv)
              .set("lowVoltageLimit", round(lo * kv, 3)).set("highVoltageLimit", round(hi * kv, 3)))
        name = f"Bus_{kv:g}kV_{site:03d}"
        tn = (new("TopologicalNode", TP, name).set("BaseVoltage", bv)
              .set("ConnectivityNodeContainer", Ref(vl.id)))
        cn = (new("ConnectivityNode", EQ, name).set("ConnectivityNodeContainer", Ref(vl.id))
              .set("TopologicalNode", Ref(tn.id), TP))
        voltage_levels.append(vl)
        cns.append(cn)
        tns.append(tn)

    terminals: List[Record] = []

    def connect(eq: Record, bus_i: int, seq: int = 1) -> Record:
        term = (new("Terminal", EQ, f"{eq.get('name')}_T{seq}").set("sequenceNumber", seq)
                .set("ConductingEquipment", Ref(eq.id)).set("ConnectivityNode", Ref(cns[bus_i].id))
                .set("TopologicalNode", Ref(tns[bus_i].id), TP).set("connected", True, SSH))
        terminals.append(term)
        return term

    patl = (new("OperationalLimitType", EQ, "PATL")
            .set("direction", Enum("OperationalLimitDirectionKind", "absoluteValue"))
            .set("entsoe:OperationalLimitType.limitType", Enum("LimitTypeKind", "patl", "entsoe"))
            .set("isInfiniteDuration", True, CIM17_ONLY))

    def current_limit(term: Record, amps: float):
        amps = round(amps, 1)
        ols = new("OperationalLimitSet", EQ, f"{term.get('name')}_OLS").set("Terminal", Ref(term.id))
        (new("CurrentLimit", EQ, f"{term.get('name')}_PATL").set("OperationalLimitSet", Ref(ols.id))
         .set("OperationalLimitType", Ref(patl.id)).set("value", amps).set("normalValue", amps, CIM17_ONLY))

    # --- Geography (GL) and diagram (DL) helpers ----------------------------------
    crs = new("CoordinateSystem", GL, "WGS84").set("crsUrn", "urn:ogc:def:crs:EPSG::4326")

    def locate(psr: Record, sites: Sequence[int]):
        loc = (new("Location", GL, f"{psr.get('name')}_Loc").set("CoordinateSystem", Ref(crs.id))
               .set("PowerSystemResources", Ref(psr.id)))
        for k, site in enumerate(sites, start=1):
            lat, lon = lat_lon(site)
            (new("PositionPoint", GL).set("Location", Ref(loc.id)).set("sequenceNumber", k)
             .set("xPosition", f"{lon:.6f}").set("yPosition", f"{lat:.6f}"))

    diagram = new("Diagram", DL, "Overview").set("orientation", Enum("OrientationKind", "negative"))

    def draw(obj: Record, points: Sequence[Tuple[float, float]], order: int):
        do = (new("DiagramObject", DL, f"{obj.get('name')}_DO").set("Diagram", Ref(diagram.id))
              .set("IdentifiedObject", Ref(obj.id)).set("rotation", 0.0)
              .set("drawingOrder", order).set("isPolygon", False))
        for k, (x, y) in enumerate(points, start=1):
            (new("DiagramObjectPoint", DL).set("DiagramObject", Ref(do.id)).set("sequenceNumber", k)
             .set("xPosition", float(round(x, 1))).set("yPosition", float(round(y, 1))))

    for i, sub in enumerate(substations):
        locate(sub, [i])
    for b in range(n_total):
        x, y = bus_xy(b)
        draw(cns[b], [(x - DL_BUSBAR_HALF_WIDTH, y), (x + DL_BUSBAR_HALF_WIDTH, y)], 3)

    # --- Lines: one Line container per corridor, one ACLineSegment per circuit -----
    bus_charging_mvar = [0.0] * n_total   # half of each circuit's charging at each end
    for br in (b for b in plan.branches if b.kind == "line"):
        kv = bus_kv[br.u]
        lt = line_type(kv)
        name = f"Line_{kv:g}kV_{bus_site[br.u]:03d}_{bus_site[br.v]:03d}"
        line_container = new("Line", EQ, name).set("Region", Ref(subregion.id))
        (x1, y1), (x2, y2) = bus_xy(br.u), bus_xy(br.v)
        length = math.hypot(x2 - x1, y2 - y1) or 1.0
        nx_, ny_ = -(y2 - y1) / length, (x2 - x1) / length        # unit normal for parallel circuits
        for c in range(1, br.circuits + 1):
            seg = (new("ACLineSegment", EQ, f"{name}_C{c}")
                   .set("description", f"{lt.name}, circuit {c} of {br.circuits}")
                   .set("EquipmentContainer", Ref(line_container.id))
                   .set("BaseVoltage", Ref(base_voltages[kv].id))
                   .set("length", round(br.length_km, 2))
                   .set("r", br.r_ohm).set("x", br.x_ohm).set("bch", br.bch_s).set("gch", 0.0))
            for seq, b in ((1, br.u), (2, br.v)):
                current_limit(connect(seg, b, seq), lt.i_th_a)
                bus_charging_mvar[b] += br.bch_s * kv**2 / 2
            locate(seg, [bus_site[br.u], bus_site[br.v]])
            off = (c - (br.circuits + 1) / 2) * 6.0
            draw(seg, [(x1 + nx_ * off, y1 + ny_ * off), (x2 + nx_ * off, y2 + ny_ * off)], 1)

    # Per-bus slots for drawing equipment next to its busbar
    slots: Dict[Tuple[int, str], int] = {}

    def slot_xy(bus_i: int, kind: str) -> Tuple[float, float]:
        k = slots.get((bus_i, kind), 0)
        slots[(bus_i, kind)] = k + 1
        x, y = bus_xy(bus_i)
        dx, dy = {"gen": (0.0, -22.0), "load": (0.0, 22.0), "shunt": (24.0, 8.0)}[kind]
        return x + dx + 10.0 * k, y + dy

    # --- Loads -----------------------------------------------------------------------
    bus_load_q = [0.0] * n_total
    for idx, (bus_i, p_mw, q_mvar) in enumerate(plan.loads):
        bus_load_q[bus_i] += q_mvar
        load = (new("EnergyConsumer", EQ, f"Load_{idx:02d}")
                .set("EquipmentContainer", Ref(voltage_levels[bus_i].id))
                .set("BaseVoltage", Ref(base_voltages[bus_kv[bus_i]].id))
                .set("p", p_mw, SSH).set("q", q_mvar, SSH))
        connect(load, bus_i)
        draw(load, [slot_xy(bus_i, "load")], 2)

    # --- Shunt compensation: capacitors at load buses, reactors against line charging
    shunts: List[Record] = []
    for bus_i in range(n_total):
        q_mvar = CAP_COMPENSATION * bus_load_q[bus_i] - REACTOR_COMPENSATION * bus_charging_mvar[bus_i]
        if abs(q_mvar) < MIN_SHUNT_MVAR:
            continue
        kv = bus_kv[bus_i]
        kind = "Cap" if q_mvar > 0 else "Reactor"
        shunt = (new("LinearShuntCompensator", EQ, f"{kind}_{kv:g}kV_{bus_site[bus_i]:03d}")
                 .set("EquipmentContainer", Ref(voltage_levels[bus_i].id))
                 .set("BaseVoltage", Ref(base_voltages[kv].id))
                 .set("nomU", kv)
                 .set("bPerSection", round(q_mvar / kv**2, 9))            # b > 0: capacitive
                 .set("gPerSection", 0.0)
                 .set("maximumSections", 1).set("normalSections", 1).set("grounded", True)
                 .set("sections", 1.0, SSH).set("controlEnabled", False, SSH))
        connect(shunt, bus_i)
        draw(shunt, [slot_xy(bus_i, "shunt")], 2)
        shunts.append(shunt)

    # --- Generators (SynchronousMachine + ThermalGeneratingUnit + RegulatingControl) --
    fleet_max_p = sum(g.max_p for g in plan.gens)
    for idx, g in enumerate(plan.gens):
        kv = bus_kv[g.bus]
        name = f"Gen_{kv:g}kV_{idx:02d}"
        vl = Ref(voltage_levels[g.bus].id)
        unit = (new("ThermalGeneratingUnit", EQ, f"{name}_GU").set("EquipmentContainer", vl)
                .set("maxOperatingP", round(g.max_p, 2))
                .set("minOperatingP", round(MIN_P_FRACTION * g.max_p, 2))
                .set("nominalP", round(g.max_p, 2)).set("initialP", g.p)
                .set("genControlSource", Enum("GeneratorControlSource", "offAGC"))
                .set("normalPF", round(g.max_p / fleet_max_p, 4), SSH))
        q_cap = g.rated_s * math.sqrt(1 - RATED_POWER_FACTOR**2)
        gen = (new("SynchronousMachine", EQ, name).set("EquipmentContainer", vl)
               .set("BaseVoltage", Ref(base_voltages[kv].id)).set("GeneratingUnit", Ref(unit.id))
               .set("type", Enum("SynchronousMachineKind", "generator"))
               .set("ratedS", g.rated_s).set("ratedU", kv).set("ratedPowerFactor", RATED_POWER_FACTOR)
               .set("maxQ", round(q_cap, 2)).set("minQ", round(-0.5 * q_cap, 2))
               # SSH: load sign convention
               .set("p", -g.p, SSH).set("q", -g.q0, SSH)
               .set("referencePriority", 1 if g.slack else 0, SSH)
               .set("operatingMode", Enum("SynchronousMachineOperatingMode", "generator"), SSH)
               .set("controlEnabled", True, SSH))
        term = connect(gen, g.bus)
        rc = (new("RegulatingControl", EQ, f"{name}_RC")
              .set("mode", Enum("RegulatingControlModeKind", "voltage")).set("Terminal", Ref(term.id))
              .set("discrete", False, SSH).set("enabled", True, SSH).set("targetDeadband", 0.5, SSH)
              .set("targetValue", round(g.vm_pu * kv, 3), SSH)
              .set("targetValueUnitMultiplier", Enum("UnitMultiplier", "k"), SSH))
        gen.set("RegulatingControl", Ref(rc.id))
        draw(gen, [slot_xy(g.bus, "gen")], 2)

    # --- Transformers ----------------------------------------------------------------
    t = transformer_type(plan.backbone_kv, plan.voltage_kv)
    z_base = plan.backbone_kv**2 / t.rated_s_mva
    r_ohm = t.ur_percent / 100 * z_base
    x_ohm = math.sqrt((t.uk_percent / 100 * z_base) ** 2 - r_ohm**2)
    for br in (b for b in plan.branches if b.kind == "trafo"):
        site = bus_site[br.v]
        (xh, yh), (xl, yl) = bus_xy(br.u), bus_xy(br.v)
        for k in range(br.circuits):
            pt = (new("PowerTransformer", EQ, f"TR_{site:03d}_{k + 1}")
                  .set("EquipmentContainer", Ref(substations[site].id))
                  .set("vectorGroup", "YNyn0", CIM17_ONLY))
            for end_no, bus_i in ((1, br.u), (2, br.v)):
                kv = bus_kv[bus_i]
                end = (new("PowerTransformerEnd", EQ, f"{pt.get('name')}_End{end_no}")
                       .set("PowerTransformer", Ref(pt.id)).set("endNumber", end_no)
                       .set("BaseVoltage", Ref(base_voltages[kv].id))
                       .set("ratedS", t.rated_s_mva).set("ratedU", kv)
                       .set("r", round(r_ohm if end_no == 1 else 0.0, 4))
                       .set("x", round(x_ohm if end_no == 1 else 0.0, 4))
                       .set("g", 0.0).set("b", 0.0)
                       .set("connectionKind", Enum("WindingConnection", "Yn")).set("phaseAngleClock", 0))
                term = connect(pt, bus_i, end_no)
                end.set("Terminal", Ref(term.id))
                current_limit(term, t.rated_s_mva / (math.sqrt(3) * kv) * 1000)
            off = (k - (br.circuits - 1) / 2) * 8.0
            draw(pt, [((xh + xl) / 2 + off, (yh + yl) / 2)], 2)

    slack_bus = next(g.bus for g in plan.gens if g.slack)
    return Built(rs, tns, cns, voltage_levels, terminals, shunts, slack_bus)


def add_state_variables(built: Built, state: Dict) -> None:
    """Add the SV profile from a solved power flow (see verification.solve_state)."""
    new = built.records.new
    (new("TopologicalIsland", SV, "Island_1")
     .set("AngleRefTopologicalNode", Ref(built.tn[built.slack_bus].id))
     .set("TopologicalNodes", [Ref(tn.id) for tn in built.tn]))
    for tn, cn in zip(built.tn, built.cn):
        v_kv, angle = state["bus"][cn.id]
        new("SvVoltage", SV).set("TopologicalNode", Ref(tn.id)).set("v", round(v_kv, 4)).set("angle", round(angle, 4))
    for term in built.terminals:
        p, q = state["terminal"][term.id]
        new("SvPowerFlow", SV).set("Terminal", Ref(term.id)).set("p", round(p, 4)).set("q", round(q, 4))
    for sh in built.shunts:
        new("SvShuntCompensatorSections", SV).set("ShuntCompensator", Ref(sh.id)).set("sections", 1.0)


def generate(**kwargs) -> Tuple[Plan, Built, Optional[Dict]]:
    """Plan the grid, build all records and, if pandapower is available and the
    power flow converges, the SV profile. Returns (plan, built, state)."""
    plan = plan_network(**kwargs)
    built = build_records(plan)
    state = verification.solve_state(to_cimgraph(built.records.records), FREQUENCY_HZ)
    if state is not None:
        add_state_variables(built, state)
    return plan, built, state


def create_synthetic_cim(**kwargs):
    """In-memory cim-graph (CIM17) model of a synthetic grid."""
    _, built, _ = generate(**kwargs)
    return to_cimgraph(built.records.records)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

SCENARIO_TIME = datetime.datetime(2025, 1, 15, 11, 30, tzinfo=datetime.timezone.utc)  # winter weekday


# ---------------------------------------------------------------------------
# Parameter files (TOML)
# ---------------------------------------------------------------------------

def _number(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("expected a number")
    return float(v)


def _integer(v):
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("expected an integer")
    return v


def _pair(convert):
    def conv(v):
        if not isinstance(v, list) or len(v) != 2:
            raise ValueError("expected a list of two values, e.g. [1, 2]")
        a, b = convert(v[0]), convert(v[1])
        if a > b:
            raise ValueError("first value must not be larger than the second")
        return (a, b)
    return conv


def _boolean(v):
    if not isinstance(v, bool):
        raise ValueError("expected true or false")
    return v


def _text(v):
    if not isinstance(v, str):
        raise ValueError("expected a string")
    return v


def _loads(v):
    if v == "all":
        return None
    return _integer(v)


def _origin(v):
    lat, lon = (_number(x) for x in (v if isinstance(v, list) and len(v) == 2 else [None, None]))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("expected [latitude, longitude]")
    return (lat, lon)


def _output_format(v):
    if v not in ("both", "cim17", "cgmes"):
        raise ValueError('expected "both", "cim17" or "cgmes"')
    return v


# (section, key) -> (command-line option, converter); command-line arguments override these
PARAM_OPTIONS = {
    ("grid", "buses"): ("buses", _integer),
    ("grid", "generators"): ("gens", _integer),
    ("grid", "loads"): ("loads", _loads),
    ("grid", "voltage_kv"): ("voltage", _number),
    ("grid", "backbone_voltage_kv"): ("backbone_voltage", _number),
    ("grid", "backbone_buses"): ("backbone_buses", _integer),
    ("grid", "seed"): ("seed", _integer),
    ("output", "format"): ("format", _output_format),
    ("output", "cim17"): ("output", _text),
    ("output", "cgmes"): ("cgmes_output", _text),
    ("output", "n1_report"): ("skip_n1", lambda v: not _boolean(v)),
}

# (section, key) -> (module constant, converter) or ((constant, dict key), converter)
PARAM_CONSTANTS = {
    ("geography", "spacing_km"): ("SPACING_KM_110", _number),
    ("geography", "origin"): ("GEO_ORIGIN", _origin),
    ("geography", "urban_share"): ("URBAN_SHARE", _number),
    ("geography", "sites_per_town"): ("SITES_PER_TOWN", _number),
    ("geography", "town_sigma"): ("TOWN_SIGMA", _number),
    ("geography", "min_site_separation"): ("MIN_SITE_SEPARATION", _number),
    ("geography", "routing_factor"): ("ROUTING_FACTOR", _number),
    ("topology", "avg_degree"): ("AVG_DEGREE_110", _number),
    ("topology", "ring_size"): ("RING_SIZE", _pair(_integer)),
    ("topology", "preferred_min_ring"): ("PREFERRED_MIN_RING", _integer),
    ("topology", "ring_link_max"): ("RING_LINK_MAX", _number),
    ("topology", "stub_link_max"): ("STUB_LINK_MAX", _number),
    ("topology", "max_stub_depth"): ("MAX_STUB_DEPTH", _integer),
    ("topology", "stub_tip_penalty"): ("STUB_TIP_PENALTY", _number),
    ("topology", "line_clearance"): ("LINE_CLEARANCE", _number),
    ("backbone", "avg_degree"): ("BACKBONE_AVG_DEGREE", _number),
    ("backbone", "buses_per_site"): ("BUSES_PER_BACKBONE_SITE", _number),
    ("lines", "circuits_per_corridor"): ("CIRCUITS_PER_CORRIDOR", _integer),
    ("lines", "max_circuits"): ("MAX_LINE_CIRCUITS", _integer),
    ("transformers", "min_per_site"): ("MIN_TRANSFORMERS_PER_SITE", _integer),
    ("transformers", "max_per_site"): ("MAX_TRANSFORMERS_PER_SITE", _integer),
    ("transformers", "margin"): ("TRANSFORMER_MARGIN", _number),
    ("loads", "mw_range"): ("LOAD_MW_RANGE", _pair(_number)),
    ("loads", "q_ratio"): ("LOAD_Q_RATIO", _pair(_number)),
    ("loads", "urban_factor"): (("LOAD_FACTOR", "urban"), _number),
    ("loads", "rural_factor"): (("LOAD_FACTOR", "rural"), _number),
    ("generation", "backbone_share"): ("BACKBONE_GEN_SHARE", _number),
    ("generation", "backbone_unit_mva"): (("UNIT_MVA", "backbone"), _pair(_number)),
    ("generation", "regional_unit_mva"): (("UNIT_MVA", "regional"), _pair(_number)),
    ("generation", "fleet_loading"): ("FLEET_LOADING", _pair(_number)),
    ("generation", "rated_power_factor"): ("RATED_POWER_FACTOR", _number),
    ("generation", "loss_allowance"): ("LOSS_ALLOWANCE", _number),
    ("generation", "backbone_vm_pu"): (("GEN_VM_PU", "backbone"), _pair(_number)),
    ("generation", "regional_vm_pu"): (("GEN_VM_PU", "regional"), _pair(_number)),
    ("compensation", "capacitor_share"): ("CAP_COMPENSATION", _number),
    ("compensation", "reactor_share"): ("REACTOR_COMPENSATION", _number),
    ("compensation", "min_mvar"): ("MIN_SHUNT_MVAR", _number),
    ("planning", "dc_loading_target"): ("DC_LOADING_TARGET", _number),
    ("planning", "n1_corridor_loading"): ("N1_CORRIDOR_LOADING", _number),
}


def load_parameter_file(path: str) -> Tuple[Dict, Dict]:
    """Read a TOML parameter file. Returns (command-line defaults, module constants).
    Raises ValueError with the file name and key for unknown or invalid entries."""
    import tomllib
    with open(path, "rb") as f:
        data = tomllib.load(f)
    sections = sorted({sec for sec, _ in list(PARAM_OPTIONS) + list(PARAM_CONSTANTS)})
    options, constants = {}, {}
    for section, entries in data.items():
        if not isinstance(entries, dict):
            raise ValueError(f"{path}: '{section}' must be a [section]; sections: {', '.join(sections)}")
        for key, value in entries.items():
            spec = PARAM_OPTIONS.get((section, key)) or PARAM_CONSTANTS.get((section, key))
            if spec is None:
                known = sorted(k for s, k in list(PARAM_OPTIONS) + list(PARAM_CONSTANTS) if s == section)
                hint = f"known keys: {', '.join(known)}" if known else f"sections: {', '.join(sections)}"
                raise ValueError(f"{path}: unknown parameter [{section}] {key}; {hint}")
            target, convert = spec
            try:
                converted = convert(value)
            except (ValueError, TypeError) as e:
                raise ValueError(f"{path}: [{section}] {key} = {value!r}: {e}") from None
            (options if (section, key) in PARAM_OPTIONS else constants)[target] = converted
    return options, constants


def apply_constants(constants: Dict) -> None:
    """Override module constants, e.g. {"SPACING_KM_110": 5.0, ("LOAD_FACTOR", "urban"): 1.2}."""
    module = globals()
    for target, value in constants.items():
        if isinstance(target, tuple):
            name, key = target
            module[name] = {**module[name], key: value}
        else:
            module[target] = value


def main():
    # A parameter file supplies defaults; explicit command-line arguments override it.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--params")
    params_path = pre.parse_known_args()[0].params

    parser = argparse.ArgumentParser(description="Synthetic CIM/CGMES power grid generator "
                                                 "(German/European defaults)")
    parser.add_argument("--buses", type=int, default=40, help="Number of lower-level (110 kV) buses / substations")
    parser.add_argument("--gens", type=int, default=8, help="Number of generators")
    parser.add_argument("--loads", type=int, default=None,
                        help="Number of loads at lower-level buses (default: one per substation)")
    parser.add_argument("--voltage", type=float, default=DEFAULT_VOLTAGE_KV, help="Lower-level nominal voltage (kV)")
    parser.add_argument("--backbone-voltage", type=float, default=DEFAULT_BACKBONE_KV,
                        help="Backbone nominal voltage (kV)")
    parser.add_argument("--backbone-buses", type=int, default=None,
                        help=f"Substations with a backbone bus (default: buses/{BUSES_PER_BACKBONE_SITE}, "
                             f"0 for a single-level grid)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--format", choices=["both", "cim17", "cgmes"], default="both",
                        help="Output format(s): CIM17 RDF/XML, CGMES 2.4.15 zip, or both")
    parser.add_argument("--output", type=str, default="synthetic_grid.cim.xml", help="CIM17 output path")
    parser.add_argument("--cgmes-output", type=str, default=None,
                        help="CGMES zip output path (default: <output stem>_CGMES.zip)")
    parser.add_argument("--skip-n1", action="store_true",
                        help="Skip the N-1 contingency report (saves time on large grids)")
    parser.add_argument("--params", type=str, default=None,
                        help="TOML parameter file (see params/); command-line arguments override it")
    if params_path:
        try:
            options, constants = load_parameter_file(params_path)
        except (OSError, ValueError) as e:
            parser.error(str(e))
        apply_constants(constants)
        parser.set_defaults(**options)
    args = parser.parse_args()

    n_backbone = default_backbone_buses(args.buses) if args.backbone_buses is None else args.backbone_buses
    if args.loads is None:
        args.loads = args.buses
    try:
        validate_sizes(args.buses, args.gens, args.loads, n_backbone, args.voltage, args.backbone_voltage)
    except ValueError as e:
        parser.error(str(e))
    cgmes_out = args.cgmes_output or (args.output.split(".")[0] + "_CGMES.zip")

    levels = f"{args.voltage:g} kV"
    if n_backbone:
        levels = f"{args.backbone_voltage:g}/{args.voltage:g} kV, {n_backbone} backbone buses"
    description = (f"Synthetic grid {args.buses} buses {args.gens} gens {args.loads} loads "
                   f"{levels} seed {args.seed}")
    if params_path:
        print(f"Parameters: {params_path}")
    print(f"Generating synthetic grid: {args.buses} buses, {args.gens} gens, {args.loads} loads "
          f"@ {levels}, {FREQUENCY_HZ:g} Hz (seed={args.seed})")
    plan, built, state = generate(
        n_buses=args.buses, n_gens=args.gens, n_loads=args.loads, voltage_kv=args.voltage,
        backbone_kv=args.backbone_voltage, n_backbone=n_backbone, seed=args.seed)
    records = built.records.records
    counts = built.records.counts()
    print("Object counts:", counts)
    t = plan.metrics
    print(f"Topology {args.voltage:g} kV: {t['groups']} grid group(s), {t['corridors']} corridors "
          f"({CIRCUITS_PER_CORRIDOR}+ circuits each), avg degree {t['avg_degree']:.2f}, "
          f"stub ends {t['leaf_share']:.0%}, rings {t['rings']}"
          + (f" (size {t['ring_size'][0]}-{t['ring_size'][2]}, median {t['ring_size'][1]})" if t['ring_size'] else "")
          + f", triangles {t['triangles']}, clustering {t['clustering']:.2f}")
    if state is None:
        print("Note: no SV profile (pandapower missing or power flow did not converge)")

    ok = True
    if args.format in ("both", "cim17"):
        write_cim17(records, args.output)
        print(f"Wrote CIM17 RDF/XML → {args.output} ({os.path.getsize(args.output)} bytes)")
        print("Verification (CIM17):")
        ok &= verification.verify_cim17(args.output, counts, FREQUENCY_HZ, n1=not args.skip_n1)

    if args.format in ("both", "cgmes"):
        files = write_cgmes(records, cgmes_out, args.seed, description, SCENARIO_TIME)
        print(f"Wrote CGMES 2.4.15 → {cgmes_out} ({os.path.getsize(cgmes_out)} bytes): "
              + ", ".join(f.split("_")[3] for f in files))
        print("Verification (CGMES):")
        expected = {k: counts.get(k, 0) for k in ("TopologicalNode", "ACLineSegment", "PowerTransformer",
                                                  "SynchronousMachine", "EnergyConsumer",
                                                  "LinearShuntCompensator")}
        sv = None
        if state is not None:
            sv = {vl.id: state["bus"][cn.id][0] for vl, cn in zip(built.vl, built.cn)}
        ok &= verification.verify_cgmes(cgmes_out, expected, sv)

    print("Done." if ok else "Verification FAILED.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
