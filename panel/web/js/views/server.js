import { del, get, patch, post, put, socket } from "../api.js";
import { h, icon, replace } from "../dom.js";
import {
  ago, busy, bytes, confirmDialog, copyButton, fail, field, mb, download, notice, promptDialog, safeUrl, status, uploadFile, statusLabel, suffixed, toast, uptime, warnIf,
} from "../ui.js";
import { sourceEditor, SOFTWARE } from "./picker.js";
import { activityTab, backupsTab, joinCheck, modsTab, playersTab, schedulesTab, sharingTab, statsSection } from "./tabs.js";
import { variableFields } from "./vars.js";

const can = (perm) => (s) => s.permissions.includes(perm);
const isJava = (s) => s.template.key === "minecraft-java";
const moddable = (s) => isJava(s) && (s.modpack || s.variables.TYPE !== "VANILLA");
const TABS = [
  ["console", "Console", "terminal", can("console")],
  ["files", "Files", "folder", can("files")],
  ["players", "Players", "users", (s) => isJava(s) && can("players")(s)],
  ["mods", "Mods", "puzzle", (s) => moddable(s) && can("settings")(s)],
  ["backups", "Backups", "archive", can("backups")],
  ["schedules", "Schedules", "clock", can("schedules")],
  ["software", "Software", "layers", (s) => isJava(s) && can("settings")(s)],
  ["properties", "Properties", "sliders", can("settings")],
  ["network", "Network", "network"],
  ["sharing", "Sharing", "share", can("owner")],
  ["activity", "Activity", "info", can("owner")],
  ["settings", "Settings", "gear"],
];

export async function renderServer(root, { params, query, me, navigate, setCrumbs }) {
  const id = Number(params[0]);
  let server;
  try {
    server = await get(`/servers/${id}`);
  } catch (err) {
    if (err.status !== 404) throw err;
    setCrumbs([{ label: "Servers", href: "#/" }, { label: "Not found" }]);
    root.append(h("div", { class: "empty" }, h("h2", {}, "Server not found"),
      h("p", {}, "It was deleted, or it isn't shared with you."), h("a", { class: "btn", href: "#/" }, "Back to servers")));
    return;
  }
  const tabs = TABS.filter(([, , , show]) => !show || show(server));
  const tabKey = tabs.some(([k]) => k === params[1]) ? params[1] : tabs[0][0];
  setCrumbs([{ label: "Servers", href: "#/" }, { label: server.name }]);

  const cleanups = [];
  const listeners = new Set(); // notified with every fresh status
  const ctx = {
    server: () => server,
    me,
    query: query || new URLSearchParams(),
    navigate,
    onStatus: (fn) => listeners.add(fn),
    async reload() {
      server = await get(`/servers/${id}`);
      head.update();
      return server;
    },
    setStatus(st) {
      server.status = { ...server.status, ...st };
      head.update();
      listeners.forEach((fn) => fn(server.status));
    },
  };

  const head = header(ctx);
  const tabBar = h("div", { class: "tabs", role: "tablist", "aria-label": "Server sections" },
    tabs.map(([key, label]) => h("a", { class: "tab", role: "tab", href: `#/servers/${id}/${key}`, "aria-selected": String(key === tabKey) }, label)));
  const panel = h("div", { role: "tabpanel" });
  root.append(head.el, tabBar, panel);

  const views = { console: consoleTab, files: filesTab, software: softwareTab, properties: propertiesTab, network: networkTab,
    settings: settingsTab, backups: backupsTab, schedules: schedulesTab, players: playersTab, mods: modsTab,
    sharing: sharingTab, activity: activityTab };
  const cleanup = await views[tabKey](panel, ctx);
  if (cleanup) cleanups.push(cleanup);
  if (tabKey === "console") {
    const stats = await statsSection(server);
    panel.append(h("div", { class: "stats-head" }, h("h2", {}, "Last 24 hours")), stats.el);
    cleanups.push(stats.stop);
  }

  // The console streams status itself; other tabs poll.
  if (tabKey !== "console") {
    const timer = setInterval(async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const fresh = await get(`/servers/${id}`);
        ctx.setStatus(fresh.status);
      } catch { /* keep last known */ }
    }, 5000);
    cleanups.push(() => clearInterval(timer));
  }
  return () => cleanups.forEach((fn) => fn());
}

// ----- header + power ---------------------------------------------------------------------------

