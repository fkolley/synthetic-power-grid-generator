"""
Verification of generated grids:

* check_model / run_power_flow: structural checks and an AC power flow (pandapower)
  on a cim-graph CIM17 model (in memory or read back from file).
* solve_state: the same power flow, returning bus voltages and terminal flows for
  the SV profile.
* verify_cim17: read the CIM17 file back and run all checks.
* verify_cgmes: import the CGMES zip with PowSyBl (pypowsybl), the reference
  open-source CGMES importer, and compare it with the generated model.
"""

import math
import warnings
from typing import Dict, List, Optional, Tuple

import networkx as nx

from cim_records import cim  # also selects the cim-graph profile before cimgraph is used
from cimprofiles.units import CIMUnit
from cimgraph.databases import XMLFile
from cimgraph.models import GraphModel, NodeBreakerModel


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
        if cn.TopologicalNode is None:
            problems.append(f"{cn.name}: no TopologicalNode")
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

    located = {psr.identifier for loc in _objs(model, cim.Location) for psr in loc.PowerSystemResources}
    missing = [s.name for s in _objs(model, cim.Substation) if s.identifier not in located]
    if missing:
        problems.append(f"{len(missing)} substation(s) without Location, e.g. {missing[0]}")
    drawn = {do.IdentifiedObject.identifier for do in _objs(model, cim.DiagramObject) if do.IdentifiedObject}
    undrawn = [cn.name for cn in _objs(model, cim.ConnectivityNode) if cn.identifier not in drawn]
    if undrawn:
        problems.append(f"{len(undrawn)} bus(es) without DiagramObject, e.g. {undrawn[0]}")
    return problems


def _import_pandapower():
    try:
        warnings.filterwarnings("ignore")
        import pandapower as pp
        return pp
    except ImportError:
        return None


def build_pandapower(model: GraphModel, frequency_hz: float):
    """Convert a CIM17 cim-graph model to a pandapower network. Returns (net, maps)
    where maps relate pandapower elements back to CIM terminal / node identifiers."""
    pp = _import_pandapower()
    net = pp.create_empty_network(f_hz=frequency_hz)
    maps: Dict = {"bus": {}, "vlim": {}, "line": {}, "trafo": {}, "load": {}, "gen": {}, "shunt": {}}
    for cn in _objs(model, cim.ConnectivityNode):
        vl = cn.ConnectivityNodeContainer
        vn_kv = vl.BaseVoltage.nominalVoltage.to("kV")
        b = pp.create_bus(net, vn_kv=vn_kv, name=cn.name)
        maps["bus"][cn.identifier] = b
        maps["vlim"][b] = (vl.lowVoltageLimit.to("kV") / vn_kv, vl.highVoltageLimit.to("kV") / vn_kv)

    terms_by_eq = _terminals_by_equipment(model)
    limits = _current_limits(model)

    def bus(eq, i=0):
        return maps["bus"][terms_by_eq[eq.identifier][i].ConnectivityNode.identifier]

    for seg in _objs(model, cim.ACLineSegment):
        km = seg.length.to("km")
        ts = terms_by_eq[seg.identifier]
        i_max = min(limits.get(t.identifier, math.inf) for t in ts)
        idx = pp.create_line_from_parameters(
            net, bus(seg, 0), bus(seg, 1), length_km=km, name=seg.name,
            r_ohm_per_km=float(seg.r) / km, x_ohm_per_km=float(seg.x) / km,
            c_nf_per_km=float(seg.bch) / (2 * math.pi * net.f_hz) / km * 1e9,
            max_i_ka=i_max / 1000 if math.isfinite(i_max) else 99.0)
        maps["line"][idx] = (ts[0].identifier, ts[1].identifier)
    ends_by_pt = _transformer_ends(model)
    for pt in _objs(model, cim.PowerTransformer):
        hv, lv = ends_by_pt[pt.identifier]
        s = hv.ratedS.to("MVA")
        u_hv, u_lv = hv.ratedU.to("kV"), lv.ratedU.to("kV")
        z_base = u_hv**2 / s
        r = float(hv.r) + float(lv.r) * (u_hv / u_lv) ** 2
        x = float(hv.x) + float(lv.x) * (u_hv / u_lv) ** 2
        idx = pp.create_transformer_from_parameters(
            net, maps["bus"][hv.Terminal.ConnectivityNode.identifier],
            maps["bus"][lv.Terminal.ConnectivityNode.identifier],
            sn_mva=s, vn_hv_kv=u_hv, vn_lv_kv=u_lv,
            vkr_percent=r / z_base * 100, vk_percent=math.hypot(r, x) / z_base * 100,
            pfe_kw=0.0, i0_percent=0.0, name=pt.name)
        maps["trafo"][idx] = (hv.Terminal.identifier, lv.Terminal.identifier)
    for load in _objs(model, cim.EnergyConsumer):
        idx = pp.create_load(net, bus(load), p_mw=load.p.to("MW"), q_mvar=load.q.to("MVAr"), name=load.name)
        maps["load"][idx] = terms_by_eq[load.identifier][0].identifier
    for sh in _objs(model, cim.LinearShuntCompensator):
        vn = sh.nomU.to("kV")
        # pandapower: q_mvar > 0 absorbs; CIM: b > 0 is capacitive (injects)
        idx = pp.create_shunt(net, bus(sh), q_mvar=-float(sh.bPerSection) * vn**2,
                              p_mw=float(sh.gPerSection) * vn**2, vn_kv=vn, step=int(sh.sections),
                              max_step=sh.maximumSections, name=sh.name)
        maps["shunt"][idx] = terms_by_eq[sh.identifier][0].identifier
    for g in _objs(model, cim.SynchronousMachine):
        b = bus(g)
        idx = pp.create_gen(net, b, p_mw=-g.p.to("MW"), name=g.name,
                            vm_pu=g.RegulatingControl.targetValue / net.bus.vn_kv.at[b],
                            min_q_mvar=g.minQ.to("MVAr"), max_q_mvar=g.maxQ.to("MVAr"),
                            slack=(g.referencePriority or 0) > 0)
        maps["gen"][idx] = terms_by_eq[g.identifier][0].identifier
    return net, maps


