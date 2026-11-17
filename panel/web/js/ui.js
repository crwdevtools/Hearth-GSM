// Shared UI pieces: toasts, confirm dialog, formatters, status, copy, activity bars.

import { h, icon, replace } from "./dom.js";

// ----- formatters -------------------------------------------------------------------------------

export function bytes(n, digits = 1) {
  if (!n || n < 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  const v = n / 1024 ** i;
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(digits)} ${units[i]}`;
}

export function mb(n) {
  if (!n) return "—";
  return n >= 1024 ? `${+(n / 1024).toFixed(n % 1024 ? 1 : 0)} GB` : `${n} MB`;
}

export function compact(n) {
  if (n === null || n === undefined) return "—";
  if (n < 1000) return String(n);
  const units = [["B", 1e9], ["M", 1e6], ["K", 1e3]];
  for (const [u, v] of units) if (n >= v) return `${+(n / v).toFixed(n / v >= 100 ? 0 : 1)}${u}`;
  return String(n);
}

// Docker reports nanoseconds; keep milliseconds so every browser's Date parser accepts it.
const parseTime = (iso) => new Date(String(iso).replace(/(\.\d{3})\d+/, "$1")).getTime();

export function safeUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

export function ago(iso) {
  if (!iso) return "—";
  const s = (Date.now() - parseTime(iso)) / 1000;
  if (Number.isNaN(s)) return "—";
  if (s < 60) return "just now";
  const steps = [[60, "min"], [3600, "h"], [86400, "d"], [2592000, "mo"], [31536000, "y"]];
  let label = "min", div = 60;
  for (const [sec, l] of steps) if (s >= sec) { div = sec; label = l; }
  return `${Math.floor(s / div)} ${label} ago`;
}

export function uptime(iso) {
  if (!iso || iso.startsWith("0001")) return "—";
  const t = parseTime(iso);
  if (Number.isNaN(t)) return "—";
  let s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  const d = Math.floor(s / 86400); s -= d * 86400;
  const hr = Math.floor(s / 3600); s -= hr * 3600;
  const m = Math.floor(s / 60);
  if (d) return `${d}d ${hr}h`;
  if (hr) return `${hr}h ${m}m`;
  return `${m}m ${s % 60}s`;
}

// ----- status -----------------------------------------------------------------------------------

const STATE_LABEL = {
  running: "Running", starting: "Starting", stopping: "Stopping", offline: "Offline", installing: "Installing",
  install_failed: "Install failed", missing: "Not installed", node_offline: "Node offline", unknown: "Checking…",
  sleeping: "Sleeping", restoring: "Restoring",
};

export function statusLabel(state) {
  return STATE_LABEL[state] || state;
}

export function status(state) {
  return h("span", { class: "status", "data-state": state || "unknown" }, h("span", { class: "dot" }), statusLabel(state || "unknown"));
}

// ----- toasts -----------------------------------------------------------------------------------

const toastRoot = () => document.getElementById("toasts");

export function toast(message, kind = "ok", ms = 4200) {
  const iconName = kind === "bad" ? "alert" : kind === "warn" ? "alert" : "check";
  const el = h("div", { class: `toast ${kind}`, role: kind === "bad" ? "alert" : "status" }, icon(iconName), h("div", {}, message));
  toastRoot().append(el);
  setTimeout(() => {
    el.style.transition = "opacity .25s, transform .25s";
    el.style.opacity = "0";
    el.style.transform = "translateY(6px)";
    setTimeout(() => el.remove(), 260);
  }, ms);
}

export function fail(err) {
  if (err?.name === "AbortError") return;
  toast(err?.message || String(err), "bad", 6500);
}

export function warnIf(result) {
  if (result?.warning) toast(result.warning, "warn", 8000);
  return result;
}

// ----- confirm dialog (destructive actions only) ------------------------------------------------

export function confirmDialog({ title, body, confirm = "Confirm", danger = false, typeToConfirm = null }) {
  return new Promise((resolve) => {
    const input = typeToConfirm
      ? h("input", { class: "mono", autocomplete: "off", spellcheck: "false", "aria-label": `Type ${typeToConfirm} to confirm` })
      : null;
    const ok = h("button", { class: `btn ${danger ? "danger" : "primary"}`, type: "submit", disabled: !!typeToConfirm }, confirm);
    input?.addEventListener("input", () => (ok.disabled = input.value.trim() !== typeToConfirm));
    const dialog = h("dialog", { "aria-labelledby": "dlg-title" },
      h("form", { method: "dialog" },
        h("div", { class: "dialog-body" },
          h("h2", { id: "dlg-title" }, title),
          body ? (typeof body === "string" ? h("p", {}, body) : body) : null,
          typeToConfirm ? h("div", { class: "field" }, h("span", { class: "hint" }, "Type ", h("code", {}, typeToConfirm), " to confirm."), input) : null,
        ),
        h("div", { class: "dialog-foot" },
          h("button", { class: "btn ghost", type: "button", onclick: () => dialog.close("cancel") }, "Cancel"),
          ok,
        ),
      ),
    );
    dialog.addEventListener("close", () => {
      resolve(dialog.returnValue !== "cancel" && dialog.returnValue !== "");
      dialog.remove();
    });
    ok.addEventListener("click", () => (dialog.returnValue = "ok"));
    document.body.append(dialog);
    dialog.showModal();
    (input || ok).focus();
  });
}

export function promptDialog({ title, label, value = "", confirm = "Save", hint = null }) {
  return new Promise((resolve) => {
    const input = h("input", { class: "mono", value, autocomplete: "off", spellcheck: "false", required: true, maxlength: 255 });
    let result = null;
    const dialog = h("dialog", { "aria-labelledby": "dlg-title" },
      h("form", { method: "dialog", onsubmit: () => (result = input.value.trim() || null) },
        h("div", { class: "dialog-body" }, h("h2", { id: "dlg-title" }, title), field(label, input, hint ? { hint } : undefined)),
        h("div", { class: "dialog-foot" },
          h("button", { class: "btn ghost", type: "button", onclick: () => dialog.close() }, "Cancel"),
          h("button", { class: "btn primary", type: "submit" }, confirm))));
    dialog.addEventListener("close", () => { resolve(result); dialog.remove(); });
    document.body.append(dialog);
    dialog.showModal();
    input.select();
  });
}

// ----- small components -------------------------------------------------------------------------

export function copyButton(text, label = "Copy") {
  const btn = h("button", { class: "btn sm ghost icon-only", type: "button", title: label, "aria-label": label }, icon("copy"));
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(typeof text === "function" ? text() : text);
      replace(btn, icon("check"));
      setTimeout(() => replace(btn, icon("copy")), 1400);
    } catch {
      toast("Copy failed — select the text and copy it manually.", "warn");
    }
  });
  return btn;
}

export function activityBars(history = [], count = 40) {
  const samples = history.slice(-count);
  const peak = Math.max(100, ...samples.map((s) => s.cpu || 0));
  const bars = [];
  for (let i = 0; i < count - samples.length; i++) bars.push(h("i", { style: { height: "1px" } }));
  for (const s of samples) {
    const pct = Math.max(1, Math.round(((s.cpu || 0) / peak) * 100));
    bars.push(h("i", { class: s.cpu > 90 ? "hot" : s.cpu > 0.5 ? "on" : "", style: { height: `${pct}%` } }));
  }
  const last = samples.at(-1);
  return h("div", { class: "activity", role: "img", "aria-label": last ? `CPU ${last.cpu}% now` : "No activity yet" }, bars);
}

export async function busy(button, fn) {
  if (button.getAttribute("aria-busy") === "true") return;
  button.setAttribute("aria-busy", "true");
  try {
    return await fn();
  } catch (err) {
    fail(err);
  } finally {
    button.removeAttribute("aria-busy");
  }
}

export function field(label, control, { hint, id, error } = {}) {
  const cid = id || control.id || `f-${Math.random().toString(36).slice(2, 9)}`;
  control.id = cid;
  return h("div", { class: "field" },
    h("label", { for: cid }, label),
    control,
    hint ? h("span", { class: "hint" }, hint) : null,
    error ? h("span", { class: "error" }, error) : null,
  );
}

export function suffixed(label, input, unit, hint) {
  return h("div", { class: "field" },
    h("label", { for: input.id }, label),
    h("div", { class: "input-suffix" }, input, h("span", { "aria-hidden": "true" }, unit)),
    hint ? h("span", { class: "hint" }, hint) : null);
}

export function skeletonRows(cols, rows = 4) {
  return Array.from({ length: rows }, () =>
    h("tr", {}, Array.from({ length: cols }, (_, i) => h("td", {}, h("div", { class: "skeleton", style: { width: i === 1 ? "60%" : "50%" } })))),
  );
}

export function notice(kind, text, action) {
  return h("div", { class: `notice ${kind}`, role: kind === "bad" ? "alert" : "status" },
    icon(kind === "info" ? "info" : "alert"), h("div", { class: "grow" }, text), action || null);
}

export const WORDMARK = [
  "██╗  ██╗███████╗ █████╗ ██████╗ ████████╗██╗  ██╗",
  "██║  ██║██╔════╝██╔══██╗██╔══██╗╚══██╔══╝██║  ██║",
  "███████║█████╗  ███████║██████╔╝   ██║   ███████║",
  "██╔══██║██╔══╝  ██╔══██║██╔══██╗   ██║   ██╔══██║",
  "██║  ██║███████╗██║  ██║██║  ██║   ██║   ██║  ██║",
  "╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝",
].join("\n");

export function wordmark() {
  return h("pre", { class: "wordmark", role: "img", "aria-label": "Hearth" }, WORDMARK);
}

export function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

export function section(title, text, ...body) {
  return h("section", { class: "section" },
    h("header", {}, h("h2", {}, title), text ? h("p", {}, text) : null),
    h("div", { class: "section-body" }, body));
}

export function emptyRow(cols, text) {
  return h("tr", {}, h("td", { colspan: cols, class: "muted", style: { padding: "24px 12px" } }, text));
}

// Download through a normal navigation so the browser streams it to disk (no memory limit).
export function download(url) {
  const a = h("a", { href: url, download: "", style: { display: "none" } });
  document.body.append(a);
  a.click();
  a.remove();
}

export async function uploadFile(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onload = () => {
      if (xhr.status < 300) return resolve();
      let msg = `Upload failed (${xhr.status}).`;
      try { msg = JSON.parse(xhr.responseText).detail || msg; } catch { /* keep default */ }
      reject(new Error(msg));
    };
    xhr.onerror = () => reject(new Error("Upload failed: connection lost."));
    xhr.send(file);
  });
}