function header(ctx) {
  const title = h("h1", {});
  const meta = h("div", { class: "meta" });
  const actions = h("div", { class: "actions" });
  const alert = h("div", {});
  const el = h("div", {}, h("div", { class: "page-head" }, h("div", { style: { minWidth: 0 } }, title, meta), actions), alert);

  const power = (action, label, kind, ic) => {
    const btn = h("button", { class: `btn ${kind}`, type: "button" }, icon(ic), label);
    btn.addEventListener("click", () => busy(btn, async () => {
      if (action === "kill" && !(await confirmDialog({ title: "Kill the server?", body: "The process stops immediately without saving. Unsaved world changes are lost.", confirm: "Kill", danger: true }))) return;
      await post(`/servers/${ctx.server().id}/power`, { action });
      ctx.setStatus({ state: { start: "starting", stop: "stopping", restart: "stopping", kill: "offline" }[action] });
    }));
    return btn;
  };

  let shown = "";
  function update() {
    const s = ctx.server();
    const st = s.status.state;
    // Status arrives every 2 s; only rebuild when something visible changed, so buttons keep
    // focus and in-flight clicks.
    const signature = JSON.stringify([s.name, s.address, st, s.status.error, s.status.restarts, s.modpack?.update?.id, s.modpack?.name, s.template.name, s.suspended, s.moving, s.maintenance_until]);
    if (signature === shown) return;
    shown = signature;
    replace(title, s.name);
    replace(meta,
      status(st),
      h("span", {}, s.modpack ? s.modpack.name : s.template.name),
      h("span", { class: "mono", style: { fontSize: "13px" } }, s.node.name),
      s.address ? h("span", { class: "cell-flex", style: { gap: "2px" } }, h("span", { class: "mono", style: { fontSize: "13px", color: "var(--fg)" } }, s.address), copyButton(s.address, "Copy address")) : null,
      ctx.me.is_admin && s.owner.id !== ctx.me.id ? h("span", { class: "faint" }, `owned by ${s.owner.username}`) : null,
      s.status.restarts > 0 ? h("span", { class: "badge warn", title: "Times Docker restarted it after a crash since it was last started" },
        `Crashed ${s.status.restarts}×`) : null,
    );
    const buttons = [];
    if (["offline", "sleeping"].includes(st)) buttons.push(power("start", "Start", "primary", "play"));
    if (["running", "starting"].includes(st)) buttons.push(power("restart", "Restart", "", "restart"), power("stop", "Stop", "", "stop"));
    if (["stopping", "starting"].includes(st)) buttons.push(power("kill", "Kill", "danger", "kill"));
    if (["install_failed", "missing"].includes(st)) {
      const btn = h("button", { class: "btn primary", type: "button" }, icon("refresh"), "Reinstall");
      btn.addEventListener("click", () => busy(btn, async () => {
        await post(`/servers/${s.id}/reinstall`);
        ctx.setStatus({ state: "installing", error: null });
        toast("Reinstalling. Server files are kept.");
      }));
      buttons.push(btn);
    }
    if (!s.permissions.includes("power") || s.moving) buttons.length = 0;
    replace(actions, buttons);
    const err = s.status.error;
    replace(alert,
      s.suspended ? notice("bad", h("span", {}, h("strong", {}, "Suspended. "), s.suspended_reason || "An admin suspended this server; it can't be started.")) : null,
      s.moving ? notice("info", h("span", {}, h("strong", {}, "Moving to another node. "), `${s.moving}…`)) : null,
      s.maintenance_until ? notice("info", h("span", {}, h("strong", {}, "Paused for maintenance "), `until ${new Date(s.maintenance_until).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}. Schedules, auto-updates, auto-sleep and crash handling wait until then.`)) : null,
      s.disk_mb && (s.status.disk || 0) >= s.disk_mb * 1048576 ? notice("bad", h("span", {}, h("strong", {}, "At its disk limit. "), "Uploads and mod installs are blocked. Delete old files or ask an admin to raise the limit.")) : null,
      st === "sleeping" ? notice("info", h("span", {}, h("strong", {}, "Sleeping. "), "Nobody was playing, so it stopped to free memory. It wakes up when someone tries to join (they rejoin a minute later), or press Start.")) : null,
      st === "restoring" ? notice("info", "Restoring a backup. The server comes back automatically when it's done.") : null,
      st === "installing" ? notice("info", "Pulling the image and creating the container. Large images and modpacks can take a few minutes.") : null,
      st === "node_offline" ? notice("bad", h("span", {}, h("strong", {}, `${s.node.name} is unreachable. `), "The server may still be running; Hearth can't see it right now.")) : null,
      err && st !== "node_offline" ? notice("bad", h("span", {}, h("strong", {}, `${statusLabel(st)}: `), err)) : null,
      s.modpack?.update ? notice("info", h("span", {}, h("strong", {}, `${s.modpack.update.name} is available. `), "Review it under Software."),
        h("a", { class: "btn sm", href: `#/servers/${s.id}/software` }, "Review")) : null,
    );
  }
  update();
  return { el, update };
}

// ----- console ----------------------------------------------------------------------------------

const ANSI = { 30: "#555", 31: "#f2585c", 32: "#45c27a", 33: "#e9a23b", 34: "#52a8ff", 35: "#c084fc", 36: "#34d1d1", 37: "#d4d4d4",
  90: "#8c8c8c", 91: "#ff7b7e", 92: "#6ee7a0", 93: "#f5c56b", 94: "#7cc0ff", 95: "#d8a8ff", 96: "#67e8e8", 97: "#ffffff" };
// Minecraft section-sign colour codes, which some servers print raw.
const MC = { 0: "#555", 1: "#5b6cff", 2: "#45c27a", 3: "#34d1d1", 4: "#f2585c", 5: "#c084fc", 6: "#e9a23b", 7: "#d4d4d4",
  8: "#8c8c8c", 9: "#7cc0ff", a: "#6ee7a0", b: "#67e8e8", c: "#ff7b7e", d: "#d8a8ff", e: "#f5c56b", f: "#ffffff" };

