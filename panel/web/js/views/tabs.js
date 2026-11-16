// Server tabs added with backups, schedules, players, mods, sharing and activity.

import { del, get, patch, post, put } from "../api.js";
import { h, icon, replace } from "../dom.js";
import {
  ago, busy, bytes, compact, confirmDialog, download, emptyRow, fail, field, notice, safeUrl, section,
  skeletonRows, toast, when,
} from "../ui.js";
import { lineChart } from "./chart.js";

const table = (head, body) => h("div", { class: "table-wrap" }, h("table", { class: "board" },
  h("thead", {}, h("tr", {}, head.map((t) => h("th", { scope: "col", class: t.cls }, t.label ?? t)))), body));

// ----- stats (console page) ----------------------------------------------------------------------

export async function statsSection(server) {
  const box = h("div", { class: "chart-grid" });
  const draw = async () => {
    try {
      const m = await get(`/servers/${server.id}/metrics`);
      const minutes = m.minutes.length >= 2 ? m.minutes : m.recent;
      replace(box,
        lineChart({ title: "Memory · last 24 h", points: minutes.map((p) => ({ t: p.t, v: p.mem })),
          format: (v) => bytes(v), limit: m.memory_limit, step: 1024 ** 3 }),
        lineChart({ title: "CPU · last 24 h", points: minutes.map((p) => ({ t: p.t, v: p.cpu })),
          format: (v) => `${Math.round(v)}%`, limit: m.cpu_limit }));
    } catch {
      replace(box, h("p", { class: "faint" }, "Stats are unavailable while the node is unreachable."));
    }
  };
  await draw();
  const timer = setInterval(() => document.visibilityState === "visible" && draw(), 60_000);
  return { el: box, stop: () => clearInterval(timer) };
}

// ----- can friends join? -------------------------------------------------------------------------

export function joinCheck(server) {
  const list = h("ol", { class: "checklist" });
  const run = h("button", { class: "btn", type: "button" }, icon("refresh"), "Check now");
  const draw = (data) => replace(list, data.checks.map((c) => h("li", { "data-ok": String(c.ok) },
    h("span", { class: "check-mark", "aria-hidden": "true" }, icon(c.ok === true ? "check" : c.ok === false ? "x" : "info")),
    h("div", {}, h("div", { class: "t" }, c.title), c.detail ? h("div", { class: "d" }, c.detail) : null))));
  run.addEventListener("click", () => busy(run, async () => draw(await get(`/servers/${server.id}/join-check`))));
  return h("div", { class: "section-body" },
    h("p", { class: "muted" }, "Checks each step between “server running” and “a friend can connect”, including a test from outside your network (via mcsrvstat.us)."),
    h("div", {}, run), list);
}

// ----- backups -----------------------------------------------------------------------------------

const KIND = { manual: "Manual", scheduled: "Scheduled", "pre-update": "Before update", "pre-restore": "Safety" };