def _runpp(net) -> Optional[str]:
    pp = _import_pandapower()
    try:
        pp.runpp(net, algorithm="nr", enforce_q_lims=True, numba=False)
        return None
    except Exception as e:  # pandapower raises LoadflowNotConverged
        return f"did not converge ({type(e).__name__})"


def solve_state(model: GraphModel, frequency_hz: float) -> Optional[Dict]:
    """Solve the AC power flow for the SV profile. Returns None if pandapower is
    missing or the power flow does not converge, else
    {"bus": {cn_id: (v_kv, angle_deg)}, "terminal": {terminal_id: (p_mw, q_mvar)}}
    with flows in load sign convention (positive = out of the bus into the equipment)."""
    if _import_pandapower() is None:
        return None
    net, maps = build_pandapower(model, frequency_hz)
    if _runpp(net):
        return None
    rb = net.res_bus
    state: Dict = {"bus": {}, "terminal": {}}
    for cn_id, b in maps["bus"].items():
        state["bus"][str(cn_id)] = (rb.vm_pu.at[b] * net.bus.vn_kv.at[b], rb.va_degree.at[b])
    t = state["terminal"]
    for i, (t1, t2) in maps["line"].items():
        r = net.res_line.loc[i]
        t[str(t1)] = (r.p_from_mw, r.q_from_mvar)
        t[str(t2)] = (r.p_to_mw, r.q_to_mvar)
    for i, (t1, t2) in maps["trafo"].items():
        r = net.res_trafo.loc[i]
        t[str(t1)] = (r.p_hv_mw, r.q_hv_mvar)
        t[str(t2)] = (r.p_lv_mw, r.q_lv_mvar)
    for i, tid in maps["load"].items():
        t[str(tid)] = (net.res_load.p_mw.at[i], net.res_load.q_mvar.at[i])
    for i, tid in maps["shunt"].items():
        t[str(tid)] = (net.res_shunt.p_mw.at[i], net.res_shunt.q_mvar.at[i])
    for i, tid in maps["gen"].items():
        t[str(tid)] = (-net.res_gen.p_mw.at[i], -net.res_gen.q_mvar.at[i])
    return state