function ansiToNodes(text, state) {
  // eslint-disable-next-line no-control-regex
  const clean = text.replace(/\r(?!\n)/g, "").replace(/\x1b\[[0-9;?]*[A-HJKSTfhlnsu]/g, "").replace(/\x1b\][^\x07]*\x07/g, "");
  const out = [];
  // eslint-disable-next-line no-control-regex
  const re = /\x1b\[([0-9;]*)m|§([0-9a-fk-or])/gi;
  let last = 0;
  let m;
  const emit = (chunk) => {
    if (!chunk) return;
    if (!state.color && !state.bold) out.push(document.createTextNode(chunk));
    else out.push(h("span", { style: { color: state.color || null, fontWeight: state.bold ? "600" : null } }, chunk));
  };
  while ((m = re.exec(clean))) {
    emit(clean.slice(last, m.index));
    last = re.lastIndex;
    if (m[2] !== undefined) {
      const code = m[2].toLowerCase();
      if (MC[code]) state.color = MC[code];
      else if (code === "l") state.bold = true;
      else if (code === "r") { state.color = null; state.bold = false; }
      continue;
    }
    const codes = (m[1] || "0").split(";").map(Number);
    for (let i = 0; i < codes.length; i++) {
      const c = codes[i];
      if (c === 0) { state.color = null; state.bold = false; }
      else if (c === 1) state.bold = true;
      else if (c === 22) state.bold = false;
      else if (c === 39) state.color = null;
      else if (ANSI[c]) state.color = ANSI[c];
      else if (c === 38 && codes[i + 1] === 2) { state.color = `rgb(${codes[i + 2]},${codes[i + 3]},${codes[i + 4]})`; i += 4; }
      else if (c === 38 && codes[i + 1] === 5) { i += 2; }
    }
  }
  emit(clean.slice(last));
  return out;
}

async function consoleTab(panel, ctx) {
  const s = ctx.server();
  const out = h("pre", { class: "console-out", "aria-live": "off", "aria-label": "Server console output", tabindex: "0" });
  const input = h("input", { "aria-label": "Console command", placeholder: "Type a command…", autocomplete: "off", spellcheck: "false" });
  const jump = h("button", { class: "btn sm jump", type: "button" }, icon("arrowdown"), "Latest");
  const stats = h("dl", { class: "stats" });
  const found = h("pre", { class: "console-found", hidden: true, "aria-label": "Matching console lines", tabindex: "0" });
  const search = h("input", { type: "search", class: "console-search", placeholder: "Search output…", "aria-label": "Search console output", spellcheck: "false" });
  const count = h("span", { class: "faint console-count", "aria-live": "polite" });
  const logs = isJava(s)
    ? [h("button", { class: "btn sm ghost", type: "button", title: "Download logs/latest.log (includes everything since the last start)",
      onclick: () => download(`/api/servers/${s.id}/files/download?path=${encodeURIComponent("/logs/latest.log")}`) }, icon("arrowdown"), "latest.log"),
    h("a", { class: "btn sm ghost", href: `#/servers/${s.id}/files?path=${encodeURIComponent("/crash-reports")}`, title: "Open the crash-reports folder" }, icon("file"), "Crash reports")]
    : [];
  panel.append(h("div", { class: "console-grid" },
    h("div", { class: "console" },
      h("div", { class: "console-bar" }, icon("search"), search, count, h("span", { class: "grow" }), ...logs),
      out, found, jump,
      h("form", { class: "console-in", onsubmit: (e) => { e.preventDefault(); send(); } },
        h("span", { class: "prompt", "aria-hidden": "true" }, "›"), input,
        h("button", { class: "btn sm ghost", type: "submit" }, "Send"))),
    stats));

  const ansiState = { color: null, bold: false };
  let stick = true;
  const MAX_NODES = 4000;
  out.addEventListener("scroll", () => {
    stick = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
    jump.classList.toggle("show", !stick);
  });
  jump.addEventListener("click", () => { out.scrollTop = out.scrollHeight; });

  let carry = "";
  // Output is queued and drawn once per frame: the history that arrives when the console opens
  // (or a burst of log spam) appears in a single update instead of scrolling in line by line.
  let pending = [];
  let frame = 0;
  function write(text, cls) {
    pending.push([text, cls]);
    if (pending.length > MAX_NODES) pending = pending.slice(-MAX_NODES);  // tab in the background
    if (!frame) frame = requestAnimationFrame(flush);
  }
  function flush() {
    frame = 0;
    const batch = pending;
    pending = [];
    const fragment = document.createDocumentFragment();
    for (let [text, cls] of batch) {
      if (!cls) {
        // A colour code can be split across two chunks; hold back an unfinished one.
        text = carry + text.replace(/\r\n/g, "\n");
        // eslint-disable-next-line no-control-regex
        const partial = text.match(/\x1b(\[[0-9;?]*)?$|§$/);
        carry = partial ? partial[0] : "";
        if (partial) text = text.slice(0, partial.index);
      }
      fragment.append(...(cls ? [h("span", { class: cls }, text)] : ansiToNodes(text, ansiState)));
    }
    out.append(fragment);
    const extra = out.childNodes.length - MAX_NODES;
    if (extra > 0) {
      const range = document.createRange();
      range.setStartBefore(out.firstChild);
      range.setEndAfter(out.childNodes[extra - 1]);
      range.deleteContents();
    }
    if (stick) out.scrollTop = out.scrollHeight;
    if (search.value.trim()) { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 400); }
  }
  // ----- search: show only the matching lines, with the match marked -------------------------
  function runSearch() {
    const q = search.value.trim().toLowerCase();
    found.hidden = !q;
    out.hidden = !!q;
    jump.hidden = !!q;
    if (!q) { replace(count); return; }
    const lines = out.textContent.split("\n").filter((l) => l.toLowerCase().includes(q));
    replace(count, `${lines.length} ${lines.length === 1 ? "line" : "lines"}`);
    replace(found, lines.length ? lines.slice(-2000).flatMap((l) => {
      const parts = [];
      let at = 0;
      const low = l.toLowerCase();
      for (let i = low.indexOf(q); i >= 0; i = low.indexOf(q, at)) {
        parts.push(l.slice(at, i), h("mark", {}, l.slice(i, i + q.length)));
        at = i + q.length;
      }
      parts.push(l.slice(at), "\n");
      return parts;
    }) : h("span", { class: "sys" }, "No lines match."));
    found.scrollTop = found.scrollHeight;
  }
  let searchTimer = 0;
  search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 120); });
  search.addEventListener("keydown", (e) => { if (e.key === "Escape") { search.value = ""; runSearch(); input.focus(); } });

  function clearConsole() {
    pending = [];
    carry = "";
    ansiState.color = null;
    ansiState.bold = false;
    out.replaceChildren();
  }

  // ----- stats ---------------------------------------------------------------------------------
  function stat(label, value, sub, pct) {
    const level = pct > 90 ? "crit" : pct > 75 ? "hot" : "";
    return h("div", { class: "stat" }, h("dt", {}, label), h("dd", {}, value, sub ? h("small", {}, ` ${sub}`) : null),
      pct !== undefined ? h("div", { class: "meter", "aria-hidden": "true" }, h("span", { class: level, style: { width: `${Math.min(100, pct)}%` } })) : null);
  }
  function drawStats(st) {
    const live = st.state === "running";
    const memLimit = s.memory_mb * 1024 * 1024;
    const cpuLimit = s.cpu_percent || null;
    replace(stats,
      stat("CPU", live ? `${(st.cpu ?? 0).toFixed(1)}%` : "—", cpuLimit ? `/ ${cpuLimit}%` : "", live && cpuLimit ? (st.cpu / cpuLimit) * 100 : undefined),
      stat("Memory", live ? bytes(st.memory) : "—", `/ ${mb(s.memory_mb)}`, live ? (st.memory / memLimit) * 100 : undefined),
      stat("Disk", bytes(st.disk || 0), s.disk_mb ? `/ ${mb(s.disk_mb)}` : "", s.disk_mb ? ((st.disk || 0) / (s.disk_mb * 1048576)) * 100 : undefined),
      stat("Network", live ? `↓ ${bytes(st.network?.rx || 0)}` : "—", live ? `↑ ${bytes(st.network?.tx || 0)}` : ""),
      stat("Uptime", live ? uptime(st.started_at) : "—"),
    );
  }
  drawStats(s.status);
  ctx.onStatus(drawStats);

  // ----- socket --------------------------------------------------------------------------------
  let ws = null;
  let closed = false;
  let retry = 0;
  let retryTimer = null;
  // Command history survives reloads (per server, in this browser). Up/Down walk through it.
  const historyKey = `hearth.console.${s.id}`;
  let history = [];
  try { history = JSON.parse(localStorage.getItem(historyKey) || "[]").filter((x) => typeof x === "string").slice(0, 100); } catch { history = []; }
  let historyIndex = -1;
  let draft = "";

  let reconnected = false;
  function connect() {
    ws = socket(`/servers/${s.id}/console`);
    let fresh = true;
    ws.onopen = () => {
      retry = 0;
    };
    ws.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }
      if (msg.type === "log") {
        // After a reconnect the node sends its recent history again: show it in place of what's
        // on screen rather than appending a second copy.
        if (fresh && reconnected) clearConsole();
        fresh = false;
        write(msg.data);
      }
      else if (msg.type === "status") ctx.setStatus(msg.data);
      else if (msg.type === "error") write(`${msg.data}\n`, "err");
    };
    ws.onclose = () => {
      if (closed) return;
      const wait = Math.min(15000, 1000 * 2 ** retry++);
      reconnected = true;
      retryTimer = setTimeout(connect, wait);
    };
  }

  function send() {
    const command = input.value.trim();
    if (!command) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      toast("Console is reconnecting — try again in a moment.", "warn");
      return;
    }
    ws.send(JSON.stringify({ type: "command", data: command }));
    write(`› ${command}\n`, "echo");
    if (history[0] !== command) history.unshift(command);
    history.length = Math.min(history.length, 100);
    try { localStorage.setItem(historyKey, JSON.stringify(history)); } catch { /* private mode: keep it in memory */ }
    historyIndex = -1;
    input.value = "";
    stick = true;
  }
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowUp" && history.length) {
      if (historyIndex === -1) draft = input.value;
      historyIndex = Math.min(history.length - 1, historyIndex + 1);
      input.value = history[historyIndex];
      e.preventDefault();
    } else if (e.key === "ArrowDown" && historyIndex >= 0) {
      historyIndex -= 1;
      input.value = historyIndex >= 0 ? history[historyIndex] : draft;
      e.preventDefault();
    }
  });

  connect();
  input.focus({ preventScroll: true });
  return () => {
    closed = true;
    clearTimeout(retryTimer);
    cancelAnimationFrame(frame);
    ws?.close();
  };
}

