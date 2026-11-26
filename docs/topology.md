# Topology generation

This document describes how the generator builds the network topology: where
substations are, which substations are connected, with how many circuits, and where
generation, load and transformers go. It is the planning stage in `plan_network()` in
`synthetic_cim_generator.py`. Everything after it (CIM objects, power flow, file export)
works on the plan it produces.

The reasons behind each design choice are in the
[decision log](topology-decisions.md). This document describes *what* the algorithm does.

## Overview

```
 1. Substation sites          clustered_sites()       towns + rural background
 2. Supply points (380/110)   spread_sites()          spread over the area
 3. 380 kV backbone           build_backbone_mesh()   meshed
 4. Grid groups               supply_groups()         one or two supply points each
 5. 110 kV ring-and-stub grid build_regional_grid()   tree → rings → stub repair
 6. Line parameters           line_params()           per corridor, double-circuit
 7. Generators                place_generators()      spread by hop distance
 8. Loads                     plan_network()          stub ends first
 9. Transformers              plan_network()          per supply point, ≥ 2
10. Reinforcement             reinforce()             DC power flow, N-1 within a corridor
```

All randomness comes from one `random.Random(seed)`, used in a fixed order. The same
arguments always produce the same plan.

Terms used below:
- **Corridor:** a route between two substations, i.e. an edge of the topology graph.
- **Circuit:** one three-phase system on a corridor. A double-circuit corridor carries two.
- **Stub end:** a substation with a single corridor (degree 1) that is not a supply point.

---

## 1. Substation sites — `clustered_sites()`

The `n` 110 kV substations are placed in a square with side length
`side = spacing × √n × 2`. Here `spacing` is `SPACING_KM_110` (12 km at 110 kV), so the
typical distance between neighbouring substations stays about the same at any grid size.

The placement is a **Thomas cluster process**:

1. **Towns:** create `max(1, round(n / SITES_PER_TOWN))` town centres, placed uniformly at
   least 10 % of the side length from the edge. Each town gets a weight `w ~ Pareto(α = 1.5)`,
   so there are a few large towns and many small ones.
2. **Sites:** for each substation, draw a position:
   - with probability `URBAN_SHARE` (0.6): pick a town in proportion to its weight and
     draw from a normal distribution around its centre, with
     `σ = spacing × TOWN_SIGMA × min(2.5, √(w / mean w))`. The site is marked *urban*.
   - otherwise: a uniform position anywhere in the square, marked *rural*.
3. **Separation:** positions closer than `MIN_SITE_SEPARATION × spacing` to an existing
   site are rejected and redrawn, up to 60 attempts.

The *urban* / *rural* mark is used for load sizes (step 8).

## 2. Supply points — `spread_sites()`

`n_backbone` of the 110 kV sites get a 380 kV voltage level and become **supply points**
(380/110 kV transformer substations). The default is `round(n / 8)`; if that is below 2,
the grid is single-level.

The sites are chosen by **randomised farthest-point sampling**:
- the first site is random;
- each further site is drawn at random from the sites whose distance to the nearest
  chosen site is at least 80 % of the current maximum.

This spreads supply points evenly over the area.

## 3. 380 kV backbone — `build_backbone_mesh()`

The transmission level is deliberately meshed:

1. Euclidean minimum spanning tree over the supply points.
2. For each supply point, its 4 nearest neighbours are candidate corridors. Candidates are
   added shortest first until the average degree reaches `BACKBONE_AVG_DEGREE` (2.8).
3. Any supply point with only one corridor gets a second one to its nearest
   non-neighbour.

Triangles are allowed at this level.

## 4. Grid groups — `supply_groups()`

The 110 kV level is split into separate **grid groups** (Netzgruppen), each fed by one or
two supply points:

1. Take the 380 kV corridors in order of length, shortest first.
2. If neither end is in a group yet, the two supply points form a group.
3. Supply points left over form groups on their own.

Every 110 kV substation then joins the group of its **nearest supply point**, so each
group is the union of one or two nearest-supply-point regions (Voronoi cells).

- Groups are **not connected to each other at 110 kV**; they meet only through the
  380 kV grid.
- In single-level mode there is one group with no supply point.

## 5. 110 kV ring-and-stub grid — `build_regional_grid()`

Each group is built independently, in four phases. Notation:
- `L_med` is the median length of all spanning-tree corridors;
- `d(u, v)` is the straight-line distance between two substations.

### Phase A — spanning tree

For each group, add a Euclidean minimum spanning tree over its substations. Tree
corridors are short, never cross each other, and leave about 30 % of substations as stub
ends.