def run_power_flow(model: GraphModel, frequency_hz: float) -> Tuple[bool, List[str]]:
    """AC power flow on a CIM17 model. Checks bus voltages against each
    VoltageLevel's limits and branch loading against PATL limits / ratings."""
    if _import_pandapower() is None:
        return True, ["skipped (pandapower not installed)"]
    net, maps = build_pandapower(model, frequency_hz)
    err = _runpp(net)
    if err:
        return False, [err]

    ok = True
    vlim = maps["vlim"]
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


def run_n1_analysis(model: GraphModel, frequency_hz: float) -> Optional[Dict]:
    """N-1 contingency analysis: take each line and transformer out of service in turn
    and re-solve the AC power flow. Returns None if pandapower is missing, else
    {"total": n, "secure": n, "cases": [...]} where each case lists the outaged element
    and its violations: non-convergence, islanded buses, voltages outside the
    VoltageLevel limits and branches above 100 % loading. The base case must converge."""
    if _import_pandapower() is None:
        return None
    net, maps = build_pandapower(model, frequency_hz)
    if _runpp(net):
        return None
    vlim = maps["vlim"]
    cases = []
    for table, label in (("line", "line"), ("trafo", "transformer")):
        for idx in list(net[table].index):
            net[table].at[idx, "in_service"] = False
            case = {"element": f"{label} {net[table].at[idx, 'name']}", "violations": []}
            err = _runpp(net)
            if err:
                case["violations"].append(err)
            else:
                vm = net.res_bus.vm_pu
                islanded = int(vm.isna().sum())
                if islanded:
                    case["violations"].append(f"{islanded} bus(es) islanded")
                bad = [b for b in net.bus.index if not math.isnan(vm.at[b]) and not vlim[b][0] <= vm.at[b] <= vlim[b][1]]
                if bad:
                    case["violations"].append(f"{len(bad)} bus(es) outside voltage limits "
                                              f"(worst {max(abs(vm.at[b] - 1) for b in bad):.3f} pu from 1.0)")
                for res_label, res in (("line", net.res_line), ("transformer", net.res_trafo)):
                    loading = res.loading_percent.dropna()
                    over = loading[loading > 100]
                    if len(over):
                        case["violations"].append(f"{len(over)} {res_label}(s) overloaded "
                                                  f"(max {over.max():.0f} %)")
            net[table].at[idx, "in_service"] = True
            cases.append(case)
    secure = sum(1 for c in cases if not c["violations"])
    return {"total": len(cases), "secure": secure, "cases": cases}


def print_n1_report(result: Optional[Dict], max_listed: int = 5) -> None:
    """Print the N-1 summary. Informational only: violations do not fail the run."""
    if result is None:
        print("  N-1 contingencies: skipped (pandapower not installed or base case did not converge)")
        return
    print(f"  N-1 contingencies: {result['secure']} of {result['total']} outages without violations")
    insecure = [c for c in result["cases"] if c["violations"]]
    for c in insecure[:max_listed]:
        print(f"    {c['element']} out: {'; '.join(c['violations'])}")
    if len(insecure) > max_listed:
        print(f"    ... and {len(insecure) - max_listed} more")