// ----- files ------------------------------------------------------------------------------------

async function filesTab(panel, ctx) {
  const s = ctx.server();
  const start = ctx.query.get("path") || "/";
  let path = /^\/[^\0]*$/.test(start) && !start.split("/").includes("..") ? start : "/";
  const bar = h("div", { class: "pathbar" });
  const tbody = h("tbody", {});
  const body = h("div", {});
  const picker = h("input", { type: "file", multiple: true, hidden: true });
  const worldPicker = h("input", { type: "file", accept: ".zip,.tar.gz,.tgz", hidden: true });
  const progress = h("span", { class: "faint mono", style: { fontSize: "12.5px" }, "aria-live": "polite" });
  const isJava = s.template.key === "minecraft-java";
  const tools = h("div", { class: "toolbar" }, bar, h("span", { class: "grow" }), progress,
    h("button", { class: "btn sm", type: "button", onclick: () => picker.click() }, icon("upload"), "Upload"),
    h("button", { class: "btn sm", type: "button", onclick: () => create(false) }, icon("file"), "New file"),
    h("button", { class: "btn sm", type: "button", onclick: () => create(true) }, icon("folder"), "New folder"),
    h("button", { class: "btn sm ghost", type: "button", title: "Download this folder as a .zip",
      onclick: () => download(`/api/servers/${s.id}/files/archive?path=${encodeURIComponent(path)}`) }, icon("arrowdown"), "Zip"),
    picker, worldPicker);
  const worldTools = isJava ? h("div", { class: "toolbar world-tools" },
    h("span", { class: "muted" }, "World:"),
    h("button", { class: "btn sm", type: "button", onclick: () => download(`/api/servers/${s.id}/files/world`) }, icon("arrowdown"), "Download world"),
    h("button", { class: "btn sm", type: "button", onclick: () => worldPicker.click() }, icon("upload"), "Import world"),
    h("span", { class: "faint", style: { fontSize: "13px" } }, "Import a singleplayer world (.zip of its folder). A running server restarts onto it.")) : null;

  picker.addEventListener("change", async () => {
    const chosen = [...picker.files];
    picker.value = "";
    for (const [i, file] of chosen.entries()) {
      try {
        await uploadFile(`/api/servers/${s.id}/files/upload?path=${encodeURIComponent(join(path, file.name))}`, file,
          (f) => (progress.textContent = `${i + 1}/${chosen.length} ${file.name} ${Math.round(f * 100)}%`));
      } catch (err) { fail(err); break; }
    }
    progress.textContent = "";
    toast(`Uploaded ${chosen.length} file${chosen.length === 1 ? "" : "s"}.`);
    open(path);
  });

  worldPicker.addEventListener("change", async () => {
    const file = worldPicker.files[0];
    worldPicker.value = "";
    if (!file) return;
    const ok = await confirmDialog({ title: `Replace the world with “${file.name}”?`, confirm: "Import", danger: true,
      body: "The current world is backed up first, then replaced. If the server is running, it stops for the swap and starts again afterwards." });
    if (!ok) return;
    try {
      const { path: staging } = await post(`/servers/${s.id}/files/upload-name?filename=${encodeURIComponent(file.name)}`);
      await uploadFile(`/api/servers/${s.id}/files/upload?path=${encodeURIComponent(staging)}`, file,
        (f) => (progress.textContent = `Uploading world ${Math.round(f * 100)}%`));
      progress.textContent = "Backing up and importing…";
      await post(`/servers/${s.id}/files/world`, { upload: staging, backup_first: true });
      toast("World imported. Start the server to play it.");
      open(path);
    } catch (err) { fail(err); } finally { progress.textContent = ""; }
  });
  panel.append(
    s.template.key === "minecraft-java"
      ? notice("info", "Settings managed under Properties overwrite matching keys in server.properties each time the server starts.")
      : null,
    tools, worldTools, body);

  const join = (dir, name) => (dir.endsWith("/") ? dir : `${dir}/`) + name;

  function drawBar() {
    const parts = path.split("/").filter(Boolean);
    replace(bar,
      h("button", { type: "button", onclick: () => open("/") }, "server"),
      parts.map((p, i) => [h("span", { class: "sep" }, "/"),
        h("button", { type: "button", onclick: () => open(`/${parts.slice(0, i + 1).join("/")}`) }, p)]));
  }

  async function open(dir) {
    path = dir;
    drawBar();
    replace(body, h("div", { class: "table-wrap" }, h("table", { class: "board" },
      h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", { class: "right hide-sm" }, "Size"), h("th", { class: "right hide-sm" }, "Modified"), h("th", {}, h("span", { class: "sr-only" }, "Actions")))),
      tbody)));
    replace(tbody, h("tr", {}, h("td", { colspan: 4 }, h("div", { class: "skeleton", style: { width: "30%" } }))));
    try {
      const entries = await get(`/servers/${s.id}/files`, { path });
      if (!entries.length) {
        replace(tbody, h("tr", {}, h("td", { colspan: 4, class: "muted" }, path === "/" ? "No files yet. They appear after the server's first start." : "This folder is empty.")));
        return;
      }
      replace(tbody, path !== "/" ? h("tr", { class: "row", onclick: () => open(path.split("/").slice(0, -1).join("/") || "/") },
        h("td", { colspan: 4 }, h("span", { class: "file-name" }, icon("folder"), ".."))) : null,
      entries.map((e) => {
        const full = join(path, e.name);
        return h("tr", { class: "row", tabindex: "0", onclick: () => (e.is_dir ? open(full) : edit(full)),
          onkeydown: (ev) => ev.key === "Enter" && (e.is_dir ? open(full) : edit(full)) },
        h("td", {}, h("span", { class: "file-name" }, icon(e.is_dir ? "folder" : "file"), e.name)),
        h("td", { class: "right figure hide-sm faint" }, e.is_dir ? "—" : bytes(e.size)),
        h("td", { class: "right figure hide-sm faint" }, ago(new Date(e.modified * 1000).toISOString())),
        h("td", { style: { width: "112px" } }, h("div", { class: "row-actions" },
          e.is_dir ? null : h("button", { class: "btn sm ghost icon-only", type: "button", title: "Download", "aria-label": `Download ${e.name}`, onclick: (ev) => { ev.stopPropagation(); download(`/api/servers/${s.id}/files/download?path=${encodeURIComponent(full)}`); } }, icon("arrowdown")),
          h("button", { class: "btn sm ghost icon-only", type: "button", title: "Rename", "aria-label": `Rename ${e.name}`, onclick: (ev) => { ev.stopPropagation(); rename(full, e.name); } }, icon("edit")),
          h("button", { class: "btn sm ghost icon-only", type: "button", title: "Delete", "aria-label": `Delete ${e.name}`, onclick: (ev) => { ev.stopPropagation(); remove(full, e); } }, icon("trash")))));
      }));
    } catch (err) {
      replace(tbody, h("tr", {}, h("td", { colspan: 4 }, err.status === 404 && path !== "/"
        ? h("span", { class: "muted" }, "This folder doesn't exist yet. ", h("button", { class: "link", type: "button", onclick: () => open("/") }, "Back to the server folder"))
        : notice("bad", err.message))));
    }
  }

  async function edit(file) {
    let content;
    try {
      content = (await get(`/servers/${s.id}/files/content`, { path: file })).content;
    } catch (err) {
      return fail(err);
    }
    const area = h("textarea", { class: "editor", spellcheck: "false", "aria-label": `Editing ${file}` }, content);
    const save = h("button", { class: "btn primary", type: "button" }, "Save");
    save.addEventListener("click", () => busy(save, async () => {
      await put(`/servers/${s.id}/files/content`, { path: file, content: area.value });
      toast(`Saved ${file.split("/").pop()}.`);
    }));
    area.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save.click(); }
      if (e.key === "Tab") { e.preventDefault(); area.setRangeText("    ", area.selectionStart, area.selectionEnd, "end"); }
    });
    replace(body, h("div", { class: "section-body" },
      h("div", { class: "toolbar" }, h("span", { class: "mono" }, file), h("span", { class: "grow" }),
        h("span", { class: "faint", style: { fontSize: "12.5px" } }, "⌘S / Ctrl+S saves"),
        h("button", { class: "btn ghost", type: "button", onclick: () => open(path) }, "Close"), save),
      area));
    area.focus();
  }

  async function create(dir) {
    const name = await promptDialog({ title: dir ? "New folder" : "New file", label: "Name", confirm: "Create" });
    if (!name) return;
    if (name.includes("/") || name === "." || name === "..") return toast("Use a plain name without slashes.", "warn");
    try {
      if (dir) await post(`/servers/${s.id}/files/mkdir`, { path: join(path, name) });
      else await put(`/servers/${s.id}/files/content`, { path: join(path, name), content: "" });
      dir ? open(path) : edit(join(path, name));
    } catch (err) { fail(err); }
  }

  async function rename(full, current) {
    const name = await promptDialog({ title: `Rename ${current}`, label: "New name", value: current, confirm: "Rename" });
    if (!name || name === current) return;
    if (name.includes("/")) return toast("Use a plain name without slashes.", "warn");
    try {
      await post(`/servers/${s.id}/files/rename`, { source: full, target: join(path, name) });
      open(path);
    } catch (err) { fail(err); }
  }

  async function remove(full, entry) {
    const ok = await confirmDialog({ title: `Delete ${entry.name}?`, body: entry.is_dir ? "The folder and everything in it is removed from the node. This can't be undone." : "The file is removed from the node. This can't be undone.", confirm: "Delete", danger: true });
    if (!ok) return;
    try {
      await del(`/servers/${s.id}/files`, { path: full });
      toast(`Deleted ${entry.name}.`);
      open(path);
    } catch (err) { fail(err); }
  }

  await open(path);
}