### Phase B — closing rings

Groups with fewer than `RING_SIZE[0]` (5) substations stay radial. For larger groups,
corridors are added greedily until the group reaches
`target = max(n_g − 1, round(AVG_DEGREE_110 × n_g / 2))` corridors (average degree 2.4),
or until no valid candidate is left.

On each iteration, every pair `(u, v)` in the group that is not yet connected is
evaluated:

1. **Length:** `d(u, v) ≤ RING_LINK_MAX × L_med` (3.0).
2. **Ring size:** `hops(u, v) + 1` must lie within `RING_SIZE` (5–20). `hops` is the
   shortest path in number of corridors, so this is the size of the smallest ring the new
   corridor would create. Using hops, not km, guarantees there are no rings of 3 or 4.
3. **Score:** `score = (path_km(u, v) / d(u, v)) / (d(u, v) / L_med)`.
   - The first factor is the detour ratio: how far power currently has to travel compared
     with the direct distance. A high detour means the new corridor adds real redundancy.
   - The second factor prefers short corridors.
4. **Factors on the score:**
   - ×0.6 if the ring would have fewer than `PREFERRED_MIN_RING` (6) substations;
   - ×1.5 if the current path from `u` to `v` passes a supply point, so the ring runs
     through the feeding substation;
   - ×`STUB_TIP_PENALTY` (0.35) if `u` or `v` is a stub end, so stubs are kept and rings
     form through inner substations.
5. **Geometry:** a candidate is accepted only if the new corridor crosses no existing
   corridor (in any group), and passes no other substation closer than
   `LINE_CLEARANCE × spacing`.

The best candidate is added, distances are recomputed, and the search repeats.

### Phase C — limiting stub length

A substation's **stub depth** is its distance in corridors to the nearest "core"
substation. The core consists of:
- substations on a ring (i.e. touching a corridor that is not a bridge),
- the supply points.

While a stub end deeper than `MAX_STUB_DEPTH` (2) exists, the deepest one is closed into
a ring. The candidate search is the same as in Phase B, but:
- only corridors starting at that stub end are considered;
- rings of 4 or more are allowed;
- corridors may be up to `STUB_LINK_MAX × L_med` (3.5) long.

### Phase D — every stub end needs a load

If there are more stub ends (excluding supply points) than loads (`--loads`), the
deepest stub ends are closed into rings in the same way. If no candidate satisfies the
length limit, the limit is dropped. This phase only runs when `--loads` is below the
number of substations.

### Corridor lengths

The line length of every corridor is `max(2 km, d(u, v) × ROUTING_FACTOR)`, where
`ROUTING_FACTOR` (1.15) accounts for routing around obstacles. This applies to both
levels.

## 6. Line parameters

Each corridor gets impedances from its voltage level's standard conductor (Al/St 265/35:
1 conductor per phase at 110 kV, 2 at 220 kV, 4 at 380 kV), with ±10 % random spread.

Every corridor starts with `CIRCUITS_PER_CORRIDOR` (2) circuits. Each circuit becomes its
own `ACLineSegment` with its own thermal limits, within one CIM `Line` container per
corridor.

## 7. Generators — `place_generators()`

- **Backbone units:** about half of `--gens`, at most one per supply point. They are
  spread over the 380 kV grid by randomised farthest-point sampling on hop distance.
- **Regional units:** the rest, spread the same way over the 110 kV substations. Hop
  distances are measured on the *combined* graph (110 kV + transformers + 380 kV),
  because the 110 kV groups are not connected to each other directly.
- **Sizing:** unit sizes are random within their tier, then scaled so that total dispatch
  (load + 3 % losses) is 50–80 % of the fleet's capacity. The largest backbone unit is
  the slack.

## 8. Loads

By default every 110 kV substation gets one load. Load buses are assigned in this order:
1. stub ends, since a stub must end at a consumer;
2. substations without a generator;
3. generator substations.

The active power is `uniform(LOAD_MW_RANGE) × LOAD_FACTOR[urban|rural]`, where the range
is 5–45 MW and the factor is 1.3 for urban and 0.75 for rural substations. The reactive
power is 0.1–0.4 times the active power.

## 9. Transformers

Each supply point gets 300 MVA 380/110 kV transformers. Every 110 kV substation counts
toward the **nearest supply point of its own group**: its load minus regional generation
is added to that supply point's net demand. The number of units is
`max(2, ceil(|net demand| × 1.4 / 300 MVA))`.

## 10. Reinforcement — `reinforce()`

