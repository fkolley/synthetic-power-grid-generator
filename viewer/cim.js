/*
 * CIM / CGMES RDF/XML loader and network model builder.
 *
 * Reads CGMES 2.4.15 profile files (rdf:ID="_x", rdf:about="#_x", values in kV/MW/km)
 * and CIM17 files written by cim-graph (rdf:about="urn:uuid:x", typed SI units), merges
 * all files into one object store and builds a network model for drawing:
 * substations, buses, line corridors, transformers, generators, loads and shunts with
 * solved state (SV), thermal limits, geographic (GL) and diagram (DL) coordinates.
 */
(function (global) {
  "use strict";

  const RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#";

  // Unit symbol -> factor to the display unit (kV, MW, MVAr, MVA, km, ohm, S, A, deg)
  const UNIT_FACTOR = {
    V: 1e-3, volt: 1e-3, kV: 1,
    W: 1e-6, watt: 1e-6, kW: 1e-3, MW: 1,
    VAr: 1e-6, kVAr: 1e-3, MVAr: 1,
    VA: 1e-6, volt_ampere: 1e-6, kVA: 1e-3, MVA: 1,
    m: 1e-3, meter: 1e-3, km: 1,
    ohm: 1, S: 1, siemens: 1, A: 1, ampere: 1, kA: 1e3,
    deg: 1, degree: 1, rad: 180 / Math.PI, s: 1, second: 1,
  };

  function normId(s) {
    if (!s) return s;
    s = s.trim();
    const hash = s.lastIndexOf("#");
    if (hash >= 0) s = s.slice(hash + 1);
    if (s.startsWith("urn:uuid:")) s = s.slice(9);
    if (s.startsWith("_")) s = s.slice(1);
    return s.toLowerCase();
  }

  function isReference(resource) {
    // Enumerations are absolute IRIs ending in "#Kind.value"; references are "#_id" or urn:uuid
    if (resource.startsWith("#") || resource.startsWith("urn:uuid:") || resource.startsWith("_")) return true;
    const frag = resource.slice(resource.lastIndexOf("#") + 1);
    return !/^[A-Za-z]+\.[A-Za-z0-9_]+$/.test(frag);
  }

  function parseValue(el) {
    const text = el.textContent.trim();
    const datatype = el.getAttributeNS(RDF, "datatype");
    if (text !== "" && !isNaN(Number(text))) {
      let v = Number(text);
      if (datatype) {
        const unit = datatype.slice(datatype.lastIndexOf(".") + 1);
        if (unit in UNIT_FACTOR) v *= UNIT_FACTOR[unit];
      }
      return v;
    }
    if (text === "true") return true;
    if (text === "false") return false;
    return text;
  }

  /** Object store: id -> {id, cls, p: {attr: value | {ref: id} | [..]}} */
  class Store {
    constructor() {
      this.objects = new Map();
      this.backrefs = new Map();     // target id -> [{from, prop}]
      this.files = [];
    }

    _obj(id) {
      let o = this.objects.get(id);
      if (!o) { o = { id, cls: null, p: {} }; this.objects.set(id, o); }
      return o;
    }

    addXml(name, text) {
      const doc = new DOMParser().parseFromString(text, "application/xml");
      const err = doc.getElementsByTagName("parsererror")[0];
      if (err) throw new Error(`${name}: not well-formed XML`);
      const root = doc.documentElement;
      if (root.localName !== "RDF") throw new Error(`${name}: not an RDF/XML file`);
      const header = { name, profiles: [], description: "", scenarioTime: "" };
      let count = 0;
      for (const el of root.children) {
        if (el.localName === "FullModel") {
          for (const c of el.children) {
            if (c.localName === "Model.profile") header.profiles.push(c.textContent.trim());
            if (c.localName === "Model.description") header.description = c.textContent.trim();
            if (c.localName === "Model.scenarioTime") header.scenarioTime = c.textContent.trim();
          }
          continue;
        }
        const defId = el.getAttributeNS(RDF, "ID");
        const about = el.getAttributeNS(RDF, "about");
        const id = normId(defId || about);
        if (!id) continue;
        const o = this._obj(id);
        if (defId || !o.cls) o.cls = el.localName;
        for (const c of el.children) {
          const key = c.localName.slice(c.localName.lastIndexOf(".") + 1);
          if (key === "identifier" || key === "mRID") continue;
          const res = c.getAttributeNS(RDF, "resource");
          let value;
          if (res) {
            value = isReference(res) ? { ref: normId(res) } : res.slice(res.lastIndexOf(".") + 1);
          } else {
            value = parseValue(c);
          }
          if (key in o.p) {
            if (!Array.isArray(o.p[key])) o.p[key] = [o.p[key]];
            o.p[key].push(value);
          } else {
            o.p[key] = value;
          }
        }
        count++;
      }
      header.objects = count;
      this.files.push(header);
    }

    finish() {
      for (const o of this.objects.values()) {
        for (const [prop, val] of Object.entries(o.p)) {
          for (const v of Array.isArray(val) ? val : [val]) {
            if (v && typeof v === "object" && v.ref) {
              if (!this.backrefs.has(v.ref)) this.backrefs.set(v.ref, []);
              this.backrefs.get(v.ref).push({ from: o, prop });
            }
          }
        }
      }
    }

    get(id) { return this.objects.get(id); }

    ofClass(...classes) {
      const set = new Set(classes);
      return [...this.objects.values()].filter((o) => set.has(o.cls));
    }

    /** Objects referenced by o.prop */
    refs(o, prop) {
      const v = o && o.p[prop];
      if (v === undefined) return [];
      return (Array.isArray(v) ? v : [v]).filter((x) => x && x.ref).map((x) => this.objects.get(x.ref)).filter(Boolean);
    }

    ref(o, prop) { return this.refs(o, prop)[0]; }

    /** Objects whose `prop` references o (optionally restricted to classes) */
    referrers(o, prop, classes) {
      const list = (o && this.backrefs.get(o.id)) || [];
      return list.filter((b) => b.prop === prop && (!classes || classes.includes(b.from.cls))).map((b) => b.from);
    }

    /** Association in either direction: o.fwd -> X, or X.inv -> o */
    linked(o, fwd, inv, classes) {
      const out = new Set(this.refs(o, fwd));
      if (inv) for (const x of this.referrers(o, inv, classes)) out.add(x);
      return [...out];
    }
  }

  async function loadFiles(files) {
    const store = new Store();
    for (const f of files) {
      if (/\.zip$/i.test(f.name)) {
        const zip = await JSZip.loadAsync(f.data);
        const names = Object.keys(zip.files).filter((n) => /\.(xml|rdf)$/i.test(n) && !zip.files[n].dir).sort();
        for (const n of names) store.addXml(n, await zip.files[n].async("string"));
      } else {
        store.addXml(f.name, typeof f.data === "string" ? f.data : new TextDecoder().decode(f.data));
      }
    }
    store.finish();
    return buildModelWithLayout(store);
  }

  // ---------------------------------------------------------------------------

  const num = (v, d = 0) => (typeof v === "number" && isFinite(v) ? v : d);

  function buildModel(store) {
    const S = store;
    const name = (o) => (o && (o.p.name || o.p.description)) || (o ? `${o.cls} ${o.id.slice(0, 8)}` : "");

    // --- Coordinates ---------------------------------------------------------------
    const pointsOf = (loc, pointCls, pointProp) =>
      S.referrers(loc, pointProp, [pointCls])
        .map((pt) => ({ seq: num(pt.p.sequenceNumber), x: Number(pt.p.xPosition), y: Number(pt.p.yPosition) }))
        .filter((pt) => isFinite(pt.x) && isFinite(pt.y))
        .sort((a, b) => a.seq - b.seq);

    const geoOf = new Map();          // psr id -> [[lat, lon], ...]
    for (const loc of S.ofClass("Location")) {
      const pts = pointsOf(loc, "PositionPoint", "Location").map((pt) => [pt.y, pt.x]);
      if (!pts.length) continue;
      const psrs = S.linked(loc, "PowerSystemResources", "Location");
      for (const psr of psrs) geoOf.set(psr.id, pts);
    }
    const dlOf = new Map();           // object id -> [[x, y], ...]
    for (const d of S.ofClass("DiagramObject")) {
      const pts = pointsOf(d, "DiagramObjectPoint", "DiagramObject").map((pt) => [pt.x, pt.y]);
      const target = S.ref(d, "IdentifiedObject");
      if (target && pts.length) dlOf.set(target.id, pts);
    }
    for (const o of S.objects.values()) {        // CIM17 inverse: IdentifiedObject.DiagramObjects
      for (const d of S.refs(o, "DiagramObjects")) {
        const pts = pointsOf(d, "DiagramObjectPoint", "DiagramObject").map((pt) => [pt.x, pt.y]);
        if (pts.length && !dlOf.has(o.id)) dlOf.set(o.id, pts);
      }
    }

    // --- State variables and limits ---------------------------------------------------
    const svV = new Map();           // TopologicalNode id -> {v, angle}
    for (const sv of S.ofClass("SvVoltage")) {
      const tn = S.ref(sv, "TopologicalNode");
      if (tn) svV.set(tn.id, { v: num(sv.p.v, NaN), angle: num(sv.p.angle, NaN) });
    }
    const svFlow = new Map();        // Terminal id -> {p, q}
    for (const f of S.ofClass("SvPowerFlow")) {
      const t = S.ref(f, "Terminal");
      if (t) svFlow.set(t.id, { p: num(f.p.p, NaN), q: num(f.p.q, NaN) });
    }
    const limitA = new Map();        // Terminal / Equipment id -> PATL [A]
    for (const lim of S.ofClass("CurrentLimit")) {
      const ols = S.ref(lim, "OperationalLimitSet") || S.referrers(lim, "OperationalLimitValue")[0];
      if (!ols) continue;
      const value = num(lim.p.value, num(lim.p.normalValue, NaN));
      for (const target of [S.ref(ols, "Terminal"), S.ref(ols, "Equipment")].filter(Boolean)) {
        const prev = limitA.get(target.id);
        if (!(prev <= value)) limitA.set(target.id, value);
      }
    }

    // --- Containers ----------------------------------------------------------------------
    const kvOfBaseVoltage = (bv) => num(bv && bv.p.nominalVoltage, NaN);
    const substations = new Map();
    for (const s of S.ofClass("Substation")) {
      substations.set(s.id, { kind: "substation", id: s.id, obj: s, name: name(s), geo: geoOf.get(s.id),
                              vls: [], buses: [], gens: [], loads: [], shunts: [], trafos: [] });
    }
    const vls = new Map();
    for (const vl of S.ofClass("VoltageLevel")) {
      const sub = substations.get((S.ref(vl, "Substation") || {}).id);
      const kv = kvOfBaseVoltage(S.ref(vl, "BaseVoltage"));
      const rec = { id: vl.id, obj: vl, name: name(vl), kv, sub, low: num(vl.p.lowVoltageLimit, NaN),
                    high: num(vl.p.highVoltageLimit, NaN) };
      vls.set(vl.id, rec);
      if (sub) sub.vls.push(rec);
    }

    // --- Buses: ConnectivityNodes, or TopologicalNodes for bus-branch models -------------
    const buses = new Map();
    const hasCN = S.ofClass("ConnectivityNode").length > 0;
    for (const n of S.ofClass(hasCN ? "ConnectivityNode" : "TopologicalNode")) {
      const container = S.ref(n, "ConnectivityNodeContainer");
      const vl = container && vls.get(container.id);
      const tn = hasCN ? S.ref(n, "TopologicalNode") : n;
      const kv = vl ? vl.kv : kvOfBaseVoltage(S.ref(tn, "BaseVoltage"));
      const sv = tn && svV.get(tn.id);
      const bus = { kind: "bus", id: n.id, obj: n, name: name(n), kv, vl, sub: vl && vl.sub,
                    v: sv ? sv.v : NaN, angle: sv ? sv.angle : NaN, dl: dlOf.get(n.id) || (tn && dlOf.get(tn.id)),
                    equipment: [] };
      bus.vpu = bus.v / bus.kv;
      if (vl) bus.limits = [vl.low / bus.kv, vl.high / bus.kv];
      buses.set(n.id, bus);
      if (bus.sub) bus.sub.buses.push(bus);
    }
    const busOfTerminal = (t) => buses.get((S.ref(t, hasCN ? "ConnectivityNode" : "TopologicalNode") || {}).id);

    const terminalsOf = (eq) =>
      S.linked(eq, "Terminals", "ConductingEquipment", ["Terminal"])
        .sort((a, b) => num(a.p.sequenceNumber) - num(b.p.sequenceNumber));

    function terminalInfo(t) {
      const bus = busOfTerminal(t);
      const f = svFlow.get(t.id);
      const info = { terminal: t, bus, p: f ? f.p : NaN, q: f ? f.q : NaN, limit: limitA.get(t.id) };
      if (f && bus && bus.v > 0) {
        info.s = Math.hypot(f.p, f.q);
        info.i = (info.s * 1000) / (Math.sqrt(3) * bus.v);       // A
        if (info.limit > 0) info.loading = info.i / info.limit;
      }
      return info;
    }

    function equipmentContainerSub(eq) {
      const c = S.ref(eq, "EquipmentContainer");
      if (!c) return undefined;
      if (substations.has(c.id)) return substations.get(c.id);
      const vl = vls.get(c.id);
      return vl && vl.sub;
    }

    // --- Lines: segments grouped into corridors by their Line container ----------------
    const corridors = new Map();
    const segments = [];
    for (const seg of S.ofClass("ACLineSegment")) {
      const terms = terminalsOf(seg).map(terminalInfo);
      if (terms.length < 2 || !terms[0].bus || !terms[1].bus) continue;
      const loading = Math.max(...terms.map((t) => (t.loading >= 0 ? t.loading : -1)));
      const s = { kind: "segment", id: seg.id, obj: seg, name: name(seg), kv: terms[0].bus.kv,
                  length: num(seg.p.length, NaN), r: num(seg.p.r, NaN), x: num(seg.p.x, NaN), bch: num(seg.p.bch, NaN),
                  terms, loading: loading >= 0 ? loading : NaN, limit: terms[0].limit,
                  geo: geoOf.get(seg.id), dl: dlOf.get(seg.id) };
      segments.push(s);
      const container = S.ref(seg, "EquipmentContainer");
      const key = container && container.cls === "Line" ? container.id : seg.id;
      if (!corridors.has(key)) {
        corridors.set(key, { kind: "line", id: key, obj: container && container.cls === "Line" ? container : seg,
                             name: container && container.cls === "Line" ? name(container) : s.name,
                             kv: s.kv, segments: [], geo: geoOf.get(key) });
      }
      const c = corridors.get(key);
      c.segments.push(s);
      s.corridor = c;
    }
    for (const c of corridors.values()) {
      const first = c.segments[0];
      c.bus1 = first.terms[0].bus;
      c.bus2 = first.terms[1].bus;
      c.length = first.length;
      c.loading = Math.max(...c.segments.map((s) => (s.loading >= 0 ? s.loading : -1)));
      if (!(c.loading >= 0)) c.loading = NaN;
      c.p1 = c.segments.reduce((a, s) => a + num(s.terms[0].p), 0);
      c.q1 = c.segments.reduce((a, s) => a + num(s.terms[0].q), 0);
      c.p2 = c.segments.reduce((a, s) => a + num(s.terms[1].p), 0);
      c.losses = c.p1 + c.p2;
      if (!c.geo) c.geo = first.geo;
      if (!c.geo && c.bus1.sub && c.bus2.sub && c.bus1.sub.geo && c.bus2.sub.geo) {
        c.geo = [c.bus1.sub.geo[0], c.bus2.sub.geo[0]];
      }
      for (const b of [c.bus1, c.bus2]) b.equipment.push(c);
    }

    // --- Transformers ------------------------------------------------------------------
    const trafos = [];
    for (const pt of S.ofClass("PowerTransformer")) {
      const ends = S.linked(pt, "PowerTransformerEnd", "PowerTransformer", ["PowerTransformerEnd"])
        .sort((a, b) => num(a.p.endNumber) - num(b.p.endNumber));
      const terms = (ends.length ? ends.map((e) => S.ref(e, "Terminal")).filter(Boolean) : terminalsOf(pt)).map(terminalInfo);
      if (terms.length < 2 || !terms[0].bus || !terms[1].bus) continue;
      const ratedS = num(ends[0] && ends[0].p.ratedS, NaN);
      const sMax = Math.max(...terms.map((t) => num(t.s, 0)));
      const t = { kind: "trafo", id: pt.id, obj: pt, name: name(pt), ends, terms, ratedS,
                  kv: terms[0].bus.kv, kvLow: terms[1].bus.kv,
                  loading: ratedS > 0 && terms[0].s >= 0 ? sMax / ratedS : NaN,
                  sub: equipmentContainerSub(pt) || terms[0].bus.sub, dl: dlOf.get(pt.id) };
      trafos.push(t);
      if (t.sub) t.sub.trafos.push(t);
      for (const ti of terms) ti.bus.equipment.push(t);
    }

    // --- Single-terminal equipment -------------------------------------------------------
    function single(cls, kind, extra) {
      const out = [];
      for (const eq of S.ofClass(...cls)) {
        const terms = terminalsOf(eq).map(terminalInfo);
        if (!terms.length || !terms[0].bus) continue;
        const bus = terms[0].bus;
        const e = Object.assign({ kind, id: eq.id, obj: eq, name: name(eq), bus, kv: bus.kv, term: terms[0],
                                  sub: bus.sub, dl: dlOf.get(eq.id), geo: geoOf.get(eq.id) }, extra(eq, terms[0]));
        out.push(e);
        bus.equipment.push(e);
      }
      return out;
    }
    const gens = single(["SynchronousMachine", "AsynchronousMachine", "ExternalNetworkInjection"], "gen", (eq, t) => {
      const unit = S.ref(eq, "GeneratingUnit");
      const rc = S.ref(eq, "RegulatingControl");
      // Load sign convention in CIM: generation is negative
      const p = isFinite(t.p) ? -t.p : -num(eq.p.p, NaN);
      const q = isFinite(t.q) ? -t.q : -num(eq.p.q, NaN);
      return { p, q, ratedS: num(eq.p.ratedS, NaN), maxQ: num(eq.p.maxQ, NaN), minQ: num(eq.p.minQ, NaN),
               slack: num(eq.p.referencePriority) > 0, unit, unitCls: unit && unit.cls,
               pMax: unit ? num(unit.p.maxOperatingP, NaN) : NaN, pMin: unit ? num(unit.p.minOperatingP, NaN) : NaN,
               target: rc ? num(rc.p.targetValue, NaN) : NaN };
    });
    const loads = single(["EnergyConsumer", "ConformLoad", "NonConformLoad"], "load", (eq, t) => ({
      p: isFinite(t.p) ? t.p : num(eq.p.p, NaN), q: isFinite(t.q) ? t.q : num(eq.p.q, NaN),
    }));
    const shunts = single(["LinearShuntCompensator", "NonlinearShuntCompensator"], "shunt", (eq, t) => {
      const b = num(eq.p.bPerSection, NaN) * num(eq.p.sections, 1);
      return { b, capacitive: b > 0, qNom: b * Math.pow(num(eq.p.nomU, t.bus.kv), 2), q: t.q };
    });
    for (const g of gens) if (g.sub) g.sub.gens.push(g);
    for (const l of loads) if (l.sub) l.sub.loads.push(l);
    for (const s of shunts) if (s.sub) s.sub.shunts.push(s);

    // --- Substation summaries ---------------------------------------------------------
    for (const sub of substations.values()) {
      sub.kv = Math.max(...sub.buses.map((b) => b.kv).filter(isFinite), -Infinity);
      sub.kvs = [...new Set(sub.buses.map((b) => b.kv))].sort((a, b) => b - a);
      sub.loadP = sub.loads.reduce((a, l) => a + num(l.p), 0);
      sub.genP = sub.gens.reduce((a, g) => a + num(g.p), 0);
      const vpus = sub.buses.map((b) => b.vpu).filter(isFinite);
      sub.vpuMin = vpus.length ? Math.min(...vpus) : NaN;
      sub.vpuMax = vpus.length ? Math.max(...vpus) : NaN;
      sub.dl = sub.buses.find((b) => b.dl) && sub.buses.find((b) => b.dl).dl;
    }

    // --- Totals -------------------------------------------------------------------------
    const totals = {
      load: loads.reduce((a, l) => a + num(l.p), 0),
      gen: gens.reduce((a, g) => a + num(g.p), 0),
      hasSV: svV.size > 0,
      vpu: [...buses.values()].map((b) => b.vpu).filter(isFinite),
      maxLoading: Math.max(0, ...[...corridors.values(), ...trafos].map((x) => x.loading).filter(isFinite)),
    };
    totals.losses = totals.hasSV ? totals.gen - totals.load - shunts.reduce((a, s) => a + num(s.term.p), 0) : NaN;

    return {
      store, files: store.files,
      substations: [...substations.values()], buses: [...buses.values()], lines: [...corridors.values()],
      segments, trafos, gens, loads, shunts, totals,
      hasGeo: [...substations.values()].some((s) => s.geo) || [...corridors.values()].some((c) => c.geo),
      hasDiagram: [...buses.values()].some((b) => b.dl),
    };
  }

  // ---------------------------------------------------------------------------
  // Automatic schematic layout for models without a DL profile
  // ---------------------------------------------------------------------------

  /** Force-directed (Fruchterman-Reingold) placement of substations, then buses,
   *  equipment and parallel circuits arranged around them like a diagram layout. */
  function autoLayout(model) {
    // Nodes: substations, plus buses that belong to none
    const nodeOf = new Map();
    const nodes = [];
    for (const b of model.buses) {
      const key = b.sub ? b.sub.id : b.id;
      if (!nodeOf.has(key)) { nodeOf.set(key, nodes.length); nodes.push({ key, buses: [] }); }
      nodes[nodeOf.get(key)].buses.push(b);
    }
    const n = nodes.length;
    const edges = new Set();
    const nodeIdx = (bus) => nodeOf.get(bus.sub ? bus.sub.id : bus.id);
    for (const c of model.lines) {
      const a = nodeIdx(c.bus1), b = nodeIdx(c.bus2);
      if (a !== b) edges.add(a < b ? `${a},${b}` : `${b},${a}`);
    }
    const E = [...edges].map((e) => e.split(",").map(Number));

    // Deterministic start on a spiral, then FR iterations
    const area = n * 140 * 140, k = Math.sqrt(area / Math.max(1, n));
    const pos = nodes.map((_, i) => { const r = 12 * Math.sqrt(i), a = i * 2.39996; return [r * Math.cos(a) * k / 12, r * Math.sin(a) * k / 12]; });
    const iterations = n > 800 ? 120 : 260;
    let temp = Math.sqrt(area) / 8;
    for (let it = 0; it < iterations; it++) {
      const disp = pos.map(() => [0, 0]);
      for (let i = 0; i < n; i++) {
        for (let j = i + 1; j < n; j++) {
          let dx = pos[i][0] - pos[j][0], dy = pos[i][1] - pos[j][1];
          const d2 = dx * dx + dy * dy || 0.01, f = (k * k) / d2;
          dx *= f; dy *= f;
          disp[i][0] += dx; disp[i][1] += dy; disp[j][0] -= dx; disp[j][1] -= dy;
        }
      }
      for (const [a, b] of E) {
        const dx = pos[a][0] - pos[b][0], dy = pos[a][1] - pos[b][1], d = Math.hypot(dx, dy) || 0.01, f = d / k;
        disp[a][0] -= dx * f; disp[a][1] -= dy * f; disp[b][0] += dx * f; disp[b][1] += dy * f;
      }
      for (let i = 0; i < n; i++) {
        const d = Math.hypot(disp[i][0], disp[i][1]) || 0.01, s = Math.min(d, temp) / d;
        pos[i][0] += disp[i][0] * s; pos[i][1] += disp[i][1] * s;
      }
      temp *= 0.985;
    }

    // Buses: highest voltage up-right of the lower ones, as in the generated DL
    const busXY = new Map();
    nodes.forEach((node, i) => {
      node.buses.sort((a, b) => num(a.kv) - num(b.kv)).forEach((b, j) => {
        const xy = [pos[i][0] + 40 * j, pos[i][1] - 40 * j];
        busXY.set(b.id, xy);
        b.dl = [[xy[0] - 15, xy[1]], [xy[0] + 15, xy[1]]];
      });
    });
    for (const c of model.lines) {
      const a = busXY.get(c.bus1.id), b = busXY.get(c.bus2.id);
      const len = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1, nx = -(b[1] - a[1]) / len, ny = (b[0] - a[0]) / len;
      c.segments.forEach((s, idx) => {
        const off = (idx - (c.segments.length - 1) / 2) * 6;
        s.dl = [[a[0] + nx * off, a[1] + ny * off], [b[0] + nx * off, b[1] + ny * off]];
      });
    }
    const slots = new Map();
    const slot = (bus, kind) => {
      const key = bus.id + kind, s = slots.get(key) || 0;
      slots.set(key, s + 1);
      const [x, y] = busXY.get(bus.id), [dx, dy] = { gen: [0, -22], load: [0, 22], shunt: [24, 8] }[kind];
      return [x + dx + 10 * s, y + dy];
    };
    for (const g of model.gens) g.dl = [slot(g.bus, "gen")];
    for (const l of model.loads) l.dl = [slot(l.bus, "load")];
    for (const s of model.shunts) s.dl = [slot(s.bus, "shunt")];
    const trafoCount = new Map();
    for (const t of model.trafos) {
      const a = busXY.get(t.terms[0].bus.id), b = busXY.get(t.terms[1].bus.id), key = t.terms[0].bus.id + t.terms[1].bus.id;
      const k2 = trafoCount.get(key) || 0;
      trafoCount.set(key, k2 + 1);
      t.dl = [[(a[0] + b[0]) / 2 + 8 * k2, (a[1] + b[1]) / 2]];
    }
    for (const sub of model.substations) sub.dl = sub.buses[0] && sub.buses[0].dl;
    model.hasDiagram = true;
    model.autoLayout = true;
  }

  const _buildModel = buildModel;
  function buildModelWithLayout(store) {
    const model = _buildModel(store);
    if (!model.hasDiagram && model.buses.length) autoLayout(model);
    return model;
  }

  global.CIM = { loadFiles, buildModel: buildModelWithLayout, autoLayout, Store, normId };
})(window);