export async function backupsTab(panel, ctx) {
  const s = ctx.server();
  const tbody = h("tbody", {}, skeletonRows(5, 3));
  const name = h("input", { placeholder: "Name (optional)", maxlength: 128, "aria-label": "Backup name", style: { maxWidth: "280px" } });
  const create = h("button", { class: "btn primary", type: "button" }, icon("plus"), "Back up now");
  let timer = null;
  let closed = false;  // a request in flight when the tab closes mustn't start polling again

  async function load() {
    const rows = await get(`/servers/${s.id}/backups`);
    clearTimeout(timer);
    if (closed) return;
    if (rows.some((b) => b.state === "creating")) timer = setTimeout(() => load().catch(() => {}), 3000);
    if (!rows.length) return replace(tbody, emptyRow(5, "No backups yet. Make one now, or add a nightly backup under Schedules."));
    replace(tbody, rows.map((b) => {
      const actions = h("div", { class: "row-actions", style: { opacity: 1 } });
      if (b.state === "ready") {
        const lock = h("button", { class: "btn sm ghost icon-only", type: "button", title: b.locked ? "Unlock" : "Keep forever (lock)",
          "aria-label": b.locked ? `Unlock ${b.name}` : `Lock ${b.name}`, "aria-pressed": String(b.locked) }, icon("key"));
        lock.addEventListener("click", () => busy(lock, async () => { await patch(`/servers/${s.id}/backups/${b.id}`, { locked: !b.locked }); load(); }));
        const dl = h("button", { class: "btn sm ghost icon-only", type: "button", title: "Download", "aria-label": `Download ${b.name}` }, icon("arrowdown"));
        dl.addEventListener("click", () => download(`/api/servers/${s.id}/backups/${b.id}/download`));
        const restore = h("button", { class: "btn sm", type: "button" }, "Restore");
        restore.addEventListener("click", () => busy(restore, async () => {
          const ok = await confirmDialog({ title: `Restore “${b.name}”?`, danger: true, confirm: "Restore",
            body: `The server stops and its files are replaced with this backup from ${when(b.created_at)}, then it starts again if it was running. A safety backup of the current files is taken first, so this can be undone.` });
          if (!ok) return;
          await post(`/servers/${s.id}/backups/${b.id}/restore`, { backup_first: true });
          toast("Restoring. The server restarts when it's done.");
          ctx.setStatus({ state: "restoring" });
          load();
        }));
        actions.append(restore, dl, lock);
      }
      const remove = h("button", { class: "btn sm ghost icon-only", type: "button", title: "Delete", "aria-label": `Delete ${b.name}`, disabled: b.locked || b.state === "creating" }, icon("trash"));
      remove.addEventListener("click", () => busy(remove, async () => {
        if (!(await confirmDialog({ title: `Delete “${b.name}”?`, body: "The backup file is removed from the node.", confirm: "Delete", danger: true }))) return;
        await del(`/servers/${s.id}/backups/${b.id}`);
        load();
      }));
      actions.append(remove);
      return h("tr", {},
        h("td", {}, h("div", { class: "name" }, b.name, b.locked ? h("span", { class: "badge", style: { marginLeft: "8px" } }, "Locked") : null),
          h("div", { class: "sub" }, `${KIND[b.kind] || b.kind} · ${when(b.created_at)}`)),
        h("td", {}, b.state === "ready" ? h("span", { class: "badge ok" }, "Ready")
          : b.state === "creating" ? h("span", { class: "status", "data-state": "starting" }, h("span", { class: "dot" }), "Creating")
            : h("span", { class: "badge bad", title: b.error || "" }, "Failed")),
        h("td", { class: "figure right hide-sm" }, b.state === "ready" ? bytes(b.size) : "—"),
        h("td", { class: "hide-md faint", style: { fontSize: "13px", maxWidth: "260px" } }, b.state === "failed" ? b.error : ""),
        h("td", { class: "right" }, actions));
    }));
  }

  create.addEventListener("click", () => busy(create, async () => {
    await post(`/servers/${s.id}/backups`, { name: name.value.trim() || null });
    name.value = "";
    toast("Backup started. Saving is paused briefly so the world file is consistent.");
    load();
  }));
  panel.append(
    h("div", { class: "toolbar" }, name, create, h("span", { class: "grow" }),
      h("span", { class: "faint", style: { fontSize: "13px" } }, "Hearth also backs up automatically before modpack updates, version changes and restores.")),
    table([{ label: "Backup" }, { label: "State" }, { label: "Size", cls: "right hide-sm" }, { label: "", cls: "hide-md" }, { label: h("span", { class: "sr-only" }, "Actions") }], tbody));
  await load();
  return () => { closed = true; clearTimeout(timer); };
}

// ----- schedules ---------------------------------------------------------------------------------

const PRESETS = [
  { label: "Daily restart at 4 AM", name: "Daily restart", cron: "0 4 * * *", action: "restart", warn_minutes: 5 },
  { label: "Nightly backup at 3 AM", name: "Nightly backup", cron: "0 3 * * *", action: "backup", keep_backups: 7 },
  { label: "Announcement every hour", name: "Hourly reminder", cron: "0 * * * *", action: "command", payload: "say Join our Discord!" },
  { label: "Weekly backup, Sunday 2 AM", name: "Weekly backup", cron: "0 2 * * 0", action: "backup", keep_backups: 4 },
];
const ACTION_LABEL = { restart: "Restart", start: "Start", stop: "Stop", backup: "Back up", command: "Run command" };