// ----- software (Minecraft Java) ----------------------------------------------------------------

async function softwareTab(panel, ctx) {
  const s = ctx.server();
  const mp = s.modpack;
  const current = mp
    ? h("div", { class: "picked" },
      safeUrl(mp.icon_url) ? h("img", { class: "pack-icon lg", src: safeUrl(mp.icon_url), alt: "" }) : h("span", { class: "pack-icon lg" }),
      h("div", { class: "body" },
        h("div", { class: "t" }, mp.name),
        h("div", { class: "faint mono", style: { fontSize: "12.5px" } }, `${mp.version_name} · MC ${mp.mc_version || "?"} · ${mp.provider}`)),
      mp.auto_update ? h("span", { class: "badge ok" }, "Auto-update on") : h("span", { class: "badge" }, "Pinned"))
    : h("div", { class: "picked" },
      h("div", { class: "body" },
        h("div", { class: "t" }, SOFTWARE.find(([k]) => k === s.variables.TYPE)?.[1] || s.variables.TYPE),
        h("div", { class: "faint mono", style: { fontSize: "12.5px" } }, `Minecraft ${s.variables.VERSION || "LATEST"}`)));

  const updateRow = h("div", {});
  function drawUpdate() {
    const m = ctx.server().modpack;
    if (!m) return;
    const check = h("button", { class: "btn sm", type: "button" }, icon("refresh"), "Check now");
    check.addEventListener("click", () => busy(check, async () => {
      const r = await post(`/servers/${s.id}/updates/check`);
      ctx.server().modpack = r.server.modpack;
      toast(r.update ? `${r.update.name} is available.` : "You're on the newest release.");
      drawUpdate();
    }));
    const children = [h("span", { class: "faint", style: { fontSize: "13px" } }, `Last checked ${ago(m.last_check)}`), check];
    if (m.update) {
      const apply = h("button", { class: "btn sm primary", type: "button" }, `Update to ${m.update.name}`);
      apply.addEventListener("click", () => busy(apply, async () => {
        if (!(await confirmDialog({ title: `Update to ${m.update.name}?`, body: "The server restarts onto the new version. Back up your world first — modpack updates can remove items or blocks.", confirm: "Update and restart" }))) return;
        await post(`/servers/${s.id}/updates/apply`);
        toast("Updating. The server restarts when ready.");
        ctx.navigate(`/servers/${s.id}/console`);
      }));
      children.push(apply);
    }
    replace(updateRow, h("div", { class: "toolbar", style: { margin: 0 } }, ...children));
  }

  const editor = sourceEditor({
    initial: mp
      ? { kind: "modpack", modpack: { ...mp }, auto_update: mp.auto_update }
      : { kind: "software", server_type: s.variables.TYPE, version: s.variables.VERSION },
    allowLatest: false,
  });
  const save = h("button", { class: "btn primary", type: "submit" }, "Apply and restart");
  const form = h("form", {},
    h("section", { class: "section" },
      h("header", {}, h("h2", {}, "Running now")),
      h("div", { class: "section-body" }, current, updateRow)),
    h("section", { class: "section" },
      h("header", {}, h("h2", {}, "Change version"), h("p", {}, "Switch software, pick any Minecraft version, or move to a different modpack version.")),
      h("div", { class: "section-body" },
        notice("warn", h("span", {}, h("strong", {}, "Back up first. "), "Downgrading or changing loaders can corrupt a world, and Hearth can't undo it.")),
        editor.el)),
    h("div", { class: "form-foot" }, h("span", { class: "note" }, "The container is rebuilt; your files stay."), save));

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const v = editor.value();
    if (!v) return toast("Choose a modpack first.", "warn");
    busy(save, async () => {
      const ok = await confirmDialog({ title: "Apply this change?", body: "The server restarts if it's running.", confirm: "Apply and restart" });
      if (!ok) return;
      warnIf(await put(`/servers/${s.id}/source`, v));
      toast("Applied. Rebuilding the server.");
      ctx.navigate(`/servers/${s.id}/console`);
    });
  });
  panel.append(form);
  drawUpdate();
}

