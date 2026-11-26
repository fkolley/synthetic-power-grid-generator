# Synthetic Power Grid Generator

Generates realistic synthetic transmission grids and exports them as **CIM17 RDF/XML**
and **CGMES 2.4.15**, ready for power-flow tools, CIM/CGMES importers and grid viewers.
Every model is checked before it is written out: it is read back, structurally
validated, and solved with an AC power flow. The CGMES export is also imported and
solved with PowSyBl.

Default parameters follow German / European (ENTSO-E) practice: a 380 kV backbone over a
110 kV regional grid at 50 Hz.

A browser-based [Grid Viewer](viewer/README.md) is included for exploring the generated
models on a map or as a single-line diagram.

## Feature highlights

**Two output formats from one model**
- **CIM17** RDF/XML (CIM100 namespace) via [cim-graph](https://github.com/PNNL-CIM-Tools/CIM-Graph),
  with typed SI units (`rdf:datatype="…#UnitSymbol.volt"`).
- **CGMES 2.4.15** zip with six profiles: equipment (EQ), topology (TP), steady-state
  hypothesis (SSH), state variables (SV), geographical location (GL) and diagram layout (DL).
  Uses `rdf:ID` identifiers, kV/MW units and full `md:FullModel` headers.
- CGMES attribute names are taken from the CIM16 schema rather than hard-coded.

**Realistic topology** ([details](docs/topology.md), [decisions](docs/topology-decisions.md))
- Two voltage levels: a meshed 380 kV backbone over a 110 kV regional grid, coupled by
  300 MVA 380/110 kV transformers (at least two per site, so one can fail).
- The 110 kV level is built the way German grids are: separate grid groups (Netzgruppen),
  each fed by one or two 380/110 kV substations, made of **rings and short stubs**
  (Stichleitungen). There are no triangles, no crossing lines, and rings run through the
  feeding substations.
- Substations cluster around towns with a sparser rural background. Every 110 kV substation
  supplies load, and urban substations supply more.
- All lines are double-circuit, as is usual on German 110 and 380 kV overhead lines.
- Every run prints topology statistics: average degree, share of stub ends, ring sizes,
  triangles and clustering.

**A grid that actually solves**
- Generation is matched to load plus losses. Large units sit on the backbone and smaller
  ones on 110 kV, and one unit is the slack. Generators have coordinated voltage setpoints,
  P/Q limits and voltage control.
- Shunt capacitors and reactors balance the reactive power of each bus.
- Network reinforcement: a DC power flow adds circuits or transformers until each
  double-circuit corridor survives the loss of one circuit (N-1 within the corridor).

**German / European defaults**
- 50 Hz; Al/St 265/35 overhead lines (1 / 2 / 4 conductors per phase at 110 / 220 / 380 kV)
  with realistic impedances and thermal ratings.
- Operating voltage ranges from the ENTSO-E System Operation Guideline. Substations are
  placed around central Germany.

**Complete CIM content**
- A full container hierarchy: region → substation → voltage level → equipment.
- Thermal limits on every line and transformer terminal.
- Topology, plus the solved power flow (bus voltages, branch flows) in the SV profile.
- Geographic coordinates (WGS84) and a schematic diagram layout, so viewers can draw the grid.

**Verified output**
- The CIM17 file is read back with cim-graph and its object counts compared with what
  was written.
- Model checks: connectivity, wiring, containers, limits, a single slack and generator
  dispatch within limits.
- AC power flow with pandapower: convergence, voltages within limits, no overloaded branches.
- N-1 contingency report: every line and transformer is taken out in turn and the AC power
  flow re-solved, listing overloads, voltage violations and islanded buses. Informational
  only (it does not fail the run); skip it with `--skip-n1`.
- The CGMES zip is imported into PowSyBl, the reference open-source CGMES importer, and
  solved with its own AC load flow. The resulting voltages are compared with the SV profile.
- The script exits with a non-zero status if any check fails.

**Reproducible**
- The same arguments always produce the same model and the same mRIDs.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python synthetic_cim_generator.py
```

This writes `synthetic_grid.cim.xml` (CIM17) and `synthetic_grid_CGMES.zip` (CGMES) and
prints the verification report:

```
Generating synthetic grid: 40 buses, 8 gens, 40 loads @ 380/110 kV, 5 backbone buses, 50 Hz (seed=42)
Topology 110 kV: 3 grid group(s), 46 corridors (2+ circuits each), avg degree 2.30, stub ends 18%, rings 9 (size 5-7, median 5), triangles 0, clustering 0.00
Wrote CIM17 RDF/XML → synthetic_grid.cim.xml (1836902 bytes)
Verification (CIM17):
  Round-trip: OK (2771 objects read back, counts match)
  Model checks: OK (connected, wiring, containers, limits, 1 slack, topology, locations, diagram)
  Balance: generation 1136.9 MW, load 1103.8 MW
  AC power flow: converged; slack P=293.9 MW, losses=10.75 MW
    110 kV: V=[1.016, 1.037] pu (111.8-114.1 kV)
    380 kV: V=[1.034, 1.038] pu (393.0-394.4 kV)
    lines: max loading 48 %
    transformers: max loading 37 %
  N-1 contingencies: 121 of 121 outages without violations
Wrote CGMES 2.4.15 → synthetic_grid_CGMES.zip (180090 bytes): EQ, TP, SSH, SV, GL, DL
Verification (CGMES):
  PowSyBl import: OK (45 TopologicalNode, 109 ACLineSegment, 12 PowerTransformer, 8 SynchronousMachine, 40 EnergyConsumer, 38 LinearShuntCompensator)
  PowSyBl GL: 40 substation positions imported
  PowSyBl AC load flow: converged; max |V| deviation from SV 0.00 % over 45/45 buses
Done.
```

## Usage

```bash
# Larger grid with a different seed
python synthetic_cim_generator.py --buses 200 --gens 30 --seed 7

# Single 110 kV level, CIM17 only
python synthetic_cim_generator.py --buses 12 --gens 3 --loads 8 --backbone-buses 0 --format cim17

# 220/110 kV instead of 380/110 kV
python synthetic_cim_generator.py --backbone-voltage 220
```

| Option | Default | Description |
|---|---|---|
| `--buses` | 40 | Number of 110 kV buses (one substation each) |
| `--gens` | 8 | Number of generators (about half on the backbone) |
| `--loads` | one per substation | Number of loads at 110 kV substations; stub ends always get one |
| `--voltage` | 110 | Lower-level nominal voltage in kV |
| `--backbone-voltage` | 380 | Backbone nominal voltage in kV |
| `--backbone-buses` | buses / 8 | Substations with a backbone bus; `0` for a single-level grid |
| `--seed` | 42 | Random seed |
| `--skip-n1` | off | Skip the N-1 contingency report |
| `--format` | `both` | `cim17`, `cgmes` or `both` |
| `--output` | `synthetic_grid.cim.xml` | CIM17 output path |
| `--cgmes-output` | `<output stem>_CGMES.zip` | CGMES output path |

Grids from a few buses up to several hundred substations have been tested. Topology
planning takes about a second even for 400 substations. Most of the runtime is
verification, and the N-1 report is the largest part of it: a 400-substation grid takes
about 3.3 minutes with both output formats, or about 35 seconds with `--skip-n1`.

## N-1 contingency analysis

The CIM17 verification ends with an N-1 report. It shows how the generated grid copes
when any single line circuit or transformer fails.

**How it works**
- The analysis runs on the CIM17 file as read back from disk, so it tests exactly what
  was written.
- Each line circuit (`ACLineSegment`) and each transformer is taken out of service in turn,
  and the AC power flow (pandapower) is solved again. Parallel circuits and parallel
  transformers count as separate outages.
- Generator dispatch and voltage setpoints stay as in the base case; the slack covers
  any change in losses. There is no redispatch or other remedial action.
- Each outage is checked for:
  - a power flow that does not converge,
  - islanded buses: part of the grid is cut off, and its load is not supplied,
  - bus voltages outside the voltage level's limits,
  - lines or transformers above 100 % of their thermal rating (`PATL`).

**Reading the report**

The default grid has no violations. This example is from a single-level grid
(`--buses 40 --backbone-buses 0 --seed 1`):
```
  N-1 contingencies: 106 of 108 outages without violations
    line Line_110kV_023_035_C1 out: 1 line(s) overloaded (max 111 %)
    line Line_110kV_023_035_C2 out: 1 line(s) overloaded (max 111 %)
```
The first line counts the outages with no violations. Each following line names the
element taken out and what went wrong. Up to five outages are listed; the rest are
summarised as "… and N more".

**What it means for the generated grids**
- The report is informational: violations do not change the exit status. It is skipped
  with `--skip-n1`, and when the base case already fails its power-flow check
  (no convergence, a voltage outside its limits, or an overload).
- The generator plans for N-1 but does not guarantee it. Its N-1 design rules:
  - every corridor is double-circuit, and reinforcement makes sure one circuit can carry
    the corridor's flow alone;
  - every supply point has at least two 380/110 kV transformers;
  - substations are either on a ring or on a short stub, and stubs are double-circuit.
- Violations can still appear, because the planning uses a DC power flow and doesn't
  look at how flows shift between corridors after an outage.
- Typical results: 121 of 121 outages without violations for the default grid, and
  1196 of 1196 for a 400-substation grid. Across the test set of 48 grids, only two
  showed violations (106 of 108 and 299 of 303).
- The analysis covers the CIM17 model only. With `--format cgmes` there is no N-1 report.

## Viewing the grid

```bash
python -m http.server 8000
# open http://localhost:8000/viewer/
```

The [Grid Viewer](viewer/README.md) runs in the browser and loads both output formats.
It has two views:
- a **geographic view** on a map, using the GL coordinates;
- a structured **single-line diagram** in the style of EMS/SCADA overview displays,
  generated from the topology.

Both views offer hover details, search, colouring by voltage level, loading or bus voltage,
and animated power flows.

## Requirements

- Python 3.10+
- `cim-graph`, `networkx`, `numpy` (required)
- `pandapower` (optional): AC power flow, used for verification and the SV profile
- `pypowsybl` (optional): CGMES import check; needs a Java-capable platform

All are listed in [requirements.txt](requirements.txt). Without the optional packages the
generator still writes both formats; the CGMES export then has no SV profile, and the
corresponding checks are skipped.

## Project structure

| Path | Contents |
|---|---|
| `synthetic_cim_generator.py` | Parameters, network planning, CIM object construction, command line |
| `cim_records.py` | Format-neutral CIM records, CIM17 writer (cim-graph), CGMES 2.4.15 writer |
| `grid_verification.py` | Model checks, pandapower power flow, PowSyBl CGMES import check |
| `viewer/` | Browser-based grid viewer (map and single-line diagram) |
| `docs/topology.md` | Technical description of the topology generation |
| `docs/topology-decisions.md` | Decision log for the topology design, with reasoning and validation |

## Conventions

- **Units:** CIM17 uses base SI units (V, W, VAr, Ω, S, m) with explicit datatypes.
  CGMES uses kV, MW, MVAr, Ω, S and km.
- **Sign convention:** load sign convention (positive means power flowing out of the bus)
  for `RotatingMachine.p/q`, `EnergyConsumer.p/q` and `SvPowerFlow`, so generation is
  negative. Generator P and Q limits use the generator convention.
- **Transformers:** the series impedance is referred to the high-voltage end (end 1).
- **Slack:** the largest backbone machine has `referencePriority = 1`.

## Limitations

- The 110 kV voltage depends on the 380 kV level, because the transformers have no
  on-load tap changers.
- N-1 security is planned for but not guaranteed; the N-1 report can show violations.
- Topology parameters (group sizes, stub share, town clustering) are informed estimates,
  not yet calibrated against real 110 kV data; see the
  [decision log](docs/topology-decisions.md).
- Grid groups are not linked at 110 kV. Real grids often have normally-open couplings
  between groups, which would need switching devices in the model.
- There are no loads or generators at 220 kV, no HVDC, and no renewable generation profiles.
- The output has not been checked against the official ENTSO-E CGMES conformity rules.
  It is validated by importing it into PowSyBl.

## License

Licensed under the [Business Source License 1.1](LICENSE) (BSL 1.1). This is not an
open-source licence:

- You may copy, modify and redistribute the code, and use it for non-production purposes
  (development, testing, evaluation).
- Production use requires a commercial licence from the Licensor (see [LICENSE](LICENSE)).
  There is no Additional Use Grant.
- On 2030-11-26, or four years after a version's first public release if that comes first,
  the code converts to the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0).