A DC power flow is run over the planned network. Then, repeatedly, every branch that
exceeds its planning limit gets one more circuit or transformer, up to 4 circuits per
corridor or 8 transformers per site, until no branch exceeds its limit. The limits:

| Units in the branch | Limit on the DC flow |
|---|---|
| 1 | `DC_LOADING_TARGET` (0.7) × rating |
| n ≥ 2 | `N1_CORRIDOR_LOADING` (0.9) × rating × (n − 1) |

The second rule is N-1 within the corridor: after losing one circuit or transformer, the
others still carry the flow at 90 % or less of their rating.

## After planning

Shunt compensation (capacitors for load reactive power, reactors for line charging),
geographic coordinates, the diagram layout and the CIM objects are all derived from the
plan in `build_records()`. They don't change the topology.

---

## Parameters

All parameters are module constants at the top of `synthetic_cim_generator.py`.

| Constant | Value | Meaning |
|---|---|---|
| `SPACING_KM_110` | 12 | Typical distance between neighbouring 110 kV substations (km) |
| `URBAN_SHARE` | 0.6 | Share of substations placed around towns |
| `SITES_PER_TOWN` | 15 | Substations per town centre |
| `TOWN_SIGMA` | 1.6 | Town radius in units of the spacing |
| `MIN_SITE_SEPARATION` | 0.35 | Minimum substation distance in units of the spacing |
| `LOAD_FACTOR` | urban 1.3, rural 0.75 | Load scaling by site type |
| `BUSES_PER_BACKBONE_SITE` | 8 | Default number of supply points = substations / this |
| `BACKBONE_AVG_DEGREE` | 2.8 | 380 kV mesh density |
| `AVG_DEGREE_110` | 2.4 | Target average degree per 110 kV group (corridors) |
| `RING_SIZE` | 5–20 | Allowed ring sizes for ring closing |
| `PREFERRED_MIN_RING` | 6 | Smaller rings get factor 0.6 |
| `RING_LINK_MAX` | 3.0 | Maximum ring-closing corridor length / median tree corridor |
| `STUB_LINK_MAX` | 3.5 | Same, for corridors that close long stubs |
| `MAX_STUB_DEPTH` | 2 | Maximum stub length in substations |
| `STUB_TIP_PENALTY` | 0.35 | Score factor for ring corridors ending at a stub end |
| `LINE_CLEARANCE` | 0.3 | Minimum distance of a new corridor from other substations, in units of the spacing |
| `ROUTING_FACTOR` | 1.15 | Line length / straight-line distance |
| `CIRCUITS_PER_CORRIDOR` | 2 | Circuits per corridor before reinforcement |
| `DC_LOADING_TARGET` | 0.7 | Planning limit for single circuits |
| `N1_CORRIDOR_LOADING` | 0.9 | Planning limit after the loss of one unit in a multi-unit branch |

## Verifying the topology

Every run prints the statistics of the 110 kV corridor graph (`topology_metrics()`):

```
Topology 110 kV: 3 grid group(s), 46 corridors (2+ circuits each), avg degree 2.30, stub ends 18%, rings 9 (size 5-7, median 5), triangles 0, clustering 0.00
```

- **Rings** are the independent cycles of the minimum cycle basis. Their sizes are the
  numbers of substations per ring.
- **Stub ends** excludes supply points: a supply point with a single 110 kV corridor is
  still fed through its transformers.

Typical results for the test set (seeds 1–4) are listed in the
[decision log](topology-decisions.md#validation-results):
- 0 triangles and clustering 0.00 for all grids;
- average degree 2.18–2.40;
- stub ends 10–22 %;
- smallest ring 4–5 substations, median 5–6;
- no crossing 110 kV corridors.

## Complexity and runtime

- Phase B evaluates all pairs in a group and runs a shortest-path search from every
  substation of the group in each iteration. With groups of typically 10–35 substations
  this is fast: planning a 400-substation grid takes about 1.2 s.
- Single-level grids form one large group, so the cost grows roughly with the cube of
  the number of substations. That is still acceptable at a few hundred substations.

## Extension points

- **Open couplings between groups:** would need switching devices (`Breaker`) in the CIM
  model.
- **Groups with three or four supply points:** extend the pairing in `supply_groups()`.
- **Mixed circuit counts:** make `CIRCUITS_PER_CORRIDOR` depend on corridor type or load
  (single-circuit stubs, four-circuit corridors).
- **Calibration:** the parameters above can be fitted to real 110 kV data, for example
  OpenStreetMap power lines, using the same metrics.
