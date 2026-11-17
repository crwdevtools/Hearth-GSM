// Minimal DOM builder. Children that are strings become text nodes, so content is never parsed as HTML.

export function h(tag, props = {}, ...children) {
  const el = tag === "svg" || props?.svg ? document.createElementNS("http://www.w3.org/2000/svg", tag) : document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false || key === "svg") continue;
    if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "class") {
      el.setAttribute("class", Array.isArray(value) ? value.filter(Boolean).join(" ") : value);
    } else if (key === "style" && typeof value === "object") {
      Object.assign(el.style, value);
    } else if (key === "dataset") {
      Object.assign(el.dataset, value);
    } else if (key in el && !(el instanceof SVGElement) && typeof value !== "string") {
      el[key] = value;
    } else if (key === "value" || key === "checked" || key === "selected") {
      el[key] = value;
    } else {
      el.setAttribute(key, value === true ? "" : value);
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

export function replace(el, ...children) {
  clear(el);
  return append(el, children);
}

// Authored 16px icon set, 1.6 stroke, drawn on a 16-unit grid.
const PATHS = {
  play: "M5 3.5v9l7-4.5z",
  stop: "M4.5 4.5h7v7h-7z",
  restart: "M2.75 8a5.25 5.25 0 1 0 1.6-3.77M2.75 2.5v2.25H5",
  kill: "M9 1.75 3.5 9h4L7 14.25 12.5 7h-4z",
  copy: "M5.5 5.5V3.25c0-.4.35-.75.75-.75h6.5c.4 0 .75.35.75.75v6.5c0 .4-.35.75-.75.75H10.5M3.25 5.5h6.5c.4 0 .75.35.75.75v6.5c0 .4-.35.75-.75.75h-6.5a.75.75 0 0 1-.75-.75v-6.5c0-.4.35-.75.75-.75z",
  check: "m3 8.5 3 3 7-7",
  x: "m4 4 8 8M12 4l-8 8",
  search: "M7 12.25a5.25 5.25 0 1 0 0-10.5 5.25 5.25 0 0 0 0 10.5zM14.25 14.25l-3.5-3.5",
  plus: "M8 3v10M3 8h10",
  folder: "M1.75 4.25c0-.55.45-1 1-1h3.4l1.5 1.5h5.6c.55 0 1 .45 1 1v6.5c0 .55-.45 1-1 1H2.75c-.55 0-1-.45-1-1z",
  file: "M4 1.75h5.25L12.5 5v9.25H4zM9 1.75V5.25h3.5",
  trash: "M2.75 4.25h10.5M6.25 4.25V2.75h3.5v1.5M4.25 4.25l.6 9h6.3l.6-9",
  edit: "M10.5 2.5 13.5 5.5 6 13H3v-3z",
  chevron: "m6 4 4 4-4 4",
  down: "m4 6 4 4 4-4",
  external: "M9.25 2.75h4v4M13.25 2.75 7.5 8.5M11.25 9.5v3.75h-8.5v-8.5H6.5",
  terminal: "m3 4.5 3 3-3 3M7.75 11.5h5.25",
  server: "M2.25 2.75h11.5v4.5H2.25zM2.25 8.75h11.5v4.5H2.25zM4.75 5h.01M4.75 11h.01",
  sliders: "M2.5 4.5h6M11.5 4.5h2M2.5 11.5h2M7.5 11.5h6M10 3v3M6 10v3",
  network: "M8 1.75v4.5M8 9.75v4.5M3.75 8H1.75M14.25 8h-2M5.5 5.5 4.1 4.1M11.9 11.9l-1.4-1.4M10.5 5.5l1.4-1.4M4.1 11.9l1.4-1.4M8 10a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
  box: "M8 1.75 13.75 5v6L8 14.25 2.25 11V5zM2.25 5 8 8.25 13.75 5M8 8.25v6",
  users: "M6 7.25a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM1.75 13.75c0-2.35 1.9-4.25 4.25-4.25s4.25 1.9 4.25 4.25M10.75 2.4a2.5 2.5 0 0 1 0 4.7M12.25 9.75c1.2.6 2 1.85 2 3.3v.7",
  gear: "M8 10.25a2.25 2.25 0 1 0 0-4.5 2.25 2.25 0 0 0 0 4.5zM12.9 9.75l1.1.65-1.25 2.15-1.2-.5a4.6 4.6 0 0 1-1.5.85L9.85 14.25h-2.5l-.2-1.35a4.6 4.6 0 0 1-1.5-.85l-1.2.5L3.2 10.4l1.1-.65a4.6 4.6 0 0 1 0-1.75L3.2 7.35 4.45 5.2l1.2.5a4.6 4.6 0 0 1 1.5-.85l.2-1.35h2.5l.2 1.35c.55.2 1.05.5 1.5.85l1.2-.5 1.25 2.15-1.1.65a4.6 4.6 0 0 1 0 1.75z",
  out: "M6 2.75H3.25v10.5H6M10.25 11l3-3-3-3M13 8H6.5",
  menu: "M2.5 4.5h11M2.5 8h11M2.5 11.5h11",
  alert: "M8 5.75v3M8 11.25h.01M7.13 2.25 1.6 12a1 1 0 0 0 .87 1.5h11.06a1 1 0 0 0 .87-1.5L8.87 2.25a1 1 0 0 0-1.74 0z",
  info: "M8 14.25a6.25 6.25 0 1 0 0-12.5 6.25 6.25 0 0 0 0 12.5zM8 7.5v3.5M8 5.25h.01",
  refresh: "M13.25 8a5.25 5.25 0 0 1-9.4 3.2M2.75 8a5.25 5.25 0 0 1 9.4-3.2M12.25 2.25v2.6h-2.6M3.75 13.75v-2.6h2.6",
  arrowdown: "M8 2.75v10.5M3.75 9 8 13.25 12.25 9",
  key: "M10 9.75a3.75 3.75 0 1 0-3.4-2.15L1.75 12.45v1.8h1.8l.7-.7v-1.3h1.3l.7-.7v-1.3h1.3l.55-.55c.6.25 1.25.4 1.9.4zM11 4.75h.01",
  cloud: "M4.5 12.75h7a3 3 0 0 0 .4-5.97A4.25 4.25 0 0 0 3.6 7.6a2.6 2.6 0 0 0 .9 5.15z",
  upload: "M8 10.5V2.75M4.75 6 8 2.75 11.25 6M2.75 10.5v2.75h10.5V10.5",
  layers: "M8 1.75 14.25 5 8 8.25 1.75 5zM1.75 8 8 11.25 14.25 8M1.75 11 8 14.25 14.25 11",
  clock: "M8 14.25a6.25 6.25 0 1 0 0-12.5 6.25 6.25 0 0 0 0 12.5zM8 4.5V8l2.5 1.5",
  archive: "M2 2.75h12v3H2zM3 5.75v7.5h10v-7.5M6.5 8.5h3",
  share: "M11.5 5.25a1.75 1.75 0 1 0 0-3.5 1.75 1.75 0 0 0 0 3.5zM4.5 9.75a1.75 1.75 0 1 0 0-3.5 1.75 1.75 0 0 0 0 3.5zM11.5 14.25a1.75 1.75 0 1 0 0-3.5 1.75 1.75 0 0 0 0 3.5zM6 7.1l4-2.2M6 8.9l4 2.2",
  puzzle: "M6 2.75h1.5a1.25 1.25 0 0 1 2.5 0H11.5v3a1.25 1.25 0 0 1 0 2.5v3h-3a1.25 1.25 0 0 0-2.5 0h-3v-3a1.25 1.25 0 0 0 0-2.5v-3z",
  moon: "M13.25 9.5A5.75 5.75 0 0 1 6.5 2.75a5.75 5.75 0 1 0 6.75 6.75z",
};

export function icon(name, label) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("class", "icon");
  if (label) {
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", label);
  } else {
    svg.setAttribute("aria-hidden", "true");
  }
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", PATHS[name] || PATHS.info);
  if (name === "play") path.setAttribute("fill", "currentColor");
  svg.append(path);
  return svg;
}