export async function schedulesTab(panel, ctx) {
  const s = ctx.server();
  const list = h("div", {});
  const formBox = h("div", {});

  async function load() {
    const data = await get(`/servers/${s.id}/schedules`);
    replace(list, table(
      [{ label: "Task" }, { label: "When" }, { label: "Last run", cls: "hide-sm" }, { label: h("span", { class: "sr-only" }, "Actions") }],
      h("tbody", {}, data.schedules.length ? data.schedules.map((row) => {
        const run = h("button", { class: "btn sm", type: "button" }, "Run now");
        run.addEventListener("click", () => busy(run, async () => { const r = await post(`/servers/${s.id}/schedules/${row.id}/run`); toast(r.result); load(); }));
        const edit = h("button", { class: "btn sm ghost icon-only", type: "button", "aria-label": `Edit ${row.name}`, title: "Edit" }, icon("edit"));
        edit.addEventListener("click", () => openForm(row));
        const remove = h("button", { class: "btn sm ghost icon-only", type: "button", "aria-label": `Delete ${row.name}`, title: "Delete" }, icon("trash"));
        remove.addEventListener("click", () => busy(remove, async () => {
          if (!(await confirmDialog({ title: `Delete “${row.name}”?`, confirm: "Delete", danger: true }))) return;
          await del(`/servers/${s.id}/schedules/${row.id}`);
          load();
        }));
        return h("tr", {},
          h("td", {}, h("div", { class: "name" }, row.name, row.enabled ? null : h("span", { class: "badge", style: { marginLeft: "8px" } }, "Paused")),
            h("div", { class: "sub" }, `${ACTION_LABEL[row.action]}${row.action === "command" ? `: ${row.payload}` : ""}${row.action === "backup" ? ` · keep ${row.keep_backups}` : ""}`)),
          h("td", {}, h("div", { class: "mono" }, row.cron), h("div", { class: "faint", style: { fontSize: "12.5px" } }, row.next_run ? `next ${when(row.next_run)}` : "—")),
          h("td", { class: "hide-sm faint", style: { fontSize: "13px", maxWidth: "280px" } }, row.last_run ? `${ago(row.last_run)}: ${row.last_result || ""}` : "Never"),
          h("td", { class: "right" }, h("div", { class: "row-actions", style: { opacity: 1 } }, run, edit, remove)));
      }) : emptyRow(4, "No scheduled tasks. Start from a preset below."))));
    list.prepend(h("p", { class: "faint", style: { fontSize: "13px", marginBottom: "12px" } },
      `Times are in ${data.timezone}${ctx.me.is_admin ? " (change under Admin → Settings)" : ""}.`));
  }

  function openForm(row = null, preset = null) {
    const v = { name: "", cron: "0 4 * * *", action: "restart", payload: "", warn_minutes: 5, keep_backups: 7, enabled: true, ...(preset || {}), ...(row || {}) };
    const name = h("input", { id: "sc-name", value: v.name, maxlength: 64, required: true });
    const cron = h("input", { id: "sc-cron", class: "mono", value: v.cron, maxlength: 64, required: true });
    const preview = h("span", { class: "hint" });
    const action = h("select", { id: "sc-action" }, Object.entries(ACTION_LABEL).map(([k, l]) => h("option", { value: k, selected: k === v.action }, l)));
    const payload = h("input", { id: "sc-payload", class: "mono", value: v.payload, placeholder: "say Restarting soon!", maxlength: 500 });
    const warn = h("input", { id: "sc-warn", type: "number", min: 0, max: 60, value: v.warn_minutes });
    const keep = h("input", { id: "sc-keep", type: "number", min: 1, max: 100, value: v.keep_backups });
    const enabled = h("input", { type: "checkbox", checked: v.enabled });
    const extra = h("div", { class: "grid-2" });
    const drawExtra = () => replace(extra,
      action.value === "command" ? field("Console command", payload) : null,
      ["restart", "stop"].includes(action.value) ? field("Warn players first", warn, { hint: "Minutes of in-game countdown. 0 = no warning." }) : null,
      action.value === "backup" ? field("Keep", keep, { hint: "Older scheduled backups are deleted. Locked ones are kept." }) : null);
    action.addEventListener("change", drawExtra);
    let t;
    const check = () => { clearTimeout(t); t = setTimeout(async () => {
      try {
        const r = await get(`/servers/${s.id}/schedules/preview`, { cron: cron.value });
        preview.textContent = `Next: ${r.next.map(when).join(" · ")}`;
        cron.removeAttribute("aria-invalid");
      } catch (err) { preview.textContent = err.message; cron.setAttribute("aria-invalid", "true"); }
    }, 250); };
    cron.addEventListener("input", check);
    drawExtra();
    check();
    const save = h("button", { class: "btn primary", type: "submit" }, row ? "Save task" : "Add task");
    const form = h("form", { class: "section-body", style: { marginTop: "20px" } },
      h("div", { class: "grid-2" }, field("Name", name), field("Repeat (cron)", cron, { hint: "minute hour day month weekday · e.g. 0 4 * * * = 4:00 every day" })),
      preview,
      h("div", { class: "grid-2" }, field("Action", action)),
      extra,
      h("label", { class: "check" }, enabled, h("span", {}, "Enabled")),
      h("div", { class: "toolbar", style: { margin: 0 } }, save, h("button", { class: "btn ghost", type: "button", onclick: () => replace(formBox, presetsRow()) }, "Cancel")));
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      busy(save, async () => {
        const body = { name: name.value.trim(), cron: cron.value.trim(), action: action.value, payload: payload.value,
          warn_minutes: Number(warn.value) || 0, keep_backups: Number(keep.value) || 7, enabled: enabled.checked };
        if (row) await put(`/servers/${s.id}/schedules/${row.id}`, body);
        else await post(`/servers/${s.id}/schedules`, body);
        toast(row ? "Task saved." : "Task added.");
        replace(formBox, presetsRow());
        load();
      });
    });
    replace(formBox, form);
    name.focus();
  }

  const presetsRow = () => h("div", { class: "toolbar", style: { marginTop: "20px" } },
    h("span", { class: "muted" }, "Add:"),
    PRESETS.map((p) => h("button", { class: "btn sm", type: "button", onclick: () => openForm(null, p) }, p.label)),
    h("button", { class: "btn sm ghost", type: "button", onclick: () => openForm() }, icon("plus"), "Custom"));

  panel.append(list, formBox);
  replace(formBox, presetsRow());
  await load();
}