// ----- properties -------------------------------------------------------------------------------

async function propertiesTab(panel, ctx) {
  const s = ctx.server();
  const template = s.template_detail;
  const opts = { secretsSet: s.secrets_set, lockAdminOnly: !ctx.me.is_admin };
  const props = variableFields(template, s.variables, { ...opts, groups: ["properties"] });
  const startup = variableFields(template, s.variables, { ...opts, groups: ["startup"] });
  const save = h("button", { class: "btn primary", type: "submit" }, "Save");
  const form = h("form", {},
    props.empty ? null : h("section", { class: "section" },
      h("header", {}, h("h2", {}, template.features.includes("minecraft_java") ? "server.properties" : "Game settings"),
        h("p", {}, "Applied every time the server starts.")),
      props.el),
    startup.empty ? null : h("section", { class: "section" },
      h("header", {}, h("h2", {}, "Startup"), h("p", {}, "How the container launches.")),
      startup.el),
    h("div", { class: "form-foot" }, h("span", { class: "note" }, "Saving restarts the server if it's running."), save));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    busy(save, async () => {
      const result = await patch(`/servers/${s.id}`, { variables: { ...props.values(), ...startup.values() } });
      warnIf(result);
      if (!result.warning) toast("Saved.");
      await ctx.reload();
    });
  });
  panel.append(form);
}

// ----- network ----------------------------------------------------------------------------------

async function networkTab(panel, ctx) {
  const draw = () => {
    const s = ctx.server();
    const rows = s.allocations.map((a) => h("tr", {},
      h("td", { class: "figure" }, h("span", { class: "cell-flex", style: { gap: "4px" } }, `${s.node.public_host}:${a.port}`, copyButton(`${s.node.public_host}:${a.port}`, "Copy address"))),
      h("td", { class: "figure faint hide-sm" }, a.ip),
      h("td", {}, a.is_primary ? h("span", { class: "badge ok" }, "Primary") : a.is_template ? h("span", { class: "badge" }, "Required by game") : h("span", { class: "badge" }, "Extra · TCP+UDP")),
      h("td", { class: "right" }, ctx.me.is_admin && !a.is_primary && !a.is_template
        ? h("button", { class: "btn sm ghost", type: "button", onclick: (e) => busy(e.currentTarget, async () => {
          const r = await del(`/servers/${s.id}/allocations/${a.id}`);
          Object.assign(ctx.server(), r);
          warnIf(r);
          draw();
        }) }, icon("trash"), "Remove") : null)));
    const add = ctx.me.is_admin ? h("button", { class: "btn sm", type: "button" }, icon("plus"), "Add port") : null;
    add?.addEventListener("click", () => busy(add, async () => {
      const r = await post(`/servers/${s.id}/allocations`, {});
      Object.assign(ctx.server(), r);
      warnIf(r);
      toast("Port added. The server restarts to open it.");
      draw();
    }));
    let addressForm = null;
    if (s.permissions.includes("settings")) {
      const addr = h("input", { id: "s-public", class: "mono", value: s.public_address || "", placeholder: "abc.joinmc.link:12345", maxlength: 260 });
      const save = h("button", { class: "btn", type: "submit" }, "Save address");
      addressForm = h("form", { class: "section-body" },
        h("div", { class: "toolbar", style: { margin: 0, alignItems: "end" } }, field("Tunnel or custom address", addr), save),
        h("p", { class: "hint" }, `Leave empty to show ${s.direct_address}. Using a playit.gg tunnel? Create it at playit.gg pointing at port ${s.allocations.find((a) => a.is_primary)?.port ?? ""}, then paste the address it gives you here.`));
      addressForm.addEventListener("submit", (e) => {
        e.preventDefault();
        busy(save, async () => {
          Object.assign(ctx.server(), await patch(`/servers/${s.id}`, { public_address: addr.value.trim() }));
          ctx.setStatus({});
          toast("Address saved.");
        });
      });
    }
    replace(panel,
      h("section", { class: "section" },
        h("header", {}, h("h2", {}, "Address"), h("p", {}, "What players type to connect.")),
        h("div", { class: "section-body" },
          h("div", { class: "cell-flex", style: { gap: "4px" } }, h("span", { class: "mono", style: { fontSize: "16px" } }, s.address), copyButton(s.address, "Copy address")),
          addressForm)),
      h("section", { class: "section" },
        h("header", {}, h("h2", {}, "Can friends join?")),
        joinCheck(s)),
      h("section", { class: "section" },
        h("header", {}, h("h2", {}, "Ports"), h("p", {}, "Extra ports open on TCP and UDP at the same number inside the container, for plugins like Dynmap or voice chat.")),
        h("div", { class: "section-body" },
          add ? h("div", {}, add) : null,
          h("div", { class: "table-wrap" }, h("table", { class: "board" },
            h("thead", {}, h("tr", {}, h("th", {}, "Address"), h("th", { class: "hide-sm" }, "Bind IP"), h("th", {}, "Role"), h("th", {}, h("span", { class: "sr-only" }, "Actions")))),
            h("tbody", {}, rows))))));
    if (s.template.key === "velocity" && s.permissions.includes("settings")) panel.append(proxySection(s));
  };
  draw();
}