def verify_cim17(filename: str, expected_counts: Dict[str, int], frequency_hz: float,
                 n1: bool = True) -> bool:
    """Read the CIM17 file back with cim-graph and check it object by object.
    With n1, an N-1 contingency report is added; it is informational and never fails the run."""
    ok = True
    model = NodeBreakerModel(container=None, connection=XMLFile(filename=filename))
    counts = {cls.__name__: len(objs) for cls, objs in model.graph.items() if objs}
    if counts == expected_counts:
        print(f"  Round-trip: OK ({sum(counts.values())} objects read back, counts match)")
    else:
        ok = False
        diff = {k: (expected_counts.get(k), counts.get(k)) for k in set(counts) | set(expected_counts)
                if expected_counts.get(k) != counts.get(k)}
        print(f"  Round-trip: MISMATCH (written, read): {diff}")

    problems = check_model(model)
    if problems:
        ok = False
        print(f"  Model checks: {len(problems)} problem(s)")
        for p in problems:
            print(f"    - {p}")
    else:
        print("  Model checks: OK (connected, wiring, containers, limits, 1 slack, topology, "
              "locations, diagram)")

    gen_p = sum(-g.p.to("MW") for g in _objs(model, cim.SynchronousMachine))
    load_p = sum(l.p.to("MW") for l in _objs(model, cim.EnergyConsumer))
    print(f"  Balance: generation {gen_p:.1f} MW, load {load_p:.1f} MW")

    pf_ok, pf_lines = run_power_flow(model, frequency_hz)
    print(f"  AC power flow: {pf_lines[0]}")
    for line in pf_lines[1:]:
        print(f"    {line}")
    if n1 and pf_ok:
        print_n1_report(run_n1_analysis(model, frequency_hz))
    return ok and pf_ok


def verify_cgmes(filename: str, expected: Dict[str, int], sv_voltages_kv: Optional[Dict[str, float]]) -> bool:
    """Import the CGMES zip with PowSyBl and compare it with the generated model.
    expected: counts of TopologicalNode, ACLineSegment, PowerTransformer,
    SynchronousMachine, EnergyConsumer, LinearShuntCompensator.
    sv_voltages_kv: VoltageLevel mRID -> solved voltage (one bus per voltage level),
    to compare with PowSyBl's own AC load flow."""
    try:
        import pypowsybl as ppl
    except ImportError:
        print("  PowSyBl import: skipped (pypowsybl not installed)")
        return True
    try:
        net = ppl.network.load(filename, parameters={
            "iidm.import.cgmes.post-processors": "cgmesGLImport"})
    except Exception as e:
        print(f"  PowSyBl import: FAILED ({str(e).splitlines()[0][:200]})")
        return False

    ok = True
    got = {
        "TopologicalNode": len(net.get_buses()),
        "ACLineSegment": len(net.get_lines()),
        "PowerTransformer": len(net.get_2_windings_transformers()),
        "SynchronousMachine": len(net.get_generators()),
        "EnergyConsumer": len(net.get_loads()),
        "LinearShuntCompensator": len(net.get_shunt_compensators()),
    }
    if got == expected:
        print(f"  PowSyBl import: OK ({', '.join(f'{v} {k}' for k, v in got.items())})")
    else:
        ok = False
        print(f"  PowSyBl import: COUNT MISMATCH expected {expected}, got {got}")

    try:
        positions = net.get_extensions("substationPosition")
        print(f"  PowSyBl GL: {len(positions)} substation positions imported")
    except Exception as e:
        print(f"  PowSyBl GL: not available ({str(e).splitlines()[0][:120]})")

    result = ppl.loadflow.run_ac(net)[0]
    status = str(result.status).split(".")[-1]
    if status != "CONVERGED":
        print(f"  PowSyBl AC load flow: {status}")
        return False
    if sv_voltages_kv:
        buses = net.get_buses(attributes=["voltage_level_id", "v_mag"])
        dev = [abs(row.v_mag - sv_voltages_kv[row.voltage_level_id]) / sv_voltages_kv[row.voltage_level_id]
               for row in buses.itertuples() if row.voltage_level_id in sv_voltages_kv]
        matched = len(dev) == len(sv_voltages_kv)
        worst = max(dev) if dev else float("nan")
        ok &= matched and worst < 0.01
        print(f"  PowSyBl AC load flow: converged; max |V| deviation from SV {worst * 100:.2f} % "
              f"over {len(dev)}/{len(sv_voltages_kv)} buses" + ("" if matched else " (buses not matched)"))
    else:
        print("  PowSyBl AC load flow: converged")
    return ok
