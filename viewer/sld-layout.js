/*
 * Structured single-line diagram layout (EMS / SCADA overview style), computed from
 * the network topology only; geographic positions and file diagram layouts are ignored.
 *
 *  1. Substation boxes: voltage levels stacked as horizontal busbars (highest on top),
 *     transformers as vertical bays between them, generators / loads / shunts as
 *     feeder bays, line bays on the outer edge of the box.
 *  2. Placement: force-directed on the substation graph, snapped to a grid; local
 *     swaps reduce the total Manhattan line length; columns and rows are sized to fit.
 *  3. Routing: every line is routed orthogonally with A* on a routing grid, avoiding
 *     boxes and labels, penalising bends and overlaps so parallel lines spread out.
 *
 * Results are written to each entity as `dlAuto` (points in diagram units, y down)
 * and to substations as `boxAuto` / `labelAuto`.
 */
(function (global) {
  "use strict";

  const CELL = 12;          // routing grid; all geometry is aligned to it
  const BAY = 24;           // distance between feeder bays on a busbar
  const PAD = 12;           // box padding
  const STUB = 36;          // feeder length above / below a busbar
  const VLGAP = 72;         // distance between stacked busbars
  const GAP = 96;           // free channel between boxes (columns and rows)
  const CHAR_W = 7.4;       // label character width estimate (11 px bold)
  const snap = (v) => Math.ceil(v / CELL) * CELL;

  // ---------------------------------------------------------------------------
  // Nodes: substations, or single buses that belong to none
  // ---------------------------------------------------------------------------
  function buildNodes(model) {
    const nodes = [];
    const nodeOfBus = new Map();
    for (const b of model.buses) {
      const key = b.sub ? b.sub.id : b.id;
      let node = nodes.find((n) => n.key === key);
      if (!node) {
        node = { key, sub: b.sub || null, name: b.sub ? b.sub.name : b.name, buses: [], lines: [], trafos: [] };
        nodes.push(node);
      }
      node.buses.push(b);
      nodeOfBus.set(b.id, node);
    }
    for (const node of nodes) node.buses.sort((a, b) => (b.kv || 0) - (a.kv || 0));
    for (const c of model.lines) {
      const n1 = nodeOfBus.get(c.bus1.id), n2 = nodeOfBus.get(c.bus2.id);
      const ends = [{ node: n1, bus: c.bus1, remote: n2 }, { node: n2, bus: c.bus2, remote: n1 }];
      c._ends = ends;
      for (const e of ends) e.node.lines.push({ corridor: c, end: e });
    }
    for (const t of model.trafos) {
      const n = nodeOfBus.get(t.terms[0].bus.id);
      if (n && n === nodeOfBus.get(t.terms[1].bus.id)) n.trafos.push(t);
    }
    const equipment = (bus) => bus.equipment.filter((e) => e.kind === "gen" || e.kind === "load" || e.kind === "shunt")
      .sort((a, b) => ["gen", "load", "shunt"].indexOf(a.kind) - ["gen", "load", "shunt"].indexOf(b.kind));
    for (const node of nodes) {
      node.eq = new Map(node.buses.map((b) => [b.id, equipment(b)]));
      node.labelW = snap(node.name.length * CHAR_W + 10);
    }
    return { nodes, nodeOfBus };
  }

  /** Rows of bays per bus: which feeders go above and below each busbar. */
  function bayRows(node) {
    const multi = node.buses.length > 1, last = node.buses.length - 1;
    const nT = node.trafos.length;
    const rows = node.buses.map(() => ({ top: [], bottom: [] }));
    node.buses.forEach((bus, i) => {
      const lines = node.lines.filter((l) => l.end.bus === bus).map((l) => ({ kind: "line", ref: l }));
      const eq = node.eq.get(bus.id).map((e) => ({ kind: "eq", ref: e }));
      const trafos = node.trafos.map((t, k) => ({ kind: "trafo", ref: t, slot: k }))
        .filter((x) => x.ref.terms.some((tt) => tt.bus === bus));
      const r = rows[i];
      if (!multi) { r.top = lines; r.bottom = eq; }
      else if (i === 0) { r.top = [...lines, ...eq]; r.bottom = trafos; }
      else if (i === last) { r.top = trafos; r.bottom = [...lines, ...eq]; }
      else { r.top = trafos; r.bottom = [...trafos.filter((x) => x.ref.terms[0].bus === bus), ...lines, ...eq]; }
      // In substations with transformers all other bays start after the transformer slots,
      // so no feeder lines up with a transformer on the other side of a busbar
      for (const row of [r.top, r.bottom]) {
        let next = multi ? nT : 0;
        for (const x of row) if (x.kind !== "trafo") x.slot = next++;
      }
    });
    return rows;
  }

  function measure(node) {
    node.rows = bayRows(node);
    const slots = Math.max(2, ...node.rows.flatMap((r) => [...r.top, ...r.bottom].map((x) => x.slot + 1)));
    node.w = snap(2 * PAD + slots * BAY);
    const topSpace = node.rows[0].top.length ? STUB : 0;
    const bottomSpace = node.rows[node.rows.length - 1].bottom.length ? STUB : 0;
    node.bus0 = snap(PAD + topSpace);
    node.h = snap(node.bus0 + (node.buses.length - 1) * VLGAP + bottomSpace + PAD);
  }

  // ---------------------------------------------------------------------------
  // Placement
  // ---------------------------------------------------------------------------
  function forceDirected(nodes, edges) {
    const n = nodes.length, k = 1, area = n;
    const pos = nodes.map((_, i) => { const r = Math.sqrt(i + 0.5), a = i * 2.39996; return [r * Math.cos(a), r * Math.sin(a)]; });
    let temp = Math.sqrt(area) / 4;
    const iterations = n > 800 ? 150 : 300;
    for (let it = 0; it < iterations; it++) {
      const disp = pos.map(() => [0, 0]);
      for (let i = 0; i < n; i++) {
        for (let j = i + 1; j < n; j++) {
          let dx = pos[i][0] - pos[j][0], dy = pos[i][1] - pos[j][1];
          const d2 = dx * dx + dy * dy || 1e-4, f = (k * k) / d2;
          dx *= f; dy *= f;
          disp[i][0] += dx; disp[i][1] += dy; disp[j][0] -= dx; disp[j][1] -= dy;
        }
      }
      for (const { a, b, w } of edges) {
        const dx = pos[a][0] - pos[b][0], dy = pos[a][1] - pos[b][1], d = Math.hypot(dx, dy) || 1e-4, f = (d / k) * w;
        disp[a][0] -= dx * f; disp[a][1] -= dy * f; disp[b][0] += dx * f; disp[b][1] += dy * f;
      }
      for (let i = 0; i < n; i++) {
        const d = Math.hypot(disp[i][0], disp[i][1]) || 1e-4, s = Math.min(d, temp) / d;
        pos[i][0] += disp[i][0] * s; pos[i][1] += disp[i][1] * s;
      }
      temp *= 0.985;
    }
    return pos;
  }

  function placeOnGrid(nodes, edges) {
    const n = nodes.length;
    const pos = forceDirected(nodes, edges);
    const cells = Math.ceil(n * 1.5) + 1;
    const cols = Math.max(1, Math.ceil(Math.sqrt(cells * 1.5))), rows = Math.max(1, Math.ceil(cells / cols));
    const xs = pos.map((p) => p[0]), ys = pos.map((p) => p[1]);
    const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
    const target = pos.map(([x, y]) => [((x - x0) / (x1 - x0 || 1)) * (cols - 1), ((y - y0) / (y1 - y0 || 1)) * (rows - 1)]);
    const occ = new Map();   // "c,r" -> node index
    const cellOf = new Array(n);
    const degree = nodes.map(() => 0);
    for (const e of edges) { degree[e.a]++; degree[e.b]++; }
    const order = nodes.map((_, i) => i).sort((a, b) => degree[b] - degree[a]);
    for (const i of order) {
      const [tx, ty] = target[i];
      let best = null, bestD = Infinity;
      for (let rad = 0; best === null || rad <= Math.ceil(Math.sqrt(bestD)) + 1; rad++) {
        for (let c = Math.round(tx) - rad; c <= Math.round(tx) + rad; c++) {
          for (let r = Math.round(ty) - rad; r <= Math.round(ty) + rad; r++) {
            if (occ.has(`${c},${r}`)) continue;
            const d = (c - tx) ** 2 + (r - ty) ** 2;
            if (d < bestD) { bestD = d; best = [c, r]; }
          }
        }
        if (rad > cols + rows) break;
      }
      cellOf[i] = best;
      occ.set(`${best[0]},${best[1]}`, i);
    }

    // Local improvement: move / swap within a small radius if it shortens connections
    const adj = nodes.map(() => []);
    for (const e of edges) { adj[e.a].push([e.b, e.w]); adj[e.b].push([e.a, e.w]); }
    const cost = (i, cell) => adj[i].reduce((s, [j, w]) => s + w * (Math.abs(cell[0] - cellOf[j][0]) + Math.abs(cell[1] - cellOf[j][1])), 0);
    for (let pass = 0; pass < 8; pass++) {
      let improved = false;
      for (let i = 0; i < n; i++) {
        const [c0, r0] = cellOf[i];
        for (let dc = -2; dc <= 2; dc++) {
          for (let dr = -2; dr <= 2; dr++) {
            if (!dc && !dr) continue;
            const cell = [cellOf[i][0] + dc, cellOf[i][1] + dr], key = `${cell[0]},${cell[1]}`;
            const j = occ.get(key);
            const before = cost(i, cellOf[i]) + (j !== undefined ? cost(j, cellOf[j]) : 0);
            const old = cellOf[i];
            cellOf[i] = cell;
            if (j !== undefined) cellOf[j] = old;
            const after = cost(i, cell) + (j !== undefined ? cost(j, old) : 0);
            if (after < before - 1e-9) {
              occ.delete(`${old[0]},${old[1]}`);
              if (j !== undefined) occ.set(`${old[0]},${old[1]}`, j);
              occ.set(key, i);
              improved = true;
            } else {
              cellOf[i] = old;
              if (j !== undefined) cellOf[j] = cell;
            }
          }
        }
        if (cellOf[i][0] !== c0 || cellOf[i][1] !== r0) improved = true;
      }
      if (!improved) break;
    }

    // Compact: renumber used columns / rows
    const usedC = [...new Set(cellOf.map((c) => c[0]))].sort((a, b) => a - b);
    const usedR = [...new Set(cellOf.map((c) => c[1]))].sort((a, b) => a - b);
    nodes.forEach((node, i) => { node.col = usedC.indexOf(cellOf[i][0]); node.row = usedR.indexOf(cellOf[i][1]); });
    return { cols: usedC.length, rows: usedR.length };
  }

  function positionBoxes(nodes, dims) {
    const colLabel = new Array(dims.cols).fill(0), colBox = new Array(dims.cols).fill(0), rowH = new Array(dims.rows).fill(0);
    for (const n of nodes) {
      colLabel[n.col] = Math.max(colLabel[n.col], n.labelW);
      colBox[n.col] = Math.max(colBox[n.col], n.w);
      rowH[n.row] = Math.max(rowH[n.row], n.h);
    }
    const colX = [GAP], rowY = [GAP];
    for (let c = 1; c < dims.cols; c++) colX.push(colX[c - 1] + colLabel[c - 1] + colBox[c - 1] + GAP);
    for (let r = 1; r < dims.rows; r++) rowY.push(rowY[r - 1] + rowH[r - 1] + GAP);
    for (const n of nodes) {
      n.x = colX[n.col] + colLabel[n.col];
      n.y = rowY[n.row];
    }
  }

  // ---------------------------------------------------------------------------
  // Box contents
  // ---------------------------------------------------------------------------
  function busY(node, i) { return node.y + node.bus0 + i * VLGAP; }
  const slotX = (node, slot) => node.x + PAD + slot * BAY + BAY / 2;

  function layoutBox(node, nodeX) {
    // Order line bays by the position of the remote substation to reduce crossings
    for (const r of node.rows) {
      for (const row of [r.top, r.bottom]) {
        const lines = row.filter((x) => x.kind === "line");
        if (!lines.length) continue;
        const slots = lines.map((x) => x.slot).sort((a, b) => a - b);
        lines.sort((a, b) => nodeX(a.ref.end.remote) - nodeX(b.ref.end.remote));
        lines.forEach((x, k) => { x.slot = slots[k]; });
      }
    }
    const usedSlots = Math.max(2, ...node.rows.flatMap((r) => [...r.top, ...r.bottom].map((x) => x.slot + 1)));
    node.buses.forEach((bus, i) => {
      const y = busY(node, i);
      bus.dlAuto = [[node.x + PAD / 2, y], [node.x + PAD + usedSlots * BAY + PAD / 2, y]];
      const r = node.rows[i];
      for (const [row, dir] of [[r.top, -1], [r.bottom, 1]]) {
        for (const x of row) {
          const px = slotX(node, x.slot);
          if (x.kind === "eq") x.ref.dlAuto = [[px, y + dir * (STUB - 12)]];
          if (x.kind === "line") {
            // port on the box edge; routing starts one cell outside it
            const edgeY = dir < 0 ? node.y : node.y + node.h;
            x.ref.end.bayPoint = [px, y];
            x.ref.end.port = [px, edgeY];
            x.ref.end.dir = dir;
          }
        }
      }
    });
    for (const [k, t] of node.trafos.entries()) {
      const i1 = node.buses.indexOf(t.terms[0].bus), i2 = node.buses.indexOf(t.terms[1].bus);
      t.dlAuto = [[slotX(node, k), (busY(node, i1) + busY(node, i2)) / 2]];
    }
    if (node.sub) {
      node.sub.boxAuto = [[node.x, node.y], [node.x + node.w, node.y + node.h]];
      node.sub.labelAuto = [node.x - 6, node.y + node.bus0];
    }
  }

  // ---------------------------------------------------------------------------
  // Orthogonal routing (A* on grid points, state = point x direction)
  // ---------------------------------------------------------------------------
  class Heap {
    constructor() { this.k = []; this.v = []; }
    push(key, val) {
      const k = this.k, v = this.v;
      let i = k.length; k.push(key); v.push(val);
      while (i > 0) { const p = (i - 1) >> 1; if (k[p] <= key) break; k[i] = k[p]; v[i] = v[p]; i = p; }
      k[i] = key; v[i] = val;
    }
    pop() {
      const k = this.k, v = this.v, top = v[0], lastK = k.pop(), lastV = v.pop();
      if (k.length) {
        let i = 0;
        for (;;) {
          let c = 2 * i + 1;
          if (c >= k.length) break;
          if (c + 1 < k.length && k[c + 1] < k[c]) c++;
          if (k[c] >= lastK) break;
          k[i] = k[c]; v[i] = v[c]; i = c;
        }
        k[i] = lastK; v[i] = lastV;
      }
      return top;
    }
    get size() { return this.k.length; }
  }

  function makeRouter(nodes) {
    const maxX = Math.max(...nodes.map((n) => n.x + n.w)) + GAP, maxY = Math.max(...nodes.map((n) => n.y + n.h)) + GAP;
    const W = Math.ceil(maxX / CELL) + 2, H = Math.ceil(maxY / CELL) + 2;
    const blocked = new Uint8Array(W * H), near = new Uint8Array(W * H);
    const usedH = new Uint16Array(W * H), usedV = new Uint16Array(W * H);
    const mark = (x0, y0, x1, y1, arr, val) => {
      for (let gy = Math.max(0, Math.floor(y0 / CELL)); gy <= Math.min(H - 1, Math.ceil(y1 / CELL)); gy++)
        for (let gx = Math.max(0, Math.floor(x0 / CELL)); gx <= Math.min(W - 1, Math.ceil(x1 / CELL)); gx++) arr[gy * W + gx] = val;
    };
    for (const n of nodes) {
      mark(n.x - CELL, n.y - CELL, n.x + n.w + CELL, n.y + n.h + CELL, near, 1);
      mark(n.x - n.labelW, n.y + n.bus0 - CELL, n.x, n.y + n.bus0 + CELL, near, 1);
    }
    for (const n of nodes) {
      mark(n.x, n.y, n.x + n.w, n.y + n.h, blocked, 1);
      mark(n.x - n.labelW, n.y + n.bus0 - CELL / 2, n.x, n.y + n.bus0 + CELL / 2, blocked, 1);   // label
    }
    const DX = [1, -1, 0, 0], DY = [0, 0, 1, -1];
    const stamp = new Int32Array(W * H * 4), g = new Float32Array(W * H * 4), from = new Int32Array(W * H * 4);
    let gen = 0;
    const BEND = 6, OVERLAP = 14, CROSS = 1.5, NEAR = 5;

    function route(a, b, dirA, dirB) {
      // a, b: grid points just outside the ports; dirA: initial direction (2 = down, 3 = up);
      // dirB: required direction when arriving at b (towards the port)
      gen++;
      const ax = Math.round(a[0] / CELL), ay = Math.round(a[1] / CELL), bx = Math.round(b[0] / CELL), by = Math.round(b[1] / CELL);
      const m = 40;
      const wx0 = Math.max(0, Math.min(ax, bx) - m), wx1 = Math.min(W - 1, Math.max(ax, bx) + m);
      const wy0 = Math.max(0, Math.min(ay, by) - m), wy1 = Math.min(H - 1, Math.max(ay, by) + m);
      const heap = new Heap();
      const start = (ay * W + ax) * 4 + dirA;
      stamp[start] = gen; g[start] = 0; from[start] = -1;
      heap.push(Math.abs(ax - bx) + Math.abs(ay - by), start);
      const goal = by * W + bx;
      let found = -1, expansions = 0;
      while (heap.size && expansions < 400000) {
        const s = heap.pop(); expansions++;
        const p = s >> 2, d = s & 3, gs = g[s];
        if (p === goal) { found = s; break; }
        const px = p % W, py = (p / W) | 0;
        for (let nd = 0; nd < 4; nd++) {
          if ((nd ^ 1) === d && (nd >> 1) === (d >> 1)) continue;          // no U-turn
          const nx = px + DX[nd], ny = py + DY[nd];
          if (nx < wx0 || nx > wx1 || ny < wy0 || ny > wy1) continue;
          const q = ny * W + nx;
          if (blocked[q] && q !== goal) continue;
          const horiz = nd < 2;
          let cost = 1 + (nd !== d ? BEND : 0) + (near[q] ? NEAR : 0)
            + (horiz ? usedH[q] * OVERLAP + usedV[q] * CROSS : usedV[q] * OVERLAP + usedH[q] * CROSS);
          if (q === goal && nd !== dirB) cost += BEND;
          const ns = q * 4 + nd, ng = gs + cost;
          if (stamp[ns] === gen && g[ns] <= ng) continue;
          stamp[ns] = gen; g[ns] = ng; from[ns] = s;
          heap.push(ng + Math.abs(nx - bx) + Math.abs(ny - by), ns);
        }
      }
      if (found < 0) {   // fallback: simple Z route
        const midY = (a[1] + b[1]) / 2;
        return [a, [a[0], midY], [b[0], midY], b];
      }
      const cells = [];
      for (let s = found; s >= 0; s = from[s]) cells.push(s);
      cells.reverse();
      const pts = [];
      for (const s of cells) {
        const p = s >> 2, d = s & 3;
        (d < 2 ? usedH : usedV)[p]++;
        pts.push([(p % W) * CELL, ((p / W) | 0) * CELL, d]);   // d: direction of arrival
      }
      // keep only the corners
      const out = [[pts[0][0], pts[0][1]]];
      for (let i = 1; i < pts.length - 1; i++) {
        if (pts[i + 1][2] !== pts[i][2]) out.push([pts[i][0], pts[i][1]]);
      }
      out.push([pts[pts.length - 1][0], pts[pts.length - 1][1]]);
      return out;
    }
    return route;
  }

  /** Offset an orthogonal polyline sideways by d (exact for right angles). */
  function offsetOrthogonal(pts, d) {
    if (!d) return pts.map((p) => [p[0], p[1]]);
    const normals = [];
    for (let i = 0; i < pts.length - 1; i++) {
      const dx = pts[i + 1][0] - pts[i][0], dy = pts[i + 1][1] - pts[i][1], len = Math.hypot(dx, dy) || 1;
      normals.push([-dy / len, dx / len]);
    }
    return pts.map((p, i) => {
      const n1 = normals[Math.max(0, i - 1)], n2 = normals[Math.min(normals.length - 1, i)];
      const same = Math.abs(n1[0] - n2[0]) < 1e-9 && Math.abs(n1[1] - n2[1]) < 1e-9;
      const nx = same ? n1[0] : n1[0] + n2[0], ny = same ? n1[1] : n1[1] + n2[1];
      return [p[0] + nx * d, p[1] + ny * d];
    });
  }

  const dedupe = (pts) => pts.filter((p, i) => i === 0 || p[0] !== pts[i - 1][0] || p[1] !== pts[i - 1][1]);

  // ---------------------------------------------------------------------------

  function layout(model) {
    const { nodes, nodeOfBus } = buildNodes(model);
    if (!nodes.length) return;
    nodes.forEach(measure);
    const index = new Map(nodes.map((n, i) => [n, i]));
    const edgeMap = new Map();
    for (const c of model.lines) {
      const a = index.get(nodeOfBus.get(c.bus1.id)), b = index.get(nodeOfBus.get(c.bus2.id));
      if (a === b) continue;
      const key = a < b ? `${a},${b}` : `${b},${a}`;
      // backbone links pull less, so backbone substations spread out as hubs of their regions
      const w = (c.kv >= 300 ? 0.5 : 1) * Math.sqrt(c.segments.length);
      edgeMap.set(key, { a: Math.min(a, b), b: Math.max(a, b), w: (edgeMap.get(key)?.w || 0) + w });
    }
    const edges = [...edgeMap.values()];
    const dims = placeOnGrid(nodes, edges);
    positionBoxes(nodes, dims);
    const nodeX = (n) => n.col * 1000 + n.row;
    for (const n of nodes) layoutBox(n, nodeX);

    // Route corridors: backbone first, then shortest first
    const route = makeRouter(nodes);
    const corridors = [...model.lines].filter((c) => c._ends[0].port && c._ends[1].port)
      .sort((p, q) => (q.kv - p.kv) || (manhattan(p) - manhattan(q)));
    function manhattan(c) {
      const [a, b] = [c._ends[0].port, c._ends[1].port];
      return Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]);
    }
    for (const c of corridors) {
      const [e1, e2] = c._ends;
      const s1 = [e1.port[0], e1.port[1] + e1.dir * CELL], s2 = [e2.port[0], e2.port[1] + e2.dir * CELL];
      const dirA = e1.dir > 0 ? 2 : 3, dirB = e2.dir > 0 ? 3 : 2;
      const path = route(s1, s2, dirA, dirB);
      const full = dedupe([e1.bayPoint, e1.port, ...path, e2.port, e2.bayPoint]);
      const simplified = full.filter((p, i) => {
        if (i === 0 || i === full.length - 1) return true;
        const a = full[i - 1], b = full[i + 1];
        return !((a[0] === p[0] && p[0] === b[0]) || (a[1] === p[1] && p[1] === b[1]));
      });
      const n = c.segments.length;
      c.segments.forEach((s, k) => { s.dlAuto = offsetOrthogonal(simplified, (k - (n - 1) / 2) * 5); });
    }
    model.structured = true;
  }

  global.SLD = { layout };
})(window);