function proxySection(s) {
  const body = h("div", { class: "section-body" }, h("p", { class: "muted" }, "Loading…"));
  const load = async () => {
    let d;
    try { d = await get(`/servers/${s.id}/proxy`); } catch (err) { return replace(body, notice("bad", err.message)); }
    if (!d.servers.length) return replace(body, h("p", { class: "muted" }, "No Paper or Purpur servers to link yet. Create one, then come back."));
    const order = d.servers.filter((x) => x.linked).map((x) => x.id);
    const boxes = d.servers.map((x) => {
      const box = h("input", { type: "checkbox", checked: x.linked, disabled: x.other_proxy });
      box.addEventListener("change", () => {
        const i = order.indexOf(x.id);
        if (box.checked && i < 0) order.push(x.id);
        if (!box.checked && i >= 0) order.splice(i, 1);
      });
      return h("label", { class: "check" }, box, h("span", {}, x.name,
        h("small", {}, `${x.node} · ${x.address}${x.other_proxy ? " · behind another proxy" : ""}`)));
    });
    const save = h("button", { class: "btn primary", type: "button" }, "Save and restart");
    save.addEventListener("click", () => busy(save, async () => {
      await put(`/servers/${s.id}/proxy`, { backends: order });
      await post(`/servers/${s.id}/power`, { action: "restart" }).catch(() => {});
      toast("Linked. Restart the backend servers too if they were running.");
      load();
    }));
    replace(body,
      h("p", { class: "hint" }, "Ticked servers join the proxy, in the order you tick them (players land on the first one). Hearth turns on Velocity's modern forwarding and switches each backend to proxy-only, so nobody can skip the proxy."),
      h("div", { style: { display: "grid", gap: "10px" } }, boxes),
      h("div", {}, save));
  };
  load();
  return h("section", { class: "section" }, h("header", {}, h("h2", {}, "Backend servers"), h("p", {}, "Which servers players reach through this proxy.")), body);
}

// ----- settings ---------------------------------------------------------------------------------

