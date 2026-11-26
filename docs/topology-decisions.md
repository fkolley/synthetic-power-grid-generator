# Decision log: realistic grid topology

This log records the decisions behind the topology generator introduced on 2026-11-26.
It replaces the earlier nearest-neighbour meshing of the 110 kV grid. The technical
description of the resulting algorithm is in [topology.md](topology.md).

**Goal:** sample grids that resemble the German / Central European system as closely as
practical.

**Trigger:** a power-system engineer's review of the previous output:

> "Mit den Dreiecken hast du recht, das sieht nicht realistisch aus. Die Standorte werden
> zufällig verteilt und mit den nächsten Nachbarn vermascht, daher die Dreiecke. Reale
> 110-kV-Netze haben eher Ringe und Stichleitungen."

("You're right about the triangles; that doesn't look realistic. The sites are placed at
random and meshed with their nearest neighbours, hence the triangles. Real 110 kV grids
tend to have rings and stubs.")

**Status of evidence:** the decisions rest on general engineering knowledge of German
transmission and sub-transmission practice. Some parameter values are informed estimates,
not values fitted to a real dataset; they are marked "estimate" below. They are collected
as constants at the top of `synthetic_cim_generator.py`, so they can be calibrated later,
for example against OpenStreetMap 110 kV data or published grid maps such as the Netze BW
110 kV map.

## Baseline: the replaced algorithm

The previous 110 kV generator placed substations uniformly at random, built a Euclidean
minimum spanning tree, added the shortest links between nearby neighbours up to an
average degree of 2.8, and finally connected every substation with only one link to its
nearest neighbour.

| Metric (110 kV corridor graph) | 40 substations | 200 substations | 400 substations |
|---|---|---|---|
| Average degree | 3.10 | 3.04 | 3.08 |
| Stub ends (degree 1) | 0 % | 0 % | 0 % |
| Triangles | 28 | 108 | 242 |
| Average clustering coefficient | 0.46 | 0.46 | 0.45 |
| Share of independent cycles that are triangles | 91 % | 90 % | 90 % |

Real power grids have clustering coefficients of roughly 0.05–0.1. Shortest-first linking
of nearby neighbours closes triangles almost by construction, and the "no single link"
rule made the problem worse.

---

## D1 — Split the 110 kV level into separate grid groups (Netzgruppen)

**Decision:** the 110 kV grid is split into groups. Each group is fed by one or two
380/110 kV supply points. Groups are not connected to each other at 110 kV; they are
coupled only through the 380 kV grid.

**Options considered:**
1. One connected 110 kV grid for the whole area, as before.
2. Separate grid groups around the supply points (chosen).
3. Separate groups plus normally-open coupling lines between them.

**Reasoning:**
- German 110 kV networks are operated in grid groups fed by a small number of
  transformer substations from the transmission grid. They are not operated as one large
  mesh in parallel with the 380 kV grid. Separate operation limits short-circuit currents
  and avoids loop flows through 110 kV.
- The previous single mesh produced exactly such parallel flows, which partly drove the
  reinforcement.
- Option 3 is more faithful, but the model has no switching devices (`Breaker`,
  `Disconnector`). Modelling open coupling points would mean adding node-breaker detail
  for very little visible benefit, so it is deferred.

**Consequences:**
- Each 110 kV substation belongs to the group of its nearest supply point.
- Supply points are paired along 380 kV corridors, shortest pairs first, so a group has
  one or two supply points. Two is the typical "Doppeleinspeisung" (double infeed).
  Allowing three or four is a possible extension.
- Group sizes come out at about 3–36 substations, median 13–16 (estimate; there is no
  reference distribution).
- In single-level mode (`--backbone-buses 0`) there are no supply points, so the whole
  grid is one group.
- Generator placement must measure hop distances across the 380 kV grid, because the
  110 kV graph is no longer connected on its own.

## D2 — Ring-and-stub structure within each group

**Decision:** within each group, build a Euclidean minimum spanning tree, then close
rings with selected ring-closing corridors. What remains outside the rings are stubs
(Stichleitungen).

**Reasoning:**
- The spanning tree gives short, geographically plausible corridors and a realistic
  starting share of stub ends (about 30 %).
- Each ring-closing corridor creates exactly one new ring. That makes ring sizes directly
  controllable, unlike adding edges by length.

**Ring-closing rules:**

| Rule | Value | Reason |
|---|---|---|
| Ring size (substations) | 5–20; 5 penalised (factor 0.6), 6+ preferred | The reviewer asked for cycles of at least 5–6 and no triangles; rings of 6–20 substations are typical. |
| Ring size measure | hop distance + 1 | The *smallest* ring created must meet the minimum. Measuring along the km-shortest path let triangles through; this was a bug found in testing. |
| Detour ratio | route length through the grid ÷ straight-line distance; high is better | Selects links that add real redundancy rather than shortcuts. This is a standard criterion in the synthetic-grid literature, e.g. Birchfield et al. |
| Length | at most 3.0 × the median tree corridor; shorter preferred | Geographically plausible, but allows the occasional longer ring link. |
| No crossings | a new corridor may not cross an existing one | Crossing rings look as unrealistic on a map as triangles. |
| Clearance | a new corridor may not pass within 0.3 × the site spacing of another substation | A line passing a substation without connecting to it is implausible. |
| Supply-point bonus | ×1.5 if the ring includes a supply point | Real rings start and end at the feeding transformer substation. |
| Stub-tip penalty | ×0.35 if the corridor ends at a stub end | Without it, ring closing consumed about half of the stub ends. Stubs are a characteristic feature the reviewer explicitly asked for. |
| Stop criterion | average degree 2.4 within the group, or no valid candidate left | See D4. |

**Selection:** greedy. The best-scoring candidate is added, all distances are
recomputed, and the search repeats. Score = detour ratio ÷ (length ÷ median tree
corridor), multiplied by the factors above.

## D3 — Short stubs, double-circuit

**Decision:**
- A stub may hang at most **2 substations** off a ring or supply point. Longer branches
  are closed into a ring from their tip. This repair step allows rings of 4 or more.
- Stubs, like all corridors, are double-circuit (see D5).

**Reasoning:**
- Real stubs typically feed one or two substations. Long radial chains are rare at 110 kV
  because they cannot be supplied after a fault.
- A double-circuit stub keeps supplying its end after the loss of one circuit. That
  matches the usual German practice of feeding single substations through a
  double-circuit stub ("Doppelstich"), and keeps the N-1 report clean for single-circuit
  outages.

## D4 — Average-degree target: 2.4 on the corridor graph

**Decision:**
- The target average degree is 2.4 for the 110 kV grid, counted on **corridors**
  (routes), not circuits. The accepted range is about 2.2–2.6.
- The original request of 2.4–3.6 was not adopted.

**Reasoning:**
- A tree has an average degree of about 2.0, and every added corridor raises it by 2/n.
- A planar graph without cycles shorter than 5 has at most 5/3·(n−2) edges, so its average
  degree is below 3.33. Without cycles shorter than 6 the bound is 3.0.
- With 15–25 % stub ends, a ring-and-stub network realistically lands at about 2.2–2.6.
  Degrees near 3 or higher require dense meshing, which is the problem being removed.
- Counting circuits instead of corridors would double the figure for double-circuit lines
  and say nothing about topology.

**Result:**
- With supply points, groups are small (median about 13–16 substations), so valid ring
  candidates run out before 2.4 is reached. Averages come out at 2.18–2.30.
- In single-level mode the target of 2.33–2.40 is reached.
- Both are inside the accepted range.

## D5 — All corridors are double-circuit; reinforcement is N-1 aware within a corridor

**Decision:**
- Every 110 kV and 380 kV corridor starts with **2 circuits**; reinforcement may raise
  this to 4.
- Reinforcement rule:
  - single circuits: DC flow at most 70 % of the rating, as before;
  - corridors or transformer groups with n ≥ 2 units: the flow must fit into n−1 units
    at at most 90 % of their rating.

**Reasoning:**
- German 110 kV and 380 kV overhead lines are usually built with two three-phase systems
  per tower.
- Planning practice is N-1. Within a double-circuit corridor, that means one circuit must
  be able to carry the corridor flow alone.

**Consequences:**
- Line charging doubles. The existing per-bus reactor sizing compensates it
  automatically.
- Corridor impedance halves, so voltage drops are smaller.
- The N-1 report now covers twice as many outages, so it takes longer: 400 substations
  went from about 1.5 to about 2.7 minutes of N-1 analysis.

## D6 — A load at every 110 kV substation; stub ends always have a load

**Decision:**
- `--loads` now defaults to the number of 110 kV substations; it was 30.
- Loads go to stub ends first, then to substations without a generator, then to generator
  substations.
- If `--loads` is set below the number of stub ends, the deepest stub ends are closed
  into rings until every stub end can have a load.

**Reasoning:**
- A 110 kV substation exists to supply medium voltage (via 110/20 kV transformers), so
  practically every one has load.
- A stub end without load or generation is meaningless, and PowSyBl treats such a bus as
  invalid. This was the reason for the previous "no single link" rule, which created
  triangles.

**Consequence:** total load rises for the same `--buses`. For the defaults it went from
820 MW to about 1,100 MW.

## D7 — Clustered substation placement

**Decision:**
- Sites come from a Thomas cluster process: 60 % of substations are scattered around town
  centres, the rest uniformly over the area (rural background).
- There is one town per 15 substations. Town weights are Pareto-distributed (α = 1.5,
  a few large towns, many small ones), and the cluster radius grows with the town weight.
- Substations are at least 0.35 × the site spacing apart.
- Urban substations carry 1.3× the base load, rural ones 0.75×.

**Reasoning:**
- Real substation density follows load density: dense in and around towns, sparse in the
  countryside. This affects ring sizes and stub lengths: short urban rings, long rural
  stubs.
- Uniform random sites under-represent both effects.

All values here are estimates.

## D8 — The 380 kV backbone stays meshed

**Decision:** the 380 kV grid keeps the meshed construction: a spanning tree, plus the
shortest neighbour links up to an average degree of 2.8, plus at least two corridors per
site. Triangles are acceptable at this level.

**Reasoning:** the transmission grid is genuinely meshed; meshing is how it achieves
N-1 at that level. The review concerned the 110 kV level.

## D9 — Topology metrics are reported and used as acceptance criteria

**Decision:**
- Every run prints the 110 kV topology statistics: number of groups, corridors, average
  degree, share of stub ends, number of rings and their size range, triangles, and
  clustering.
- The acceptance criteria used during development:

| Criterion | Target |
|---|---|
| Triangles in the 110 kV corridor graph | 0 |
| Average clustering coefficient | < 0.05 |
| Average degree (corridors) | 2.2–2.6 |
| Stub ends (excluding supply points) | about 15–25 % |
| Rings (minimum cycle basis) | sizes ≥ 4, median 5–6 |
| Crossing 110 kV corridors | 0 |
| AC power flow incl. voltage limits and loading | passes for all test grids |
| N-1 report | no regression against the previous generator |

## D10 — The old algorithm is removed, with no compatibility option

**Decision:** the nearest-neighbour meshing of the 110 kV grid is removed, and no
`--topology mesh` option is offered.

**Reasoning:** the old topology is known to be unrealistic, and keeping it adds code and
test burden. Its metrics are preserved in this log for comparison.

The same seed still produces the same model, but models differ from those of earlier
versions.

---

## Validation results

The validation used 48 grids: 6 configurations, each with seeds 1–8. Seeds 1–4 were used
while tuning the parameters; seeds 5–8 were held out and checked for power-flow pass/fail
and N-1. The metric ranges below come from seeds 1–4. N-1 was computed for all grids of
up to 100 substations, plus one 400-substation grid (seed 2). The configurations:
- 40 substations, single-level and with a backbone,
- 30 substations, single-level, with 20 loads,
- 100, 200 and 400 substations with a backbone.

| Metric | Before | After |
|---|---|---|
| Triangles (110 kV) | 28–242 | 0 |
| Clustering | 0.45–0.46 | 0.00 |
| Average degree | 3.04–3.10 | 2.18–2.40 |
| Stub ends | 0 % | 10–22 % (typically about 18 %) |
| Ring sizes (min / median / max) | 3 / 3 / … | 4–5 / 5–6 / 6–12 |
| Crossing 110 kV corridors | not checked | 0 |
| AC power flow passes | 39/39 (previous test set) | 48/48 |
| N-1 outages without violations, default grid | 77 of 80 | 121 of 121 |
| N-1 outages without violations, 400 substations | 782 of 798 | 1196 of 1196 |
| N-1 cases with violations, whole test set | — | 2 grids: 106 of 108, 299 of 303 |
| N-1 analysis time, 400 substations | about 1.5 min | about 2.7 min (twice the outages) |
| Topology planning, 400 substations | not measured | about 1.2 s |

## Open points

- **Open coupling points between grid groups:** needs switching devices in the model (D1).
- **Groups with three or four supply points:** currently at most two.
- **Calibration:** group sizes, stub share and the town process are estimates, to be
  checked against real 110 kV data (e.g. OpenStreetMap power lines in Baden-Württemberg).
- **Mixed circuit counts:** single-circuit 110 kV lines and four-circuit lines (110 kV
  sharing towers with 380 kV) also exist; currently every corridor starts with two
  circuits.
