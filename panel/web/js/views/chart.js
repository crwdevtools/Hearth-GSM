// Small single-series time chart: one line, recessive grid, optional limit line, crosshair tooltip.
// Two measures (CPU, memory) get two charts, never one chart with two axes.

const NS = "http://www.w3.org/2000/svg";
const el = (tag, attrs = {}) => {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
};

function niceMax(v) {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  const n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
}

/**
 * points: [{t: seconds, v: number|null}] (a null breaks the line: the server was off),
 * format: (v) => string, limit: number|null, max: fixed top of the axis (e.g. 20 for TPS),
 * from/to: the window shown in seconds (defaults to the data), marks: [{t, kind}] restart/crash ticks,
 * now: text for the headline value (defaults to the last reading), tipExtra: (point) => string.
 */
export function lineChart({ title, points: all, format, limit = null, height = 150, step = null, max = null, from = null, to = null,
  marks = [], now: nowText = null, tipExtra = null }) {
  const points = all.filter((p) => p.v != null);
  const wrap = document.createElement("figure");
  wrap.className = "chart";
  const cap = document.createElement("figcaption");
  const name = document.createElement("span");
  name.textContent = title;
  const now = document.createElement("span");
  now.className = "chart-now";
  cap.append(name, now);
  wrap.append(cap);

  if (points.length < 1 || (points.length < 2 && from == null)) {
    const empty = document.createElement("p");
    empty.className = "chart-empty";
    empty.textContent = "Not enough data yet. Stats build up while the server runs.";
    wrap.append(empty);
    return wrap;
  }
  const last = points.at(-1);
  now.textContent = nowText ?? format(last.v);

  const W = 600, H = height, L = 52, R = 10, T = 8, B = 22;
  const t0 = from ?? points[0].t, t1 = to ?? last.t;
  const peak = Math.max(limit || 0, ...points.map((p) => p.v)) * 1.05;
  // `step` rounds the axis to whole units (e.g. 1 GiB for memory); otherwise 1-2-2.5-5-10.
  const top = max ?? (step ? Math.max(step, Math.ceil(peak / step) * step) : niceMax(peak));
  const x = (t) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - R);
  const y = (v) => T + (1 - v / top) * (H - T - B);

  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart-svg", role: "img",
    "aria-label": `${title}: now ${format(last.v)}${limit ? `, limit ${format(limit)}` : ""}` });
  for (const f of [0, 0.5, 1]) {
    const v = top * f;
    svg.append(el("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "chart-rule" }));
    const label = el("text", { x: L - 8, y: y(v) + 4, class: "chart-axis", "text-anchor": "end" });
    label.textContent = format(v);
    svg.append(label);
  }
  const span = t1 - t0;
  const fmtTime = (t) => span > 2 * 86400
    ? new Date(t * 1000).toLocaleDateString([], { weekday: "short", day: "numeric" })
    : new Date(t * 1000).toLocaleTimeString([], span > 6 * 3600 ? { hour: "numeric" } : { hour: "numeric", minute: "2-digit" });
  for (const f of [0, 0.5, 1]) {
    const t = t0 + span * f;
    const label = el("text", { x: x(t), y: H - 6, class: "chart-axis",
      "text-anchor": f === 0 ? "start" : f === 1 ? "end" : "middle" });
    label.textContent = f === 1 ? "now" : fmtTime(t);
    svg.append(label);
  }
  if (limit) {
    svg.append(el("line", { x1: L, x2: W - R, y1: y(limit), y2: y(limit), class: "chart-limit" }));
    const lbl = el("text", { x: W - R, y: y(limit) - 5, class: "chart-axis", "text-anchor": "end" });
    lbl.textContent = `limit ${format(limit)}`;
    svg.append(lbl);
  }
  // Runs of readings; a null (or a gap marker) in `all` ends a run so the line breaks where the server was off.
  const runs = [];
  for (const p of all) {
    if (p.v == null) { runs.push([]); continue; }
    if (!runs.length) runs.push([]);
    runs.at(-1).push(p);
  }
  for (const run of runs.filter((r) => r.length)) {
    const d = run.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
    svg.append(el("path", { d: `${d}L${x(run.at(-1).t).toFixed(1)},${y(0)}L${x(run[0].t).toFixed(1)},${y(0)}Z`, class: "chart-area" }));
    svg.append(el("path", { d, class: "chart-line" }));
    if (run.length === 1) svg.append(el("circle", { cx: x(run[0].t), cy: y(run[0].v), r: 2.5, class: "chart-dot" }));
  }
  for (const m of marks.filter((m) => m.t >= t0 && m.t <= t1)) {
    svg.append(el("line", { x1: x(m.t), x2: x(m.t), y1: T, y2: H - B, class: `chart-mark ${m.kind}` }));
  }

  // Crosshair + tooltip; the hit area is the whole plot, bigger than the line.
  const cross = el("line", { y1: T, y2: H - B, class: "chart-cross", visibility: "hidden" });
  const dot = el("circle", { r: 4, class: "chart-dot", visibility: "hidden" });
  const hit = el("rect", { x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent" });
  svg.append(cross, dot, hit);
  const tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.hidden = true;
  wrap.append(svg, tip);

  const show = (clientX) => {
    const box = svg.getBoundingClientRect();
    const px = ((clientX - box.left) / box.width) * W;
    const t = t0 + ((px - L) / (W - L - R)) * span;
    let best = points[0];
    for (const p of points) if (Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
    cross.setAttribute("x1", x(best.t));
    cross.setAttribute("x2", x(best.t));
    dot.setAttribute("cx", x(best.t));
    dot.setAttribute("cy", y(best.v));
    cross.setAttribute("visibility", "visible");
    dot.setAttribute("visibility", "visible");
    tip.hidden = false;
    const when = new Date(best.t * 1000);
    tip.textContent = `${span > 86400 ? `${when.toLocaleDateString([], { weekday: "short" })} ` : ""}${when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} · ${format(best.v)}${tipExtra ? ` · ${tipExtra(best)}` : ""}`;
    const left = (x(best.t) / W) * box.width;
    tip.style.left = `${Math.min(Math.max(left, 60), box.width - 60)}px`;
  };
  const hide = () => {
    cross.setAttribute("visibility", "hidden");
    dot.setAttribute("visibility", "hidden");
    tip.hidden = true;
  };
  hit.addEventListener("pointermove", (e) => show(e.clientX));
  hit.addEventListener("pointerleave", hide);
  return wrap;
}