async function settingsTab(panel, ctx) {
  const s = ctx.server();
  const me = ctx.me;
  const admin = me.is_admin;
  const canSettings = s.permissions.includes("settings");
  const isOwner = s.permissions.includes("owner");
  const canResources = admin || (s.owner.id === me.id && me.max_servers > 0);
  const name = h("input", { id: "s-name", value: s.name, maxlength: 64, required: true, disabled: !canSettings });
  const description = h("input", { id: "s-desc", value: s.description || "", maxlength: 500, disabled: !canSettings });
  const memory = h("input", { id: "s-mem", type: "number", value: s.memory_mb, min: s.template_detail.min_memory_mb, step: 256, disabled: !canResources });
  const cpu = h("input", { id: "s-cpu", type: "number", value: s.cpu_percent, min: 0, step: 25, disabled: !canResources });
  const disk = h("input", { id: "s-disk", type: "number", value: s.disk_mb, min: 0, step: 1024, disabled: !canResources });
  const swap = h("input", { id: "s-swap", type: "number", value: s.swap_mb, min: 0, step: 256, disabled: !canResources });
  let owner = null;
  if (admin) {
    const users = await get("/users");
    owner = h("select", { id: "s-owner" }, users.map((u) => h("option", { value: u.id, selected: u.id === s.owner.id }, u.username)));
  }

  const saveDetails = h("button", { class: "btn primary", type: "submit", disabled: !canSettings && !canResources }, "Save details");
  const details = h("form", {},
    h("section", { class: "section" },
      h("header", {}, h("h2", {}, "Details")),
      h("div", { class: "section-body" }, field("Name", name), field("Description", description), owner ? field("Owner", owner) : null)),
    h("section", { class: "section" },
      h("header", {}, h("h2", {}, "Resources"), h("p", {}, canResources
        ? (admin ? "Changing limits restarts the server if it's running." : `Within your quota of ${me.max_memory_mb ? mb(me.max_memory_mb) : "unlimited memory"}. Saving restarts the server if it's running.`)
        : "Ask an admin to change these.")),
      h("div", { class: "section-body" }, h("div", { class: "grid-2" },
        suffixed("Memory", memory, "MB"), suffixed("CPU limit", cpu, "%", "100% = one core. 0 = no limit."),
        suffixed("Disk", disk, "MB", "0 = untracked."), suffixed("Swap", swap, "MB")))),
    h("div", { class: "form-foot" }, saveDetails));
  details.addEventListener("submit", (e) => {
    e.preventDefault();
    const body = {};
    if (canSettings) Object.assign(body, { name: name.value.trim(), description: description.value });
    if (admin) body.owner_id = Number(owner.value);
    if (canResources) {
      const res = { memory_mb: +memory.value, cpu_percent: +cpu.value, disk_mb: +disk.value, swap_mb: +swap.value };
      for (const [k, v] of Object.entries(res)) if (v !== s[k]) body[k] = v;
    }
    busy(saveDetails, async () => {
      const r = await patch(`/servers/${s.id}`, body);
      warnIf(r);
      if (!r.warning) toast("Saved.");
      await ctx.reload();
    });
  });

  // ----- integrations: Discord + auto-sleep ------------------------------------------------------
  let integrations = null;
  if (canSettings) {
    const discord = h("input", { id: "s-discord", type: "password", autocomplete: "off", class: "mono",
      placeholder: s.discord_webhook_set ? "Connected — paste a new URL to replace" : "https://discord.com/api/webhooks/…" });
    const sleepSel = h("select", { id: "s-sleep" }, [[0, "Never"], [10, "After 10 minutes empty"], [15, "After 15 minutes empty"],
      [30, "After 30 minutes empty"], [60, "After 1 hour empty"], [180, "After 3 hours empty"]]
      .map(([v, l]) => h("option", { value: v, selected: v === s.auto_stop_minutes }, l)));
    const wake = h("input", { type: "checkbox", checked: s.wake_on_join });
    const saveInt = h("button", { class: "btn primary", type: "submit" }, "Save integrations");
    const clearDiscord = s.discord_webhook_set ? h("button", { class: "btn sm ghost", type: "button" }, "Disconnect") : null;
    clearDiscord?.addEventListener("click", () => busy(clearDiscord, async () => { await patch(`/servers/${s.id}`, { discord_webhook: "" }); toast("Discord disconnected."); await ctx.reload(); ctx.navigate(`/servers/${s.id}/settings`); }));
    const java = s.template.key === "minecraft-java";
    integrations = h("form", {},
      h("section", { class: "section" },
        h("header", {}, h("h2", {}, "Discord"), h("p", {}, "Post starts, stops, crashes, updates and backups to a channel. In Discord: Channel settings → Integrations → Webhooks → New webhook → Copy URL.")),
        h("div", { class: "section-body" }, field("Webhook URL", discord), clearDiscord ? h("div", {}, clearDiscord) : null)),
      java ? h("section", { class: "section" },
        h("header", {}, h("h2", {}, "Auto-sleep"), h("p", {}, "Stop the server when nobody's playing to free memory and CPU.")),
        h("div", { class: "section-body" }, field("Sleep", sleepSel),
          h("label", { class: "check" }, wake, h("span", {}, "Wake up when someone joins",
            h("small", {}, "A tiny stand-in keeps the port open and shows “Sleeping” in the server list. Joining starts the server; players rejoin about a minute later."))))) : null,
      h("div", { class: "form-foot" }, saveInt));
    integrations.addEventListener("submit", (e) => {
      e.preventDefault();
      const body = {};
      if (discord.value.trim()) body.discord_webhook = discord.value.trim();
      if (java) Object.assign(body, { auto_stop_minutes: Number(sleepSel.value), wake_on_join: wake.checked });
      busy(saveInt, async () => {
        await patch(`/servers/${s.id}`, body);
        toast(body.discord_webhook ? "Saved. Discord will hear about the next event." : "Saved.");
        discord.value = "";
        await ctx.reload();
      });
    });
  }

  // ----- maintenance -----------------------------------------------------------------------------
  const reinstall = canSettings ? h("button", { class: "btn", type: "button" }, icon("refresh"), "Reinstall") : null;
  reinstall?.addEventListener("click", () => busy(reinstall, async () => {
    if (!(await confirmDialog({ title: "Reinstall the container?", body: "Hearth pulls the image again and recreates the container. Files and worlds are kept.", confirm: "Reinstall" }))) return;
    await post(`/servers/${s.id}/reinstall`);
    ctx.setStatus({ state: "installing" });
    toast("Reinstalling.");
  }));
  const clone = isOwner && (admin || me.max_servers > 0) ? h("button", { class: "btn", type: "button" }, icon("copy"), "Make a copy") : null;
  clone?.addEventListener("click", () => busy(clone, async () => {
    const copyName = await promptDialog({ title: "Copy this server", label: "Name for the copy", value: `${s.name} (test)`, confirm: "Copy" });
    if (!copyName) return;
    toast("Copying. Large worlds take a while.");
    const r = await post(`/servers/${s.id}/clone`, { name: copyName, copy_files: true });
    warnIf(r);
    toast(`${r.name} created on ${r.address}. It starts stopped.`);
    ctx.navigate(`/servers/${r.id}`);
  }));
  const suspend = admin ? h("button", { class: "btn", type: "button" }, icon(s.suspended ? "play" : "stop"), s.suspended ? "Unsuspend" : "Suspend") : null;
  suspend?.addEventListener("click", () => busy(suspend, async () => {
    if (s.suspended) {
      await post(`/servers/${s.id}/unsuspend`);
      toast("Unsuspended. The owner can start it again.");
    } else {
      const reason = await promptDialog({ title: `Suspend ${s.name}?`, label: "Reason shown to the owner", value: "", confirm: "Suspend" });
      if (reason === null) return;
      await post(`/servers/${s.id}/suspend`, { reason });
      toast("Suspended and stopped.");
    }
    await ctx.reload();
    ctx.navigate(`/servers/${s.id}/settings`);
  }));
  const pause = canSettings ? h("button", { class: "btn", type: "button" }, icon("clock"), s.maintenance_until ? "End maintenance pause" : "Pause for maintenance") : null;
  pause?.addEventListener("click", () => busy(pause, async () => {
    if (s.maintenance_until) {
      await post(`/servers/${s.id}/maintenance`, { minutes: 0 });
      toast("Maintenance pause ended.");
    } else {
      const minutes = await promptDialog({ title: "Pause for maintenance", label: "For how many minutes?", value: "60", confirm: "Pause",
        hint: "Schedules, modpack auto-updates, auto-sleep and crash-loop handling hold off until then." });
      if (minutes === null) return;
      const n = Math.round(Number(minutes));
      if (!(n >= 1 && n <= 10080)) return toast("Enter 1 to 10080 minutes.", "bad");
      await post(`/servers/${s.id}/maintenance`, { minutes: n });
      toast(`Paused for ${n} minutes.`);
    }
    await ctx.reload();
    ctx.navigate(`/servers/${s.id}/settings`);
  }));
  const moveBtn = admin ? h("button", { class: "btn", type: "button", disabled: !!s.moving }, icon("server"), "Move to another node") : null;
  moveBtn?.addEventListener("click", () => busy(moveBtn, async () => {
    const nodes = (await get("/nodes")).filter((n) => n.id !== s.node.id);
    if (!nodes.length) return toast("Add another node first (Admin → Nodes).", "bad");
    const select = h("select", { id: "move-node" }, nodes.map((n) => h("option", { value: n.id }, n.name)));
    const ok = await confirmDialog({ title: `Move ${s.name}?`, confirm: "Move",
      body: h("div", { style: { display: "grid", gap: "12px" } },
        h("p", {}, "The server stops, is backed up, copied to the new node and restored there, then starts again if it was running. It gets new ports on the new node, so players need the new address."),
        h("p", { class: "muted" }, "Older backups stay on the current node (ones with an off-site copy can be fetched later)."),
        field("New node", select)) });
    if (!ok) return;
    await post(`/servers/${s.id}/move`, { node_id: Number(select.value) });
    toast("Moving. This page shows progress.");
    await ctx.reload();
  }));
  const remove = isOwner && (admin || me.max_servers > 0) ? h("button", { class: "btn danger", type: "button" }, icon("trash"), "Delete server") : null;
  remove?.addEventListener("click", () => busy(remove, async () => {
    const ok = await confirmDialog({ title: `Delete ${s.name}?`, body: "The container, every file on the node (worlds included) and its backups are deleted. This can't be undone.", confirm: "Delete forever", danger: true, typeToConfirm: s.name });
    if (!ok) return;
    try {
      await del(`/servers/${s.id}`);
    } catch (err) {
      if (err.status !== 502) throw err;
      const force = await confirmDialog({ title: "Node unreachable", body: `${err.message}`, confirm: "Remove from panel", danger: true });
      if (!force) return;
      await del(`/servers/${s.id}`, { force: true });
    }
    toast(`${s.name} deleted.`);
    ctx.navigate("/");
  }));

  const specBox = h("div", {});
  const specToggle = canSettings ? h("details", {}, h("summary", { class: "muted", style: { cursor: "pointer" } }, "Show container spec"), specBox) : null;
  specToggle?.addEventListener("toggle", async () => {
    if (!specToggle.open || specBox.childNodes.length) return;
    try {
      const spec = await get(`/servers/${s.id}/spec`);
      replace(specBox, h("pre", { class: "code", style: { marginTop: "12px" } }, JSON.stringify(spec, null, 2)));
    } catch (err) { replace(specBox, notice("bad", err.message)); }
  });

  panel.append(details, integrations,
    h("section", { class: "section" },
      h("header", {}, h("h2", {}, "Maintenance"), clone ? h("p", {}, "A copy gets its own port and starts stopped: a safe place to try updates or mods.") : null),
      h("div", { class: "section-body" },
        h("div", { class: "toolbar", style: { margin: 0 } }, reinstall, clone, pause, moveBtn, suspend, remove),
        h("dl", { class: "facts" },
          h("dt", {}, "Server ID"), h("dd", {}, s.uuid),
          h("dt", {}, "Template"), h("dd", {}, s.template.key),
          h("dt", {}, "Created"), h("dd", {}, new Date(s.created_at).toLocaleString())),
        specToggle)));
}
