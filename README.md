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

**A grid that actually solves**
- Two voltage levels: a meshed 110 kV regional grid and a 380 kV backbone, coupled by
  300 MVA 380/110 kV transformers (at least two per site, so one can fail).
- Geographic topology: substations at random sites, each fed from at least two directions.
  Lines run to nearby substations and their length follows the distance.
- Generation is matched to load plus losses. Large units sit on the backbone and smaller
  ones on 110 kV, and one unit is the slack. Generators have coordinated voltage setpoints,
  P/Q limits and voltage control.
- Shunt capacitors and reactors balance the reactive power of each bus.
- Network reinforcement: a DC power flow adds parallel circuits or transformers until no
  branch is above 70 % of its thermal rating.

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
Generating synthetic grid: 40 buses, 8 gens, 30 loads @ 380/110 kV, 5 backbone buses, 50 Hz (seed=42)
Wrote CIM17 RDF/XML → synthetic_grid.cim.xml (1388655 bytes)
Verification (CIM17):
  Round-trip: OK (2103 objects read back, counts match)
  Model checks: OK (connected, wiring, containers, limits, 1 slack, topology, locations, diagram)
  Balance: generation 844.5 MW, load 819.9 MW
  AC power flow: converged; slack P=209.2 MW, losses=9.18 MW
    110 kV: V=[1.016, 1.039] pu (111.7-114.3 kV)
    380 kV: V=[1.034, 1.041] pu (392.9-395.6 kV)
    lines: max loading 68 %
    transformers: max loading 34 %
  N-1 contingencies: 77 of 80 outages without violations
    line Line_110kV_019_038_C1 out: 1 line(s) overloaded (max 119 %)
    line Line_110kV_002_027_C1 out: 1 line(s) overloaded (max 122 %)
    line Line_110kV_007_027_C1 out: 3 bus(es) islanded
Wrote CGMES 2.4.15 → synthetic_grid_CGMES.zip (137895 bytes): EQ, TP, SSH, SV, GL, DL
Verification (CGMES):
  PowSyBl import: OK (45 TopologicalNode, 70 ACLineSegment, 10 PowerTransformer, 8 SynchronousMachine, 30 EnergyConsumer, 34 LinearShuntCompensator)
  PowSyBl GL: 40 substation positions imported
  PowSyBl AC load flow: converged; max |V| deviation from SV 0.01 % over 45/45 buses
Done.
```

## Usage

```bash
# Larger grid with a different seed
python synthetic_cim_generator.py --buses 200 --gens 30 --loads 150 --seed 7

# Single 110 kV level, CIM17 only
python synthetic_cim_generator.py --buses 12 --gens 3 --loads 8 --backbone-buses 0 --format cim17

# 220/110 kV instead of 380/110 kV
python synthetic_cim_generator.py --backbone-voltage 220
```

| Option | Default | Description |
|---|---|---|
| `--buses` | 40 | Number of 110 kV buses (one substation each) |
| `--gens` | 8 | Number of generators (about half on the backbone) |
| `--loads` | 30 | Number of loads (at 110 kV buses) |
| `--voltage` | 110 | Lower-level nominal voltage in kV |
| `--backbone-voltage` | 380 | Backbone nominal voltage in kV |
| `--backbone-buses` | buses / 8 | Substations with a backbone bus; `0` for a single-level grid |
| `--seed` | 42 | Random seed |
| `--skip-n1` | off | Skip the N-1 contingency report |
| `--format` | `both` | `cim17`, `cgmes` or `both` |
| `--output` | `synthetic_grid.cim.xml` | CIM17 output path |
| `--cgmes-output` | `<output stem>_CGMES.zip` | CGMES output path |

Grids from a few buses up to several hundred substations have been tested. Most of the
runtime is verification, and the N-1 report is the largest part of it. With
`--format cim17`, a 400-substation grid takes about 2 minutes, or about 25 seconds with
`--skip-n1`.

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
```
  N-1 contingencies: 77 of 80 outages without violations
    line Line_110kV_019_038_C1 out: 1 line(s) overloaded (max 119 %)
    line Line_110kV_002_027_C1 out: 1 line(s) overloaded (max 122 %)
    line Line_110kV_007_027_C1 out: 3 bus(es) islanded
```
The first line counts the outages with no violations. Each following line names the
element taken out and what went wrong. Up to five outages are listed; the rest are
summarised as "… and N more".

**What it means for the generated grids**
- The report is informational: violations do not change the exit status. It is skipped
  with `--skip-n1`, and when the base case already fails its power-flow check
  (no convergence, a voltage outside its limits, or an overload).
- The generator does not make grids N-1 secure. It does apply two N-1-minded design
  rules: every backbone site has at least two 380/110 kV transformers, and every
  substation is connected by at least two lines.
- Even so, outages can island buses. A group of substations can be well meshed inside
  but attached to the rest of the grid by a single line; losing that line cuts the
  whole group off.
- Overloads appear because reinforcement only considers the intact network.
- Typical results: 77 of 80 outages without violations for the default grid, 395 of 401
  for 200 substations, and 782 of 798 for 400 substations.
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
- The grid is not made N-1 secure: the reinforcement only targets the intact network, so the
  N-1 report can show overloads and islanded buses.
- There are no loads or generators at 220 kV, no HVDC, and no renewable generation profiles.
- The output has not been checked against the official ENTSO-E CGMES conformity rules.
  It is validated by importing it into PowSyBl.