// ----- players -----------------------------------------------------------------------------------

export async function playersTab(panel, ctx) {
  const s = ctx.server();
  const body = h("div", {});
  panel.append(body);

  async function act(action, name, extra = {}) {
    await post(`/servers/${s.id}/players`, { action, name, ...extra });
    toast("Done.");
    await load();
  }

  const nameInput = (label) => h("input", { placeholder: "Player name", pattern: "[A-Za-z0-9_]{1,16}", maxlength: 16, "aria-label": label,
    spellcheck: "false", class: "mono", required: true, style: { maxWidth: "260px" } });

  const playerList = (names, buttons, emptyText) => names.length
    ? h("ul", { class: "player-list" }, names.map((n) => h("li", {},
      h("img", { class: "avatar", src: `https://mc-heads.net/avatar/${encodeURIComponent(n)}/24`, alt: "", loading: "lazy", width: 24, height: 24,
        onerror: (e) => { e.target.removeAttribute("src"); e.target.style.visibility = "visible"; } }),
      h("span", { class: "mono" }, n), h("span", { class: "grow" }), buttons(n))))
    : h("p", { class: "faint" }, emptyText);

  const btn = (label, onClick, kind = "") => {
    const b = h("button", { class: `btn sm ${kind}`, type: "button" }, label);
    b.addEventListener("click", () => busy(b, onClick));
    return b;
  };

  async function load() {
    const d = await get(`/servers/${s.id}/players`);
    const wlName = nameInput("Add to whitelist");
    const opName = nameInput("Make operator");
    replace(body,
      d.running ? null : notice("info", "The server is offline. Lists come from its files; changes need it running."),
      section(`Online${d.running ? ` · ${d.online.length}/${d.max ?? "?"}` : ""}`, "Players on the server right now.",
        playerList(d.online, (n) => [
          btn("Kick", () => act("kick", n)),
          btn("Ban", async () => { if (await confirmDialog({ title: `Ban ${n}?`, body: "They're kicked and can't rejoin until unbanned.", confirm: "Ban", danger: true })) await act("ban", n); }, "danger"),
          d.ops.includes(n) ? null : btn("Make op", () => act("op", n)),
        ], d.running ? "Nobody is online." : "—")),
      section("Whitelist", d.whitelist_enabled ? "Only these players can join." : "The whitelist is off (turn it on under Properties). Players listed here can always join.",
        h("form", { class: "toolbar", style: { margin: 0 }, onsubmit: (e) => { e.preventDefault(); busy(e.submitter, () => act("whitelist_add", wlName.value.trim())); } },
          wlName, h("button", { class: "btn", type: "submit" }, icon("plus"), "Add")),
        playerList(d.whitelist, (n) => btn("Remove", () => act("whitelist_remove", n)), "Nobody on the whitelist.")),
      section("Operators", "Ops can use every command in game.",
        h("form", { class: "toolbar", style: { margin: 0 }, onsubmit: (e) => { e.preventDefault(); busy(e.submitter, () => act("op", opName.value.trim())); } },
          opName, h("button", { class: "btn", type: "submit" }, icon("plus"), "Add")),
        playerList(d.ops, (n) => btn("Remove", () => act("deop", n)), "No operators.")),
      section("Banned", null,
        playerList(d.banned.map((b) => b.name), (n) => [
          h("span", { class: "faint", style: { fontSize: "13px" } }, d.banned.find((b) => b.name === n)?.reason || ""),
          btn("Unban", () => act("pardon", n)),
        ], "Nobody is banned.")));
  }
  await load();
}

