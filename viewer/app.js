/* Grid Viewer: geographic (GL) and schematic (DL) views of a CIM / CGMES model. */
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmt = (v, d = 1, unit = "") => (isNum(v) ? `${v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d })}${unit ? " " + unit : ""}` : "–");

  const state = {
    model: null, view: "geo", color: "voltage", selected: null,
    entities: new Map(),       // id -> entity
    fitted: { geo: false, sld: false },
    halo: { geo: null, sld: null },
  };

  // --- Colours ---------------------------------------------------------------------
  const COLORS = {
    kv380: css("--kv380"), kv220: css("--kv220"), kv110: css("--kv110"), kvlow: css("--kvlow"),
    gen: css("--gen"), load: css("--load"), shunt: css("--shunt"), neutral: "#6b7785", dim: "#4a5563", accent: css("--accent"),
  };
  const kvColor = (kv) => (kv >= 300 ? COLORS.kv380 : kv >= 200 ? COLORS.kv220 : kv >= 100 ? COLORS.kv110 : COLORS.kvlow);
  const kvLabel = (kv) => (kv >= 300 ? "≥ 300 kV" : kv >= 200 ? "200–300 kV" : kv >= 100 ? "100–200 kV" : "< 100 kV");

  function lerpColor(stops, t) {
    t = Math.max(0, Math.min(1, t));
    for (let i = 1; i < stops.length; i++) {
      if (t <= stops[i][0]) {
        const [t0, c0] = stops[i - 1], [t1, c1] = stops[i];
        const f = (t - t0) / (t1 - t0 || 1);
        const a = c0.match(/\w\w/g).map((h) => parseInt(h, 16)), b = c1.match(/\w\w/g).map((h) => parseInt(h, 16));
        return "#" + a.map((x, k) => Math.round(x + (b[k] - x) * f).toString(16).padStart(2, "0")).join("");
      }
    }
    return stops[stops.length - 1][1];
  }
  const LOADING_STOPS = [[0, "#1fb5a8"], [0.5, "#7bd35a"], [0.75, "#f1c40f"], [0.9, "#ff8c3a"], [1.0, "#ff3b4e"]];
  const VPU_STOPS = [[0, "#3b6cff"], [0.25, "#36c5f0"], [0.5, "#4ade80"], [0.75, "#facc15"], [1, "#ff3b4e"]];
  const loadingColor = (x) => (isNum(x) ? lerpColor(LOADING_STOPS, x / 1.0) : COLORS.dim);
  const vpuColor = (v) => (isNum(v) ? lerpColor(VPU_STOPS, (v - 0.9) / 0.2) : COLORS.dim);

  function colorOf(ent) {
    const mode = state.color;
    switch (ent.kind) {
      case "line": case "segment":
        return mode === "loading" ? loadingColor(ent.loading) : mode === "vpu" ? COLORS.dim : kvColor(ent.kv);
      case "trafo":
        return mode === "loading" ? loadingColor(ent.loading) : mode === "vpu" ? COLORS.dim : kvColor(ent.kv);
      case "bus":
        return mode === "vpu" ? vpuColor(ent.vpu) : mode === "loading" ? COLORS.neutral : kvColor(ent.kv);
      case "substation":
        return mode === "vpu" ? vpuColor(worstVpu(ent)) : mode === "loading" ? COLORS.neutral : kvColor(ent.kv);
      case "gen": return COLORS.gen;
      case "load": return COLORS.load;
      case "shunt": return COLORS.shunt;
    }
    return COLORS.neutral;
  }
  const worstVpu = (sub) => (Math.abs(sub.vpuMin - 1) > Math.abs(sub.vpuMax - 1) ? sub.vpuMin : sub.vpuMax);

  // --- Maps ------------------------------------------------------------------------
  function makePanes(map) {
    for (const [name, z] of [["boxes", 402], ["halo", 405], ["lines", 410], ["flow", 412], ["conn", 415], ["buses", 420]]) {
      map.createPane(name).style.zIndex = z;
    }
  }

  const geoMap = L.map("geoMap", { zoomControl: true, worldCopyJump: true, zoomSnap: 0.25, zoomDelta: 0.5 }).setView([51, 10], 6);
  makePanes(geoMap);
  L.control.scale({ imperial: false, position: "bottomleft" }).addTo(geoMap);
  const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/";
  const esri = (path, opts = {}) => L.tileLayer(`${ESRI}${path}/MapServer/tile/{z}/{y}/{x}`,
    { maxZoom: 16, attribution: "Tiles © Esri — Esri, HERE, Garmin, © OpenStreetMap contributors", ...opts });
  const BASEMAPS = {
    dark: L.layerGroup([esri("Canvas/World_Dark_Gray_Base"), esri("Canvas/World_Dark_Gray_Reference", { opacity: 0.8 })]),
    light: L.layerGroup([esri("Canvas/World_Light_Gray_Base"), esri("Canvas/World_Light_Gray_Reference")]),
    osm: L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      { maxZoom: 19, attribution: "© OpenStreetMap contributors" }),
    satellite: L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 19, attribution: "Tiles © Esri" }),
    none: null,
  };
  let basemap = BASEMAPS.dark.addTo(geoMap);

  const sldMap = L.map("sldMap", { crs: L.CRS.Simple, minZoom: -6, maxZoom: 6, zoomSnap: 0.25, zoomDelta: 0.5,
                                   attributionControl: false });
  makePanes(sldMap);
  const maps = { geo: geoMap, sld: sldMap };
  const groups = { geo: L.layerGroup().addTo(geoMap), sld: L.layerGroup().addTo(sldMap) };
  const labelGroups = { geo: L.layerGroup().addTo(geoMap), sld: L.layerGroup().addTo(sldMap) };

  // Diagram coordinates are screen-like (y down); CRS.Simple has y up.
  const P = ([x, y]) => L.latLng(-y, x);
  const G = ([lat, lon]) => L.latLng(lat, lon);
  const dist2 = (a, b) => (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2;
  const orient = (pts, start) => (start && dist2(pts[pts.length - 1], start) < dist2(pts[0], start) ? [...pts].reverse() : pts);
  const center = (pts) => [pts.reduce((a, p) => a + p[0], 0) / pts.length, pts.reduce((a, p) => a + p[1], 0) / pts.length];

  // --- Tooltip and details content ---------------------------------------------------
  const KIND_LABEL = { substation: "Substation", bus: "Bus", line: "Line", segment: "Circuit", trafo: "Transformer",
                       gen: "Generator", load: "Load", shunt: "Shunt compensator" };

  function loadingBar(x) {
    if (!isNum(x)) return "";
    return `<div class="bar"><div style="width:${Math.min(100, x * 100)}%;background:${loadingColor(x)}"></div></div>`;
  }
  const rows = (pairs) => "<table>" + pairs.filter(Boolean).map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("") + "</table>";
  const vText = (b) => (isNum(b.v) ? `${fmt(b.v, 1, "kV")} · ${fmt(b.vpu, 3)} pu` : "–");

  function tipHtml(ent) {
    const head = `<div class="k">${KIND_LABEL[ent.kind]}${isNum(ent.kv) ? " · " + fmt(ent.kv, 0, "kV") : ""}</div><div class="t">${esc(ent.name)}</div>`;
    switch (ent.kind) {
      case "substation":
        return head + rows([
          ["Voltage levels", ent.kvs.map((k) => fmt(k, 0)).join(" / ") + " kV"],
          ...ent.buses.map((b) => [`V ${fmt(b.kv, 0)} kV`, vText(b)]),
          ent.gens.length && ["Generation", fmt(ent.genP, 1, "MW")],
          ent.loads.length && ["Load", fmt(ent.loadP, 1, "MW")],
          ent.trafos.length && ["Transformers", ent.trafos.length],
        ]);
      case "bus":
        return head + rows([["Voltage", vText(ent)], ["Angle", fmt(ent.angle, 2, "°")], ["Connected", ent.equipment.length]]);
      case "line": {
        const s = ent.segments[0];
        return head + rows([
          ["From → to", `${esc(ent.bus1.sub ? ent.bus1.sub.name : ent.bus1.name)} → ${esc(ent.bus2.sub ? ent.bus2.sub.name : ent.bus2.name)}`],
          ["Length", fmt(ent.length, 1, "km")],
          ["Circuits", ent.segments.length],
          ["P / Q (from end)", `${fmt(ent.p1, 1)} MW / ${fmt(ent.q1, 1)} MVAr`],
          ["Rating", fmt(s.limit, 0, "A") + (ent.segments.length > 1 ? " each" : "")],
          ["Loading", fmt(ent.loading * 100, 0, "%")],
        ]) + loadingBar(ent.loading);
      }
      case "segment":
        return tipHtml(ent.corridor);
      case "trafo":
        return head + rows([
          ["Ratio", `${fmt(ent.kv, 0)} / ${fmt(ent.kvLow, 0)} kV`],
          ["Rated", fmt(ent.ratedS, 0, "MVA")],
          ["P / Q (HV)", `${fmt(ent.terms[0].p, 1)} MW / ${fmt(ent.terms[0].q, 1)} MVAr`],
          ["Loading", fmt(ent.loading * 100, 0, "%")],
        ]) + loadingBar(ent.loading);
      case "gen":
        return head + rows([
          ent.slack && ["Role", "Slack / reference"],
          ["P / Q", `${fmt(ent.p, 1)} MW / ${fmt(ent.q, 1)} MVAr`],
          ["Rated", fmt(ent.ratedS, 0, "MVA")],
          ["P range", `${fmt(ent.pMin, 0)}–${fmt(ent.pMax, 0)} MW`],
          ["V setpoint", fmt(ent.target, 1, "kV")],
        ]);
      case "load":
        return head + rows([["P / Q", `${fmt(ent.p, 1)} MW / ${fmt(ent.q, 1)} MVAr`],
                            ["Power factor", fmt(ent.p / Math.hypot(ent.p, ent.q), 3)]]);
      case "shunt":
        return head + rows([["Type", ent.capacitive ? "Capacitor" : "Reactor"],
                            ["Nominal Q", fmt(Math.abs(ent.qNom), 1, "MVAr")],
                            ["Actual Q", fmt(-ent.q, 1, "MVAr")]]);
    }
    return head;
  }

  function entityLink(e, right = "") {
    return `<li data-id="${e.id}"><span>${esc(e.name)}</span><span class="r">${right}</span></li>`;
  }

  function rawHtml(obj) {
    const store = state.model.store;
    const lines = Object.entries(obj.p).map(([k, v]) => {
      const show = (x) => (x && x.ref ? (() => { const t = store.get(x.ref); return t ? `→ ${t.p.name || t.id} (${t.cls})` : `→ ${x.ref}`; })() : String(x));
      return `${k}: ${Array.isArray(v) ? v.map(show).join(", ") : show(v)}`;
    });
    return `<details class="raw"><summary>CIM attributes · ${esc(obj.cls)}</summary><pre>mRID: ${esc(obj.id)}\n${esc(lines.join("\n"))}</pre></details>`;
  }

  function detailsHtml(ent) {
    let body = "";
    const lineOf = (b) => b.equipment.filter((e) => e.kind === "line");
    switch (ent.kind) {
      case "substation": {
        const lines = [...new Set(ent.buses.flatMap(lineOf))];
        body = `<table class="kv">${[
          ["Voltage levels", ent.kvs.map((k) => fmt(k, 0)).join(" / ") + " kV"],
          ["Generation", fmt(ent.genP, 1, "MW")], ["Load", fmt(ent.loadP, 1, "MW")],
          ent.geo && ["Location", `${ent.geo[0][0].toFixed(4)}° N, ${ent.geo[0][1].toFixed(4)}° E`],
        ].filter(Boolean).map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</table>`;
        const list = (title, items) => (items.length ? `<h2 style="margin-top:12px"><span class="kind">${title}</span></h2><ul class="sub-list">${items.join("")}</ul>` : "");
        body += list("Buses", ent.buses.map((b) => entityLink(b, vText(b))));
        body += list("Generators", ent.gens.map((g) => entityLink(g, fmt(g.p, 1, "MW"))));
        body += list("Loads", ent.loads.map((l) => entityLink(l, fmt(l.p, 1, "MW"))));
        body += list("Shunts", ent.shunts.map((s) => entityLink(s, fmt(Math.abs(s.qNom), 1, "MVAr"))));
        body += list("Transformers", ent.trafos.map((t) => entityLink(t, fmt(t.loading * 100, 0, "%"))));
        body += list("Lines", lines.map((l) => entityLink(l, fmt(l.loading * 100, 0, "%"))));
        break;
      }
      case "bus":
        body = `<table class="kv">${[["Voltage", vText(ent)], ["Angle", fmt(ent.angle, 2, "°")],
          ent.limits && ["Limits", `${fmt(ent.limits[0], 3)}–${fmt(ent.limits[1], 3)} pu`],
          ent.sub && ["Substation", `<a href="#" data-id="${ent.sub.id}">${esc(ent.sub.name)}</a>`]]
          .filter(Boolean).map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</table>`;
        body += `<ul class="sub-list">${ent.equipment.map((e) => entityLink(e, KIND_LABEL[e.kind])).join("")}</ul>`;
        break;
      case "line":
        body = `<table class="kv">${[
          ["From", `<a href="#" data-id="${ent.bus1.id}">${esc(ent.bus1.name)}</a>`],
          ["To", `<a href="#" data-id="${ent.bus2.id}">${esc(ent.bus2.name)}</a>`],
          ["Length", fmt(ent.length, 2, "km")], ["Circuits", ent.segments.length],
          ["P / Q from end", `${fmt(ent.p1, 2)} / ${fmt(ent.q1, 2)}`], ["Losses", fmt(ent.losses, 3, "MW")],
          ["Loading", fmt(ent.loading * 100, 1, "%")],
        ].map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</table>${loadingBar(ent.loading)}`;
        for (const s of ent.segments) {
          body += `<h2 style="margin-top:12px"><span class="kind">Circuit</span>${esc(s.name)}</h2><table class="kv">${[
            s.obj.p.description && ["Type", esc(s.obj.p.description)],
            ["R / X", `${fmt(s.r, 3)} / ${fmt(s.x, 3)} Ω`], ["B", fmt(s.bch * 1e6, 1, "µS")],
            ["Rating (PATL)", fmt(s.limit, 0, "A")], ["Loading", fmt(s.loading * 100, 1, "%")],
          ].filter(Boolean).map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</table>`;
        }
        body += rawHtml(ent.segments[0].obj);
        return body;
      default:
        body = tipHtml(ent).replace(/^<div class="k">.*?<\/div><div class="t">.*?<\/div>/, "").replace("<table>", '<table class="kv">');
        if (ent.bus) body += `<table class="kv"><tr><td>Bus</td><td><a href="#" data-id="${ent.bus.id}">${esc(ent.bus.name)}</a></td></tr></table>`;
        if (ent.kind === "trafo") {
          body += `<table class="kv">${ent.terms.map((t, i) => `<tr><td>${i ? "LV" : "HV"} bus</td><td><a href="#" data-id="${t.bus.id}">${esc(t.bus.name)}</a></td></tr>`).join("")}</table>`;
        }
    }
    return body + rawHtml(ent.obj);
  }

  // --- Drawing -------------------------------------------------------------------------
  function register(ent, view, layer, kind = "main") {
    state.entities.set(ent.id, ent);
    ent.layers = ent.layers || { geo: [], sld: [] };
    ent.layers[view].push({ layer, kind });
    if (kind === "main" || kind === "box") {
      layer.bindTooltip(() => tipHtml(ent), { sticky: true, className: "grid-tip", direction: "top", offset: [0, -10], opacity: 1 });
      layer.on("mouseover", () => hover(ent, view, true));
      layer.on("mouseout", () => hover(ent, view, false));
      layer.on("click", (ev) => { L.DomEvent.stopPropagation(ev); select(ent, false); });
    }
    return layer;
  }

  const lineWeight = (kv, n = 1) => (kv >= 300 ? 4 : kv >= 200 ? 3.2 : 2.2) + 1.3 * (n - 1);

  /** Canvas layer drawing dots that move along branches in the direction of active
   *  power flow. One canvas per map, ~30 fps, only while visible and not zooming,
   *  so it stays cheap for large grids (animating SVG dash offsets repaints every path). */
  const FlowLayer = L.Layer.extend({
    initialize(view) { this.view = view; this.items = []; this.paths = []; },
    add(item) { this.items.push(item); },
    onAdd(map) {
      this._canvas = L.DomUtil.create("canvas", "flow-canvas");
      map.getPane("flow").appendChild(this._canvas);
      map.on("moveend zoomend resize viewreset", this._reset, this);
      map.on("zoomstart", this._hide, this);
      this._reset();
      this._frame = requestAnimationFrame((t) => this._draw(t));
    },
    onRemove(map) {
      cancelAnimationFrame(this._frame);
      map.off("moveend zoomend resize viewreset", this._reset, this);
      map.off("zoomstart", this._hide, this);
      this._canvas.remove();
    },
    _hide() { this._canvas.style.visibility = "hidden"; },
    _reset() {
      const map = this._map, size = map.getSize(), dpr = window.devicePixelRatio || 1;
      Object.assign(this._canvas, { width: size.x * dpr, height: size.y * dpr });
      Object.assign(this._canvas.style, { width: `${size.x}px`, height: `${size.y}px`, visibility: "visible" });
      L.DomUtil.setPosition(this._canvas, map.containerPointToLayerPoint([0, 0]));
      const pad = 50;
      this.paths = [];
      for (const it of this.items) {
        const pts = it.latlngs.map((ll) => map.latLngToContainerPoint(ll));
        const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
        if (Math.max(...xs) < -pad || Math.min(...xs) > size.x + pad || Math.max(...ys) < -pad || Math.min(...ys) > size.y + pad) continue;
        const cum = [0];
        for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + pts[i].distanceTo(pts[i - 1]));
        if (cum[cum.length - 1] > 12) this.paths.push({ pts, cum, len: cum[cum.length - 1], it });
      }
    },
    _draw(t) {
      this._frame = requestAnimationFrame((tt) => this._draw(tt));
      if (t - (this._last || 0) < 33) return;
      this._last = t;
      const ctx = this._canvas.getContext("2d"), dpr = window.devicePixelRatio || 1;
      const active = state.view === this.view && $("main").classList.contains("animate")
        && this._canvas.style.visibility !== "hidden" && !document.hidden;
      if (!active && this._idle) return;            // nothing to draw: don't repaint every frame
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, this._canvas.width, this._canvas.height);
      this._idle = !active;
      if (!active) return;
      ctx.fillStyle = "rgba(255,255,255,0.85)";
      const spacing = 26;
      for (const p of this.paths) {
        const r = p.it.radius, off = ((t / 1000) * p.it.speed) % spacing;
        let seg = 1;
        ctx.beginPath();
        for (let d = off; d < p.len; d += spacing) {
          while (seg < p.cum.length - 1 && p.cum[seg] < d) seg++;
          const a = p.pts[seg - 1], b = p.pts[seg], f = (d - p.cum[seg - 1]) / (p.cum[seg] - p.cum[seg - 1] || 1);
          const x = a.x + (b.x - a.x) * f, y = a.y + (b.y - a.y) * f;
          ctx.moveTo(x + r, y);
          ctx.arc(x, y, r, 0, 2 * Math.PI);
        }
        ctx.fill();
      }
    },
  });
  const flowLayers = { geo: null, sld: null };

  /** Points start at terminal 1; positive P at terminal 1 flows from terminal 1 to 2. */
  function addFlow(view, latlngs, pFirst, weight, loading) {
    if (!isNum(pFirst) || Math.abs(pFirst) < 0.05) return;
    flowLayers[view].add({ latlngs: pFirst > 0 ? latlngs : [...latlngs].reverse(), radius: Math.max(1.3, weight * 0.42),
                           speed: 18 + 70 * Math.min(1.2, isNum(loading) ? loading : 0.3) });
  }

  function addLabel(view, latlng, text, offset = [8, 0], dir = "right") {
    L.tooltip({ permanent: true, direction: dir, className: "label-tip", offset, interactive: false })
      .setLatLng(latlng).setContent(esc(text)).addTo(labelGroups[view]);
  }

  function drawGeo(m) {
    const g = groups.geo;
    for (const c of m.lines) {
      if (!c.geo || c.geo.length < 2) continue;
      const start = c.bus1.sub && c.bus1.sub.geo && c.bus1.sub.geo[0];
      const pts = orient(c.geo, start);
      const w = lineWeight(c.kv, c.segments.length);
      const line = L.polyline(pts.map(G), { pane: "lines", color: colorOf(c), weight: w,
                                            opacity: 0.95, lineCap: "round", lineJoin: "round", className: "grid-el" });
      register(c, "geo", line).addTo(g);
      addFlow("geo", pts.map(G), c.p1, w, c.loading);
    }
    for (const s of m.substations) {
      if (!s.geo) continue;
      const ll = G(s.geo[0]);
      const r = s.kv >= 300 ? 7.5 : s.kv >= 200 ? 6 : 4.5;
      if (s.gens.length) {
        register(s, "geo", L.circleMarker(ll, { pane: "buses", radius: r + 3.5, color: COLORS.gen, weight: 2, fill: false,
                                                 interactive: false }), "ring").addTo(g);
      }
      register(s, "geo", L.circleMarker(ll, { pane: "buses", radius: r, color: "#0b0f14", weight: 1.5,
                                              fillColor: colorOf(s), fillOpacity: 1 })).addTo(g);
      addLabel("geo", ll, s.name);
    }
  }

  const ICON = {
    gen: (c) => `<svg width="22" height="22" viewBox="0 0 22 22"><circle cx="11" cy="11" r="9" fill="#0b0f14" stroke="${c}" stroke-width="2"/><path d="M5.5 11 q2.75 -5.5 5.5 0 t5.5 0" fill="none" stroke="${c}" stroke-width="1.8" stroke-linecap="round"/></svg>`,
    load: (c) => `<svg width="22" height="22" viewBox="0 0 22 22"><path d="M11 1 V12" stroke="${c}" stroke-width="2"/><path d="M5 11 L11 20 L17 11 Z" fill="${c}"/></svg>`,
    cap: (c) => `<svg width="22" height="22" viewBox="0 0 22 22"><path d="M11 1 V8 M4 8 H18 M4 12.5 H18 M11 12.5 V18 M6.5 18 H15.5 M8.5 20.5 H13.5" fill="none" stroke="${c}" stroke-width="2" stroke-linecap="round"/></svg>`,
    reactor: (c) => `<svg width="22" height="22" viewBox="0 0 22 22"><path d="M11 1 V4 c5 0 5 4 0 4 c5 0 5 4 0 4 c5 0 5 4 0 4 V18 M6.5 18 H15.5 M8.5 20.5 H13.5" fill="none" stroke="${c}" stroke-width="2" stroke-linecap="round"/></svg>`,
    trafo: (c1, c2) => `<svg width="26" height="26" viewBox="0 0 26 26"><circle cx="13" cy="9" r="7" fill="none" stroke="${c1}" stroke-width="2.2"/><circle cx="13" cy="17" r="7" fill="none" stroke="${c2}" stroke-width="2.2"/></svg>`,
  };
  function iconFor(ent) {
    const c = colorOf(ent);
    let html, size = 22;
    if (ent.kind === "gen") html = ICON.gen(c);
    else if (ent.kind === "load") html = ICON.load(c);
    else if (ent.kind === "shunt") html = ent.capacitive ? ICON.cap(c) : ICON.reactor(c);
    else if (ent.kind === "trafo") {
      size = 26;
      const hv = state.color === "voltage" ? kvColor(ent.kv) : c, lv = state.color === "voltage" ? kvColor(ent.kvLow) : c;
      html = ICON.trafo(hv, lv);
    }
    return L.divIcon({ className: "eq-icon", html, iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
  }

  function attachPoint(bus, pt) {
    // Nearest point on the busbar polyline (diagram coordinates)
    const d = bus.dl;
    if (d.length === 1) return d[0];
    const [a, b] = [d[0], d[d.length - 1]];
    const t = Math.max(0, Math.min(1, ((pt[0] - a[0]) * (b[0] - a[0]) + (pt[1] - a[1]) * (b[1] - a[1])) / (dist2(a, b) || 1)));
    return [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])];
  }

  function drawSld(m) {
    const g = groups.sld;
    const structured = state.sldLayout === "auto";
    for (const s of m.substations) {
      if (!s.box) continue;
      const rect = L.rectangle(s.box.map(P), { pane: "boxes", color: "#8b98a8", weight: 1, opacity: 0.5,
                                               fillColor: "#ffffff", fillOpacity: 0.03, className: "sub-box" });
      register(s, "sld", rect, "box").addTo(g);
      L.tooltip({ permanent: true, direction: "left", className: "sub-name", offset: [0, 0], interactive: false })
        .setLatLng(P(s.label)).setContent(esc(s.name)).addTo(g);
    }
    for (const s of m.segments) {
      if (!s.dl || s.dl.length < 2) continue;
      const b1 = s.terms[0].bus;
      const pts = orient(s.dl, b1.dl && center(b1.dl));
      const w = lineWeight(s.kv) * 0.8;
      const line = L.polyline(pts.map(P), { pane: "lines", color: colorOf(s), weight: w, opacity: 0.95,
                                            lineCap: "round", className: "grid-el" });
      register(s, "sld", line).addTo(g);
      addFlow("sld", pts.map(P), s.terms[0].p, w, s.loading);
    }
    for (const b of m.buses) {
      if (!b.dl) continue;
      const layer = b.dl.length > 1
        ? L.polyline(b.dl.map(P), { pane: "buses", color: colorOf(b), weight: 7, opacity: 1, lineCap: "butt", className: "grid-el" })
        : L.circleMarker(P(b.dl[0]), { pane: "buses", radius: 6, color: "#0b0f14", weight: 1.5, fillColor: colorOf(b), fillOpacity: 1 });
      register(b, "sld", layer).addTo(g);
      const right = b.dl.reduce((a, p) => (p[0] > a[0] ? p : a), b.dl[0]);
      addLabel("sld", P(right), structured ? `${fmt(b.kv, 0)} kV` : (b.sub ? `${b.sub.name} ${fmt(b.kv, 0)}` : b.name), [6, 0]);
    }
    const equipment = [...m.gens, ...m.loads, ...m.shunts, ...m.trafos];
    for (const e of equipment) {
      const pt = e.dl && e.dl[0];
      if (!pt) continue;
      const busList = e.kind === "trafo" ? e.terms.map((t) => t.bus) : [e.bus];
      for (const b of busList) {
        if (!b.dl) continue;
        const conn = L.polyline([P(pt), P(attachPoint(b, pt))], { pane: "conn", color: kvColor(b.kv), weight: 1.6,
                                                                 opacity: 0.85, interactive: false, className: "eq-conn" });
        register(e, "sld", conn, "conn").addTo(g);
      }
      register(e, "sld", L.marker(P(pt), { icon: iconFor(e), riseOnHover: true })).addTo(g);
    }
  }

  function restyle() {
    for (const ent of state.entities.values()) {
      for (const view of ["geo", "sld"]) {
        for (const { layer, kind } of (ent.layers && ent.layers[view]) || []) {
          if (kind === "conn" || kind === "ring" || kind === "box") continue;
          if (layer instanceof L.Marker) layer.setIcon(iconFor(ent));
          else if (layer instanceof L.CircleMarker) layer.setStyle({ fillColor: colorOf(ent) });
          else layer.setStyle({ color: colorOf(ent), opacity: state.color === "vpu" && (ent.kind === "line" || ent.kind === "segment") ? 0.55 : 0.95 });
        }
      }
    }
    renderLegend();
  }

  function hover(ent, view, on) {
    for (const { layer, kind } of ent.layers[view]) {
      if (kind !== "main" || !(layer instanceof L.Polyline)) continue;
      const w = layer.options._baseWeight || (layer.options._baseWeight = layer.options.weight);
      layer.setStyle({ weight: on ? w + 3 : w });
      if (on) layer.bringToFront();
    }
  }

  // --- Selection --------------------------------------------------------------------
  function positionsFor(ent, view) {
    if (view === "geo") {
      if (ent.kind === "line") return ent.geo || [];
      if (ent.kind === "segment") return ent.corridor.geo || [];
      const sub = ent.kind === "substation" ? ent : ent.sub || (ent.bus && ent.bus.sub);
      return (ent.geo || (sub && sub.geo) || []).slice(0, ent.kind === "substation" ? 1 : undefined);
    }
    if (ent.kind === "line") return ent.segments.flatMap((s) => s.dl || []);
    if (ent.kind === "substation") return ent.box || ent.buses.flatMap((b) => b.dl || []);
    return ent.dl || (ent.bus && ent.bus.dl) || [];
  }
  const toLatLngs = (pts, view) => pts.map(view === "geo" ? G : P);

  function drawHalo(view) {
    if (state.halo[view]) { maps[view].removeLayer(state.halo[view]); state.halo[view] = null; }
    const ent = state.selected;
    if (!ent) return;
    const lls = toLatLngs(positionsFor(ent, view), view);
    if (!lls.length) return;
    const style = { pane: "halo", color: COLORS.accent, opacity: 0.85, interactive: false, className: "selected-halo" };
    const isPath = ent.kind === "line" || ent.kind === "segment" || (ent.kind === "bus" && lls.length > 1 && view === "sld");
    state.halo[view] = (isPath
      ? (ent.kind === "line" && view === "sld"
          ? L.featureGroup(ent.segments.filter((s) => s.dl).map((s) => L.polyline(toLatLngs(s.dl, view), { ...style, weight: 12, lineCap: "round" })))
          : L.polyline(lls, { ...style, weight: 14, lineCap: "round" }))
      : L.featureGroup(lls.length > 1 && ent.kind === "substation" && view === "sld"
          ? [(() => {
              // Fixed margin in diagram units, so single-busbar substations get a visible box too
              const b = L.latLngBounds(lls), m = ent.box ? 8 : 30;
              return L.rectangle([[b.getSouth() - m, b.getWest() - m], [b.getNorth() + m, b.getEast() + m]],
                                 { ...style, weight: 2, fillOpacity: 0.06, fillColor: COLORS.accent });
            })()]
          : [L.circleMarker(lls[0], { ...style, radius: 16, weight: 3, fill: false })])
    ).addTo(maps[view]);
  }

  function zoomTo(ent, view = state.view) {
    const lls = toLatLngs(positionsFor(ent, view), view);
    if (!lls.length) return;
    const map = maps[view];
    if (lls.length === 1) map.flyTo(lls[0], Math.max(map.getZoom(), view === "geo" ? 10 : 1), { duration: 0.6 });
    else map.flyToBounds(L.latLngBounds(lls).pad(0.4), { duration: 0.6, maxZoom: view === "geo" ? 11 : 2 });
  }

  function select(ent, zoom = true) {
    state.selected = ent;
    drawHalo("geo");
    drawHalo("sld");
    const d = $("#details");
    if (!ent) { d.classList.add("hidden"); return; }
    d.innerHTML = `<button class="close" title="Close (Esc)">×</button><h2><span class="kind">${KIND_LABEL[ent.kind]}</span>${esc(ent.name)}</h2>`
      + detailsHtml(ent)
      + `<div class="detail-actions"><button data-act="zoom">Zoom to</button>`
      + (state.model.hasGeo && state.model.hasDiagram ? `<button data-act="other">Show in ${state.view === "geo" ? "schematic" : "map"}</button>` : "")
      + `</div>`;
    d.classList.remove("hidden");
    if (zoom) zoomTo(ent);
  }

  $("#details").addEventListener("click", (ev) => {
    const t = ev.target.closest("[data-id], [data-act], .close");
    if (!t) return;
    ev.preventDefault();
    if (t.classList.contains("close")) return select(null);
    if (t.dataset.act === "zoom") return zoomTo(state.selected);
    if (t.dataset.act === "other") return setView(state.view === "geo" ? "sld" : "geo");
    const ent = state.entities.get(t.dataset.id) || findById(t.dataset.id);
    if (ent) select(ent);
  });
  function findById(id) {
    const m = state.model;
    return [...m.substations, ...m.buses, ...m.lines, ...m.trafos, ...m.gens, ...m.loads, ...m.shunts].find((e) => e.id === id);
  }

  // --- Summary, legend, search -------------------------------------------------------
  function renderSummary(m) {
    const t = m.totals;
    const vmin = t.vpu.length ? Math.min(...t.vpu) : NaN, vmax = t.vpu.length ? Math.max(...t.vpu) : NaN;
    const stat = (v, l) => `<div class="stat"><div class="v">${v}</div><div class="l">${l}</div></div>`;
    $("#summary").innerHTML = `<h2>Network</h2><div class="stats">${[
      stat(m.substations.length, "Substations"), stat(m.buses.length, "Buses"),
      stat(`${m.lines.length}<span style="font-size:11px;color:var(--muted)"> / ${m.segments.length}</span>`, "Lines / circuits"),
      stat(m.trafos.length, "Transformers"),
      stat(fmt(t.gen, 0), "Generation MW"), stat(fmt(t.load, 0), "Load MW"),
      stat(fmt(t.losses, 1), "Losses MW"), stat(fmt(t.maxLoading * 100, 0, "%"), "Max loading"),
      stat(isNum(vmin) ? `${vmin.toFixed(3)}–${vmax.toFixed(3)}` : "–", "Voltage range pu"),
      stat(m.gens.length + " / " + m.loads.length, "Units / loads"),
    ].join("")}</div>
    <details class="files"><summary>${m.files.length} file(s) · ${m.store.objects.size.toLocaleString("en-US")} objects</summary>
    ${m.files.map((f) => `${esc(f.name)} <span style="opacity:.6">(${f.objects} objects${f.profiles.length ? ", " + f.profiles.map((p) => p.split("/").slice(-3, -2)[0]).join(", ") : ""})</span>`).join("<br>")}</details>
    ${t.hasSV ? "" : '<div class="files">No SV profile: flows and voltages unavailable.</div>'}`;
  }

  function renderLegend() {
    const m = state.model;
    if (!m) return;
    let html = "<h2>Legend</h2>";
    if (state.color === "voltage") {
      const kvs = [...new Set(m.buses.map((b) => b.kv))].sort((a, b) => b - a);
      html += kvs.map((kv) => `<div class="legend-row"><div class="swatch" style="background:${kvColor(kv)}"></div>${fmt(kv, 0)} kV</div>`).join("");
    } else if (state.color === "loading") {
      html += `<div style="color:var(--muted)">Line / transformer loading vs. PATL rating</div>
        <div class="gradient" style="background:linear-gradient(90deg,${LOADING_STOPS.map(([t, c]) => `${c} ${t * 100}%`).join(",")})"></div>
        <div class="gradient-labels"><span>0 %</span><span>50 %</span><span>75 %</span><span>≥100 %</span></div>`;
    } else {
      html += `<div style="color:var(--muted)">Bus voltage magnitude</div>
        <div class="gradient" style="background:linear-gradient(90deg,${VPU_STOPS.map(([t, c]) => `${c} ${t * 100}%`).join(",")})"></div>
        <div class="gradient-labels"><span>0.90</span><span>0.95</span><span>1.00</span><span>1.05</span><span>1.10 pu</span></div>`;
    }
    if (state.view === "geo") {
      html += `<div class="legend-row" style="margin-top:8px"><div class="dot" style="background:${COLORS.kv110};box-shadow:0 0 0 3px ${COLORS.gen}"></div>Substation with generation</div>`;
    } else {
      const sym = (svg, label) => `<div class="legend-row"><span style="width:26px;display:inline-flex;justify-content:center">${svg}</span>${label}</div>`;
      html += `<div style="margin-top:8px">${sym(ICON.gen(COLORS.gen), "Generator")}${sym(ICON.load(COLORS.load), "Load")}${sym(ICON.cap(COLORS.shunt), "Capacitor")}${sym(ICON.reactor(COLORS.shunt), "Reactor")}${sym(ICON.trafo(COLORS.kv380, COLORS.kv110), "Transformer")}</div>`;
    }
    if (state.view === "sld") {
      html += `<div style="color:var(--muted);margin-top:6px">${state.sldLayout === "auto"
        ? "Structured single-line diagram generated from the topology; geographic positions are not used."
        : "Diagram layout from the file (DL profile)."}</div>`;
    }
    if (m.totals.hasSV) html += `<div style="color:var(--muted);margin-top:6px">Moving dots show the direction of active power flow; faster means higher loading.</div>`;
    $("#legend").innerHTML = html;
  }

  function buildSearch(m) {
    const items = [...m.substations, ...m.lines, ...m.trafos, ...m.gens, ...m.loads, ...m.shunts, ...m.buses];
    state.searchIndex = new Map(items.map((e) => [`${e.name} · ${KIND_LABEL[e.kind]}`, e]));
    $("#searchList").innerHTML = [...state.searchIndex.keys()].map((k) => `<option value="${esc(k)}">`).join("");
  }
  $("#search").addEventListener("change", (ev) => {
    const ent = state.searchIndex && (state.searchIndex.get(ev.target.value)
      || [...state.searchIndex.entries()].find(([k]) => k.toLowerCase().startsWith(ev.target.value.toLowerCase()))?.[1]);
    if (ent) { select(ent); ev.target.blur(); } else toast("No match");
  });

  // --- Views --------------------------------------------------------------------------
  function fit(view) {
    const m = state.model;
    const pts = view === "geo"
      ? [...m.substations.filter((s) => s.geo).map((s) => s.geo[0]), ...m.lines.flatMap((c) => c.geo || [])]
      : [...m.buses.flatMap((b) => b.dl || []), ...m.substations.flatMap((s) => s.box || [])];
    if (pts.length) maps[view].fitBounds(L.latLngBounds(toLatLngs(pts, view)).pad(0.05), { paddingBottomRight: [340, 0] });
    state.fitted[view] = true;
    if (view === "sld") updateSldDetail();
  }

  function setView(view) {
    if (!state.model) return;
    if ((view === "geo" && !state.model.hasGeo) || (view === "sld" && !state.model.hasDiagram)) return;
    state.view = view;
    document.querySelectorAll("#viewSwitch button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    $("#geoMap").classList.toggle("hidden", view !== "geo");
    $("#sldMap").classList.toggle("hidden", view !== "sld");
    $("#basemapField").style.display = view === "geo" ? "" : "none";
    $("#layoutField").style.display = view === "sld" && state.model.hasFileDiagram ? "" : "none";
    maps[view].invalidateSize();
    if (!state.fitted[view]) fit(view);
    if (state.selected) { zoomTo(state.selected, view); select(state.selected, false); }
    renderLegend();
  }
  document.querySelectorAll("#viewSwitch button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));

  // Equipment symbols scale with the diagram so they always fit their bays; far out
  // they are hidden, and in large diagrams the substation names too.
  function updateSldDetail() {
    const scale = Math.pow(2, sldMap.getZoom()), c = $("#sldMap");
    c.style.setProperty("--eq-scale", Math.min(1.5, Math.max(0.3, scale)).toFixed(3));
    c.classList.toggle("sld-far", scale < 0.4);
    c.classList.toggle("sld-names-far", scale < 0.15);
  }
  sldMap.on("zoom zoomend", updateSldDetail);
  for (const map of Object.values(maps)) map.on("click", () => select(null));
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") select(null); });

  $("#colorMode").addEventListener("change", (ev) => { state.color = ev.target.value; restyle(); });
  $("#basemap").addEventListener("change", (ev) => {
    if (basemap) geoMap.removeLayer(basemap);
    basemap = BASEMAPS[ev.target.value];
    if (basemap) basemap.addTo(geoMap);
    $("#geoMap").style.background = ev.target.value === "light" ? "#e8ecef" : "#0b0f14";
  });
  const toggleClass = (id, el, cls, invert) => {
    const apply = () => el.classList.toggle(cls, invert ? !$(id).checked : $(id).checked);
    $(id).addEventListener("change", apply);
    apply();
  };
  toggleClass("#animFlows", $("main"), "animate", false);
  toggleClass("#showLabels", $("main"), "hide-labels", true);
  toggleClass("#showEquipment", $("main"), "hide-equipment", true);

  // --- Loading -----------------------------------------------------------------------
  function toast(msg, kind = "error", ms = 4500) {
    const t = $("#toast");
    t.textContent = msg;
    t.className = kind === "info" ? "info" : "";
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.add("hidden"), ms);
  }
  const busy = (on) => $("#busy").classList.toggle("hidden", !on);

  async function load(files) {
    busy(true);
    await new Promise((r) => setTimeout(r, 30));
    try {
      const model = await CIM.loadFiles(files);
      if (!model.buses.length) throw new Error("No network found (no ConnectivityNode / TopologicalNode objects).");
      show(model);
      const what = `${model.substations.length} substations, ${model.segments.length} circuits from ${model.files.length} file(s)`;
      toast(model.hasGeo ? `Loaded ${what}` : `Loaded ${what}. No geographic data (GL) in the files: the map view is not available.`,
            "info", model.hasGeo ? 3000 : 7000);
    } catch (e) {
      console.error(e);
      toast(`Could not load: ${e.message}`);
    } finally {
      busy(false);
    }
  }

  function show(m) {
    for (const v of ["geo", "sld"]) {
      groups[v].clearLayers(); labelGroups[v].clearLayers(); state.fitted[v] = false;
      if (flowLayers[v]) maps[v].removeLayer(flowLayers[v]);
      flowLayers[v] = new FlowLayer(v);
    }
    state.entities.clear();
    state.model = m;
    state.selected = null;
    $("#details").classList.add("hidden");
    document.body.classList.remove("no-model");
    if (m.hasGeo) drawGeo(m);
    prepareSld(m);
    drawSld(m);
    for (const v of ["geo", "sld"]) flowLayers[v].addTo(maps[v]);
    document.querySelector('#viewSwitch [data-view="geo"]').disabled = !m.hasGeo;
    document.querySelector('#viewSwitch [data-view="sld"]').disabled = !m.hasDiagram;
    renderSummary(m);
    buildSearch(m);
    const view = m.hasGeo ? "geo" : "sld";
    state.view = null;
    setView(view);
    // Render the other view once so switching is instant
    const other = view === "geo" ? "sld" : "geo";
    if ((other === "geo" && m.hasGeo) || (other === "sld" && m.hasDiagram)) {
      $(`#${other}Map`).classList.remove("hidden");
      maps[other].invalidateSize();
      fit(other);
      $(`#${other}Map`).classList.add("hidden");
    }
    restyle();
  }

  // --- Schematic layouts: structured (generated) or from the file's DL profile ----------
  const sldEntities = (m) => [...m.buses, ...m.segments, ...m.gens, ...m.loads, ...m.shunts, ...m.trafos];

  function prepareSld(m) {
    for (const e of sldEntities(m)) e.dlFile = e.dl;
    SLD.layout(m);
    m.hasDiagram = true;
    state.sldLayout = "auto";
    $("#sldLayout").value = "auto";
    applySldLayout(m);
  }

  function applySldLayout(m) {
    const auto = state.sldLayout === "auto" || !m.hasFileDiagram;
    for (const e of sldEntities(m)) e.dl = auto ? e.dlAuto : e.dlFile;
    for (const s of m.substations) { s.box = auto ? s.boxAuto : null; s.label = auto ? s.labelAuto : null; }
  }

  function redrawSld() {
    const m = state.model;
    groups.sld.clearLayers();
    labelGroups.sld.clearLayers();
    sldMap.removeLayer(flowLayers.sld);
    flowLayers.sld = new FlowLayer("sld");
    for (const ent of state.entities.values()) if (ent.layers) ent.layers.sld = [];
    applySldLayout(m);
    drawSld(m);
    flowLayers.sld.addTo(sldMap);
    restyle();
    fit("sld");
    drawHalo("sld");
  }
  $("#sldLayout").addEventListener("change", (ev) => { state.sldLayout = ev.target.value; if (state.model) redrawSld(); });

  async function readFiles(fileList) {
    const files = [];
    for (const f of fileList) files.push({ name: f.name, data: await f.arrayBuffer() });
    if (files.length) load(files);
  }

  const openPicker = () => $("#fileInput").click();
  $("#openBtn").addEventListener("click", openPicker);
  $("#openBtn2").addEventListener("click", openPicker);
  $("#fileInput").addEventListener("change", (ev) => { readFiles(ev.target.files); ev.target.value = ""; });

  document.querySelectorAll("[data-sample]").forEach((b) => b.addEventListener("click", async () => {
    const url = b.dataset.sample;
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      load([{ name: url.split("/").pop(), data: await res.arrayBuffer() }]);
    } catch (e) {
      toast(`Sample not available (${e.message}). Generate it with synthetic_cim_generator.py and serve the repository root over HTTP.`);
    }
  }));

  let dragDepth = 0;
  window.addEventListener("dragenter", (ev) => { ev.preventDefault(); dragDepth++; document.body.classList.add("dragging"); });
  window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); } });
  window.addEventListener("dragover", (ev) => ev.preventDefault());
  window.addEventListener("drop", (ev) => {
    ev.preventDefault();
    dragDepth = 0;
    document.body.classList.remove("dragging");
    readFiles(ev.dataTransfer.files);
  });

  // Load a sample directly via URL: index.html?file=../synthetic_grid_CGMES.zip
  const param = new URLSearchParams(location.search).get("file");
  if (param) {
    fetch(param).then((r) => (r.ok ? r.arrayBuffer() : Promise.reject(new Error(r.statusText))))
      .then((data) => load([{ name: param.split("/").pop(), data }]))
      .catch((e) => toast(`Could not fetch ${param}: ${e.message}`));
  }

  window.gridViewer = { state, select, setView };   // for debugging
})();