// ----- mods & plugins ----------------------------------------------------------------------------

export async function modsTab(panel, ctx) {
  const s = ctx.server();
  const installed = h("div", {});
  const results = h("div", {});
  const search = h("input", { type: "search", placeholder: "Search Modrinth…", "aria-label": "Search mods", "data-search": true });
  let target = null;
  let timer;

  async function loadInstalled() {
    const d = await get(`/servers/${s.id}/mods`);
    target = d.target;
    const noun = target.kind === "plugin" ? "plugins" : "mods";
    replace(installed, section(`Installed ${noun}`, `${target.folder} · ${target.loaders.join("/")} · Minecraft ${target.game_version}`,
      d.installed.length ? table([{ label: "File" }, { label: "Size", cls: "right hide-sm" }, { label: h("span", { class: "sr-only" }, "Actions") }],
        h("tbody", {}, d.installed.map((f) => {
          const rm = h("button", { class: "btn sm ghost icon-only", type: "button", "aria-label": `Remove ${f.name}`, title: "Remove" }, icon("trash"));
          rm.addEventListener("click", () => busy(rm, async () => {
            if (!(await confirmDialog({ title: `Remove ${f.name}?`, body: "Takes effect after a restart. Removing a mod a world depends on can break that world; back up first.", confirm: "Remove", danger: true }))) return;
            await del(`/servers/${s.id}/mods/${encodeURIComponent(f.name)}`);
            toast("Removed. Restart the server to unload it.");
            loadInstalled();
          }));
          return h("tr", {}, h("td", { class: "mono" }, f.name), h("td", { class: "figure right hide-sm" }, bytes(f.size)), h("td", { class: "right" }, rm));
        })))
        : h("p", { class: "faint" }, `No ${noun} yet.`)));
  }

  async function runSearch() {
    replace(results, h("div", { class: "skeleton", style: { width: "40%", marginTop: "12px" } }));
    try {
      const d = await get(`/servers/${s.id}/mods/search`, { query: search.value.trim() });
      replace(results, d.hits.length ? table([{ label: "Project" }, { label: "Downloads", cls: "right hide-sm" }, { label: "" }],
        h("tbody", {}, d.hits.map((p) => {
          const add = h("button", { class: "btn sm primary", type: "button" }, "Install");
          add.addEventListener("click", () => busy(add, async () => {
            const r = await post(`/servers/${s.id}/mods`, { project_id: p.id });
            toast(`Installed ${r.installed.join(", ")}. Restart the server to load ${r.installed.length > 1 ? "them" : "it"}.`, "ok", 7000);
            replace(add, icon("check"), "Installed");
            loadInstalled();
          }));
          return h("tr", {},
            h("td", {}, h("div", { class: "cell-flex" },
              safeUrl(p.icon_url) ? h("img", { class: "pack-icon", src: safeUrl(p.icon_url), alt: "", loading: "lazy" }) : h("span", { class: "pack-icon" }),
              h("div", { style: { minWidth: 0 } }, h("div", { class: "name" }, p.name), h("div", { class: "faint", style: { fontSize: "13px" } }, p.summary)))),
            h("td", { class: "figure right hide-sm" }, compact(p.downloads)),
            h("td", { class: "right" }, add));
        }))) : h("p", { class: "faint" }, "Nothing compatible matches that search."));
    } catch (err) { replace(results, notice("bad", err.message)); }
  }
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(runSearch, 300); });

  try {
    await loadInstalled();
  } catch (err) {
    replace(panel, notice("info", err.message));
    return;
  }
  panel.append(installed, section(`Add ${target.kind === "plugin" ? "plugins" : "mods"}`,
    "Only versions that match this server's loader and Minecraft version are shown. Required dependencies are installed too.",
    h("label", { class: "search", style: { maxWidth: "none" } }, icon("search"), search), results));
  runSearch();
}

// ----- sharing -----------------------------------------------------------------------------------

const PERM_LABEL = { console: "Console", power: "Start / stop", files: "Files", backups: "Backups", schedules: "Schedules", settings: "Settings & mods", players: "Players" };

export async function sharingTab(panel, ctx) {
  const s = ctx.server();
  const body = h("div", {});
  const username = h("input", { id: "share-user", placeholder: "Their Hearth username", autocomplete: "off", spellcheck: "false" });
  const add = h("button", { class: "btn primary", type: "submit" }, icon("plus"), "Share");
  const form = h("form", { class: "toolbar", style: { margin: 0, alignItems: "end" } }, field("Share with", username), add);

  const grid = (perms, onChange) => h("div", { class: "perm-grid" }, Object.entries(PERM_LABEL).map(([k, label]) => {
    const box = h("input", { type: "checkbox", checked: perms.includes(k) });
    box.addEventListener("change", () => onChange(k, box.checked, box));
    return h("label", { class: "check" }, box, h("span", {}, label));
  }));

  async function load() {
    const d = await get(`/servers/${s.id}/shares`);
    replace(body, d.shares.length ? d.shares.map((sh) => {
      let perms = [...sh.permissions];
      const remove = h("button", { class: "btn sm ghost", type: "button" }, "Remove access");
      remove.addEventListener("click", () => busy(remove, async () => { await del(`/servers/${s.id}/shares/${sh.id}`); load(); }));
      return h("div", { class: "share-card" },
        h("div", { class: "toolbar", style: { margin: 0 } }, h("strong", {}, sh.user.username), h("span", { class: "grow" }), remove),
        grid(perms, async (k, on, box) => {
          const next = on ? [...perms, k] : perms.filter((p) => p !== k);
          try { await put(`/servers/${s.id}/shares/${sh.id}`, { permissions: next }); perms = next; }
          catch (err) { box.checked = !on; fail(err); }
        }));
    }) : h("p", { class: "faint" }, "Not shared with anyone. Shared friends see this server on their dashboard with only the rights you tick."));
  }
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    busy(add, async () => {
      await post(`/servers/${s.id}/shares`, { username: username.value.trim(), permissions: ["console", "power"] });
      toast(`${username.value.trim()} can now use the console and start/stop. Adjust below.`);
      username.value = "";
      load();
    });
  });
  panel.append(section("Sharing", "Give friends access to this server without making them admins.", form, body));
  await load();
}

// ----- activity ----------------------------------------------------------------------------------

export function activityList(rows, { showServer = false } = {}) {
  return table([{ label: "When" }, { label: "Event" }, { label: "By", cls: "hide-sm" }],
    h("tbody", {}, rows.length ? rows.map((r) => h("tr", {},
      h("td", { class: "faint", style: { whiteSpace: "nowrap", fontSize: "13px" }, title: when(r.at) }, ago(r.at)),
      h("td", {}, h("div", { class: "mono", style: { fontSize: "13px" } }, r.action),
        h("div", { class: "faint", style: { fontSize: "13px", overflowWrap: "anywhere" } },
          showServer && r.server ? `${r.server} · ` : "", r.detail || "")),
      h("td", { class: "hide-sm" }, r.user || h("span", { class: "faint" }, "Hearth")))) : emptyRow(3, "Nothing yet.")));
}

export async function activityTab(panel, ctx) {
  panel.append(activityList(await get(`/servers/${ctx.server().id}/activity`)));
}
