// Admin area: Pterodactyl-style sidebar with overview, nodes, users, settings.

import { del, get, patch, post, put } from "../api.js";
import { h, icon, replace, appendAll } from "../dom.js";
import { busy, bytes, confirmDialog, copyButton, download, emptyRow, fail, field, mb, notice, section, skeletonRows, suffixed, toast } from "../ui.js";
import { activityList } from "./tabs.js";

const SECTIONS = [["overview", "Overview", "layers"], ["nodes", "Nodes", "server"], ["users", "Users", "users"],
  ["activity", "Activity", "info"], ["settings", "Settings", "gear"]];

export async function renderAdmin(root, ctx) {
  const [section = "overview", sub] = ctx.params;
  const key = SECTIONS.some(([k]) => k === section) ? section : "overview";
  ctx.setCrumbs([{ label: "Admin", href: "#/admin" }, { label: SECTIONS.find(([k]) => k === key)[1] }]);
  const content = h("div", { style: { minWidth: 0 } });
  appendAll(root, h("div", { class: "admin" },
    h("nav", { class: "side", "aria-label": "Admin" },
      h("span", { class: "group" }, "Admin"),
      SECTIONS.map(([k, label, ic]) => h("a", { href: `#/admin/${k}`, "aria-current": k === key ? "page" : null }, icon(ic), label))),
    content));
  if (key === "nodes" && sub === "new") return nodeCreate(content, ctx);
  if (key === "nodes" && sub) return nodeDetail(content, ctx, Number(sub));
  return { overview, nodes: nodeList, users: userList, activity, settings }[key](content, ctx);
}

function head(title, text, action) {
  return h("div", { class: "page-head" }, h("div", {}, h("h1", {}, title), text ? h("p", { class: "muted", style: { marginTop: "8px", maxWidth: "70ch" } }, text) : null),
    action ? h("div", { class: "actions" }, action) : null);
}

// ----- overview ---------------------------------------------------------------------------------

async function overview(root) {
  const data = await get("/admin/overview");
  const run = h("button", { class: "btn", type: "button" }, icon("refresh"), "Check modpack updates now");
  run.addEventListener("click", () => busy(run, async () => {
    const r = await post("/admin/updates/run");
    toast(`Checked ${r.checked} modpack server${r.checked === 1 ? "" : "s"}.`);
    replace(root);
    overview(root);
  }));
  const update = data.update ? notice("info", h("span", {}, `Hearth ${data.update.version} is available (you have ${data.version}). `,
    data.update.url ? h("a", { class: "link", href: data.update.url, target: "_blank", rel: "noopener noreferrer" }, "What's new") : null,
    " · Update with git pull, then start.sh or start.bat.")) : null;
  appendAll(root, head("Overview", "What this panel manages."), update,
    h("dl", { class: "facts", style: { fontSize: "14px" } },
      h("dt", {}, "Hearth"), h("dd", {}, `Version ${data.version}`),
      h("dt", {}, "Nodes"), h("dd", {}, h("a", { class: "link", href: "#/admin/nodes" }, data.nodes)),
      h("dt", {}, "Servers"), h("dd", {}, h("a", { class: "link", href: "#/" }, data.servers)),
      h("dt", {}, "Users"), h("dd", {}, h("a", { class: "link", href: "#/admin/users" }, data.users)),
      h("dt", {}, "Free ports"), h("dd", {}, data.allocations_free),
      h("dt", {}, "Modpack servers"), h("dd", {}, data.modpack_servers),
      h("dt", {}, "Updates waiting"), h("dd", {}, data.updates_available)),
    h("div", { style: { marginTop: "28px" } }, run));
}

// ----- nodes ------------------------------------------------------------------------------------

async function nodeList(root, { navigate }) {
  const tbody = h("tbody", {}, skeletonRows(5, 2));
  appendAll(root, 
    head("Nodes", "Machines that run servers. Each runs the Hearth agent next to Docker — your own box, a VPS, or a dedicated server from any provider.",
      h("a", { class: "btn primary", href: "#/admin/nodes/new" }, icon("plus"), "Add node")),
    h("div", { class: "table-wrap" }, h("table", { class: "board" },
      h("thead", {}, h("tr", {}, h("th", {}, "Node"), h("th", { class: "hide-md" }, "Agent"), h("th", { class: "hide-sm" }, "Memory"), h("th", { class: "hide-sm" }, "Ports free"), h("th", { class: "right" }, "Status"))),
      tbody)));
  const nodes = await get("/nodes");
  if (!nodes.length) {
    replace(tbody, h("tr", {}, h("td", { colspan: 5, class: "muted", style: { padding: "28px 12px" } }, "No nodes yet. Add the machine Hearth runs on first — it's the quickest start.")));
    return;
  }
  replace(tbody, nodes.map((n) => h("tr", { class: "row", tabindex: "0", onclick: () => navigate(`/admin/nodes/${n.id}`), onkeydown: (e) => e.key === "Enter" && navigate(`/admin/nodes/${n.id}`) },
    h("td", {}, h("div", { class: "name" }, n.name), h("div", { class: "sub" }, `${n.location || "—"}/${n.public_host}`)),
    h("td", { class: "figure hide-md faint" }, n.url),
    h("td", { class: "figure hide-sm" }, `${mb(n.memory_assigned_mb)} / ${n.memory_mb ? mb(n.memory_mb) : n.system?.memory_total ? bytes(n.system.memory_total) : "—"}`),
    h("td", { class: "figure hide-sm" }, `${n.allocations_free} / ${n.allocations_total}`),
    h("td", { class: "right" }, h("span", { class: "status", "data-state": n.online ? "running" : "node_offline", title: n.error || "" },
      h("span", { class: "dot" }), n.online ? `Online · Docker ${n.system.docker_version}${n.agent_outdated ? " · agent needs updating" : ""}` : "Unreachable")))));
}

function nodeForm(node = {}) {
  const inputs = {
    name: h("input", { id: "n-name", value: node.name || "", required: true, maxlength: 64, placeholder: "home-server" }),
    location: h("input", { id: "n-loc", value: node.location || "", maxlength: 64, placeholder: "Basement · Hetzner FSN1 · OVH BHS" }),
    url: h("input", { id: "n-url", class: "mono", value: node.url || "", required: true, placeholder: "http://agent:8080 or https://vps.example.com:8443" }),
    public_host: h("input", { id: "n-host", class: "mono", value: node.public_host || "", required: true, placeholder: "play.example.com or 203.0.113.7" }),
    memory_mb: h("input", { id: "n-mem", type: "number", min: 0, step: 1024, value: node.memory_mb ?? 0 }),
    disk_mb: h("input", { id: "n-disk", type: "number", min: 0, step: 1024, value: node.disk_mb ?? 0 }),
    allow_insecure: h("input", { id: "n-insecure", type: "checkbox" }),
  };
  const el = h("div", { class: "section-body" },
    h("div", { class: "grid-2" }, field("Name", inputs.name), field("Location", inputs.location, { hint: "Where it is or who hosts it." })),
    field("Agent URL", inputs.url, { hint: "How the panel reaches the agent. Use https:// for nodes on the internet." }),
    h("label", { class: "check", style: { marginTop: "-6px" } }, inputs.allow_insecure,
      h("span", {}, "Allow plain HTTP to an internet address (only if that machine is on a private network or VPN)")),
    field("Public address", inputs.public_host, { hint: "What players type to connect. Shown next to each server's port." }),
    h("div", { class: "grid-2" },
      suffixed("Memory to assign", inputs.memory_mb, "MB", "0 = don't limit. Stops over-committing the machine."),
      suffixed("Disk to assign", inputs.disk_mb, "MB", "0 = don't limit.")));
  const values = () => ({
    name: inputs.name.value.trim(), location: inputs.location.value.trim(), url: inputs.url.value.trim(),
    public_host: inputs.public_host.value.trim(), memory_mb: Number(inputs.memory_mb.value) || 0, disk_mb: Number(inputs.disk_mb.value) || 0,
    allow_insecure: inputs.allow_insecure.checked,
  });
  return { el, values };
}

function installBlock(command) {
  return h("div", { class: "code-wrap" }, h("pre", { class: "code" }, command), copyButton(command, "Copy command"));
}

async function nodeCreate(root, { navigate }) {
  const form = nodeForm({ url: "http://agent:8080", location: "Local" });
  const save = h("button", { class: "btn primary", type: "submit" }, "Add node");
  const el = h("form", {},
    h("section", { class: "section" }, h("header", {}, h("h2", {}, "Machine"),
      h("p", {}, "For the machine running this panel with the bundled docker-compose, keep http://agent:8080.")), form.el),
    h("div", { class: "form-foot" }, h("a", { class: "btn ghost", href: "#/admin/nodes" }, "Cancel"), save));
  el.addEventListener("submit", (e) => {
    e.preventDefault();
    busy(save, async () => {
      const node = await post("/nodes", form.values());
      replace(root,
        head(`${node.name} added`, "Run this on the machine to start its agent. The token is shown on the node page if you need it again."),
        installBlock(node.install_command),
        h("p", { class: "muted", style: { margin: "16px 0 24px" } }, "Using the bundled docker-compose on this machine? Put the token in HEARTH_AGENT_TOKEN in your .env and run docker compose up -d instead."),
        h("div", { class: "toolbar" }, h("a", { class: "btn primary", href: `#/admin/nodes/${node.id}` }, "Next: add ports")));
    });
  });
  appendAll(root, head("Add a node", "Any machine with Docker can be a node: the one running Hearth, a spare PC, or a server rented from any provider."), el);
}

async function nodeDetail(root, { navigate }, id) {
  const node = await get(`/nodes/${id}`);
  const status = node.online
    ? h("span", { class: "status", "data-state": "running" }, h("span", { class: "dot" }), `Online · agent ${node.system.version}`)
    : h("span", { class: "status", "data-state": "node_offline" }, h("span", { class: "dot" }), "Unreachable");

  const outdated = node.agent_outdated ? notice("warn", `This node runs an older agent (${node.system.version}). Some features won't work there until you update it: rebuild or pull the agent image and run the install command again.`) : null;
  const form = nodeForm(node);
  const save = h("button", { class: "btn primary", type: "submit" }, "Save");
  const edit = h("form", {}, h("section", { class: "section" }, h("header", {}, h("h2", {}, "Settings")), form.el),
    h("div", { class: "form-foot" }, save));
  edit.addEventListener("submit", (e) => {
    e.preventDefault();
    busy(save, async () => { await patch(`/nodes/${id}`, form.values()); toast("Node saved."); });
  });

  const sys = node.system;
  const system = sys
    ? h("dl", { class: "facts" },
      h("dt", {}, "Docker"), h("dd", {}, `${sys.docker_version} · ${sys.os} · ${sys.architecture}`),
      h("dt", {}, "CPU"), h("dd", {}, `${sys.cpus} cores`),
      h("dt", {}, "Memory"), h("dd", {}, `${bytes(sys.memory_total)} total · ${mb(node.memory_assigned_mb)} assigned`),
      h("dt", {}, "Disk"), h("dd", {}, `${bytes(sys.disk_free)} free of ${bytes(sys.disk_total)}`),
      sys.storage_path ? [h("dt", {}, "Server files"), h("dd", {}, sys.storage_path)] : null,
      h("dt", {}, "Containers"), h("dd", {}, `${sys.containers_running} running · ${sys.servers} Hearth servers`))
    : notice("bad", node.error || "The agent isn't responding.");

  // allocations
  const ip = h("input", { id: "a-ip", class: "mono", value: "0.0.0.0" });
  const ports = h("input", { id: "a-ports", class: "mono", placeholder: "25565-25600, 19132", required: true });
  const add = h("button", { class: "btn", type: "submit" }, icon("plus"), "Add ports");
  const allocForm = h("form", { class: "grid-3", style: { alignItems: "start" } },
    field("Bind IP", ip, { hint: "0.0.0.0 listens on every interface." }),
    field("Ports", ports, { hint: "Ranges and single ports, comma-separated." }),
    h("div", { class: "field" }, h("span", { class: "label", "aria-hidden": "true" }, "\u00a0"), add));
  allocForm.addEventListener("submit", (e) => {
    e.preventDefault();
    busy(add, async () => {
      const r = await post(`/nodes/${id}/allocations`, { ip: ip.value.trim(), ports: ports.value });
      toast(`Added ${r.added} port${r.added === 1 ? "" : "s"}${r.skipped ? `, ${r.skipped} already existed` : ""}.`);
      replace(root);
      nodeDetail(root, { navigate }, id);
    });
  });
  const allocRows = node.allocations.map((a) => h("tr", {},
    h("td", { class: "figure" }, `${a.ip}:${a.port}`),
    h("td", {}, a.server_id ? h("a", { class: "link", href: `#/servers/${a.server_id}` }, a.server_name) : h("span", { class: "faint" }, "Free")),
    h("td", { class: "right" }, a.server_id ? null : h("button", { class: "btn sm ghost icon-only", type: "button", title: "Remove", "aria-label": `Remove port ${a.port}`,
      onclick: (e) => busy(e.currentTarget, async () => { await del(`/nodes/${id}/allocations/${a.id}`); e.target.closest("tr").remove(); }) }, icon("trash")))));

  const rotate = h("button", { class: "btn", type: "button" }, icon("key"), "Rotate token");
  const tokenBox = h("div", {}, installBlock(node.install_command));
  rotate.addEventListener("click", () => busy(rotate, async () => {
    if (!(await confirmDialog({ title: "Rotate the agent token?", body: "The current agent stops accepting the panel until you restart it with the new token.", confirm: "Rotate", danger: true }))) return;
    const r = await post(`/nodes/${id}/token`);
    replace(tokenBox, installBlock(r.install_command));
    toast("New token issued. Restart the agent with it.");
  }));
  const remove = h("button", { class: "btn danger", type: "button" }, icon("trash"), "Delete node");
  remove.addEventListener("click", () => busy(remove, async () => {
    if (!(await confirmDialog({ title: `Delete ${node.name}?`, body: "The panel forgets this node and its ports. The agent and Docker on the machine are left alone.", confirm: "Delete", danger: true, typeToConfirm: node.name }))) return;
    await del(`/nodes/${id}`);
    toast("Node deleted.");
    navigate("/admin/nodes");
  }));

  appendAll(root, 
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, node.name),
      h("div", { class: "meta" }, status, h("span", { class: "mono", style: { fontSize: "13px" } }, node.url), node.location ? h("span", {}, node.location) : null))),
    outdated,
    h("section", { class: "section" }, h("header", {}, h("h2", {}, "System")), h("div", { class: "section-body" }, system)),
    h("section", { class: "section" },
      h("header", {}, h("h2", {}, "Ports"), h("p", {}, `${node.allocations_free} of ${node.allocations_total} free. Servers take ports from this pool; open them in the machine's firewall too.`)),
      h("div", { class: "section-body" }, allocForm,
        node.allocations.length ? h("div", { class: "table-wrap", style: { maxHeight: "420px", overflow: "auto" } }, h("table", { class: "board" }, h("tbody", {}, allocRows))) : null)),
    edit,
    await tunnelSection(node),
    h("section", { class: "section" },
      h("header", {}, h("h2", {}, "Agent"), h("p", {}, "Run on the node machine. Keep the token secret — it controls every server there.")),
      h("div", { class: "section-body" }, tokenBox, h("div", { class: "toolbar", style: { margin: 0 } }, rotate, remove))));
}

// ----- users ------------------------------------------------------------------------------------

async function userList(root, { me }) {
  const tbody = h("tbody", {}, skeletonRows(5, 2));
  const username = h("input", { id: "u-name", required: true, pattern: "[A-Za-z0-9_.\\-]{3,32}", autocomplete: "off", spellcheck: "false" });
  const password = h("input", { id: "u-pass", type: "password", required: true, minlength: 10, autocomplete: "new-password" });
  const maxServers = h("input", { id: "u-maxs", type: "number", min: 0, max: 1000, value: 0 });
  const maxMemory = h("input", { id: "u-maxm", type: "number", min: 0, step: 1024, value: 0 });
  const isAdmin = h("input", { type: "checkbox" });
  const add = h("button", { class: "btn primary", type: "submit" }, icon("plus"), "Add user");
  const form = h("form", { class: "section-body", style: { marginBottom: "28px" } },
    h("div", { class: "grid-2" }, field("Username", username), field("Password", password, { hint: "Share it with them; they can change it." })),
    h("div", { class: "grid-3", style: { alignItems: "end" } },
      field("Servers they can create", maxServers, { hint: "0 = only servers you give them." }),
      suffixed("Memory quota", maxMemory, "MB", "Total across their servers. 0 = no limit."),
      h("div", { class: "field" }, h("label", { class: "check", style: { height: "36px", alignItems: "center" } }, isAdmin, h("span", {}, "Admin")))),
    h("div", {}, add));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    busy(add, async () => {
      await post("/users", { username: username.value.trim(), password: password.value, is_admin: isAdmin.checked,
        max_servers: Number(maxServers.value) || 0, max_memory_mb: Number(maxMemory.value) || 0 });
      toast(`${username.value.trim()} can now sign in.`);
      form.reset();
      load();
    });
  });

  async function load() {
    const users = await get("/users");
    replace(tbody, users.map((u) => {
      const toggle = h("input", { type: "checkbox", checked: u.is_admin, disabled: u.id === me.id, "aria-label": `${u.username} is admin` });
      toggle.addEventListener("change", async () => {
        try { await patch(`/users/${u.id}`, { is_admin: toggle.checked }); toast(`${u.username} is ${toggle.checked ? "now" : "no longer"} an admin.`); }
        catch (err) { toggle.checked = !toggle.checked; fail(err); }
      });
      const qs = h("input", { type: "number", min: 0, max: 1000, value: u.max_servers, "aria-label": `${u.username} server quota`, style: { width: "72px" } });
      const qm = h("input", { type: "number", min: 0, step: 1024, value: u.max_memory_mb, "aria-label": `${u.username} memory quota (MB)`, style: { width: "96px" } });
      const saveQ = h("button", { class: "btn sm", type: "button" }, "Save");
      saveQ.addEventListener("click", () => busy(saveQ, async () => {
        await patch(`/users/${u.id}`, { max_servers: Number(qs.value) || 0, max_memory_mb: Number(qm.value) || 0 });
        toast(`Quota updated for ${u.username}.`);
      }));
      const reset2fa = u.totp_enabled && u.id !== me.id ? h("button", { class: "btn sm ghost", type: "button" }, "Reset 2FA") : null;
      reset2fa?.addEventListener("click", () => busy(reset2fa, async () => {
        if (!(await confirmDialog({ title: `Reset two-factor for ${u.username}?`, body: "Use this if they lost their phone. They can sign in with just their password and set it up again.", confirm: "Reset", danger: true }))) return;
        await patch(`/users/${u.id}`, { reset_2fa: true });
        load();
      }));
      const remove = u.id === me.id ? null : h("button", { class: "btn sm ghost icon-only", type: "button", title: "Delete", "aria-label": `Delete ${u.username}` }, icon("trash"));
      remove?.addEventListener("click", () => busy(remove, async () => {
        if (!(await confirmDialog({ title: `Delete ${u.username}?`, body: "They lose access immediately.", confirm: "Delete", danger: true }))) return;
        await del(`/users/${u.id}`);
        load();
      }));
      return h("tr", {},
        h("td", {}, h("div", { class: "name" }, u.username, u.id === me.id ? h("span", { class: "faint" }, " (you)") : null),
          h("div", { class: "sub" }, `${u.servers} server${u.servers === 1 ? "" : "s"} · ${mb(u.memory_used_mb) === "—" ? "0 MB" : mb(u.memory_used_mb)}${u.totp_enabled ? " · 2FA on" : ""}`)),
        h("td", {}, h("label", { class: "check" }, toggle, h("span", {}, "Admin"))),
        h("td", { class: "hide-md" }, u.is_admin ? h("span", { class: "faint" }, "No limits") : h("div", { class: "cell-flex", style: { gap: "6px" } }, qs, qm, saveQ)),
        h("td", { class: "right" }, h("div", { class: "row-actions", style: { opacity: 1 } }, reset2fa, remove)));
    }));
  }

  appendAll(root, head("Users", "Friends who can sign in. They see servers they own or that are shared with them. Give them a quota to let them create their own."), form,
    h("div", { class: "table-wrap" }, h("table", { class: "board" },
      h("thead", {}, h("tr", {}, h("th", {}, "User"), h("th", {}, "Role"), h("th", { class: "hide-md" }, "Quota (servers · MB)"), h("th", {}, h("span", { class: "sr-only" }, "Actions")))), tbody)));
  await load();
}

// ----- activity ---------------------------------------------------------------------------------

async function activity(root) {
  appendAll(root, head("Activity", "Everything that happened: who did what, and what Hearth did on its own (updates, crashes, schedules)."),
    activityList(await get("/admin/activity"), { showServer: true }));
}

// ----- tunnel (node page) -----------------------------------------------------------------------

async function tunnelSection(node) {
  const body = h("div", { class: "section-body" });
  const draw = async () => {
    let st;
    try { st = await get(`/nodes/${node.id}/tunnel`); } catch (err) { return replace(body, notice("bad", err.message)); }
    const secret = h("input", { id: "pl-secret", type: "password", class: "mono", autocomplete: "off", placeholder: st.saved ? "Saved — paste to replace" : "Secret key from playit.gg" });
    const save = h("button", { class: "btn primary", type: "submit" }, st.configured ? "Update" : "Start tunnel");
    const form = h("form", { class: "toolbar", style: { margin: 0, alignItems: "end" } }, field("playit.gg secret key", secret), save);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      busy(save, async () => { await put(`/nodes/${node.id}/tunnel`, { secret: secret.value.trim() }); toast("Tunnel agent started."); draw(); });
    });
    const stop = st.configured ? h("button", { class: "btn ghost", type: "button" }, "Remove tunnel") : null;
    stop?.addEventListener("click", () => busy(stop, async () => { await del(`/nodes/${node.id}/tunnel`); toast("Tunnel removed."); draw(); }));
    replace(body,
      h("ol", { class: "steps-list" },
        h("li", {}, "Create a free account at ", h("a", { class: "link", href: "https://playit.gg", target: "_blank", rel: "noopener noreferrer" }, "playit.gg"), " and add a Docker agent to get its secret key."),
        h("li", {}, "Paste the key here. Hearth runs the playit agent on this node."),
        h("li", {}, "On playit.gg, add a tunnel per server (Minecraft Java → local port = the server's port)."),
        h("li", {}, "Paste the address playit gives you into that server's Network tab.")),
      form,
      st.configured ? h("div", { class: "cell-flex" }, h("span", { class: "status", "data-state": st.state === "running" ? "running" : "node_offline" }, h("span", { class: "dot" }), `Tunnel agent ${st.state}`), stop) : null,
      st.log ? h("details", {}, h("summary", { class: "muted", style: { cursor: "pointer" } }, "Agent log"), h("pre", { class: "code", style: { marginTop: "8px", maxHeight: "240px" } }, st.log)) : null);
  };
  await draw();
  return section("Tunnel (no port forwarding)", "Lets friends join without opening router ports. Useful behind CGNAT, school or dorm networks.", body);
}

// ----- settings ---------------------------------------------------------------------------------

async function settings(root) {
  const s = await get("/admin/settings");
  const key = h("input", { id: "cf-key", type: "password", class: "mono", autocomplete: "off",
    placeholder: s.curseforge_api_key_set ? `Saved (${s.curseforge_api_key_hint}) — paste to replace` : "$2a$10$…" });
  const interval = h("input", { id: "upd", type: "number", min: 15, max: 10080, step: 15, value: s.update_interval_minutes });
  const zones = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  const tz = h("input", { id: "tz", class: "mono", value: s.timezone, list: "tz-list", autocomplete: "off" });
  const alertHook = h("input", { id: "alert-hook", class: "mono", value: s.alerts_discord_webhook || "", placeholder: "https://discord.com/api/webhooks/…", autocomplete: "off" });
  const save = h("button", { class: "btn primary", type: "submit" }, "Save");
  const clearKey = s.curseforge_api_key_set && !s.curseforge_from_env ? h("button", { class: "btn ghost", type: "button" }, "Remove key") : null;
  clearKey?.addEventListener("click", () => busy(clearKey, async () => {
    await put("/admin/settings", { curseforge_api_key: "" });
    toast("CurseForge key removed.");
    replace(root); settings(root);
  }));
  const form = h("form", {},
    h("section", { class: "section" },
      h("header", {}, h("h2", {}, "CurseForge"), h("p", {}, "Needed to browse CurseForge and install its modpacks. Modrinth works without a key.")),
      h("div", { class: "section-body" },
        field("API key", key, { hint: h("span", {}, "Free from ", h("a", { class: "link", href: "https://console.curseforge.com/", target: "_blank", rel: "noopener noreferrer" }, "console.curseforge.com"), s.curseforge_from_env ? ". Currently set from the CURSEFORGE_API_KEY environment variable." : ".") }),
        clearKey ? h("div", {}, clearKey) : null)),
    h("section", { class: "section" },
      h("header", {}, h("h2", {}, "Modpack updates"), h("p", {}, "How often Hearth looks for new modpack releases. Servers with auto-update on restart onto them, after a safety backup.")),
      h("div", { class: "section-body", style: { maxWidth: "260px" } }, suffixed("Check every", interval, "min", "15 minutes to 7 days."))),
    h("section", { class: "section" },
      h("header", {}, h("h2", {}, "Alerts"), h("p", {}, "Crashes and crash loops, failed backups, servers near their disk limit, and nodes going offline or running low on disk are posted to this Discord channel, for every server.")),
      h("div", { class: "section-body", style: { maxWidth: "560px" } }, field("Discord webhook", alertHook, { hint: "Channel settings → Integrations → Webhooks. Leave empty to turn off." }))),
    h("section", { class: "section" },
      h("header", {}, h("h2", {}, "Time zone"), h("p", {}, "Scheduled tasks run on this clock.")),
      h("div", { class: "section-body", style: { maxWidth: "320px" } }, field("Time zone", tz, { hint: "e.g. America/Toronto" }),
        h("datalist", { id: "tz-list" }, zones.map((z) => h("option", { value: z }))))),
    h("div", { class: "form-foot" }, save));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const body = { update_interval_minutes: Number(interval.value), timezone: tz.value.trim(), alerts_discord_webhook: alertHook.value.trim() };
    if (key.value.trim()) body.curseforge_api_key = key.value.trim();
    busy(save, async () => {
      await put("/admin/settings", body);
      toast("Settings saved.");
      replace(root); settings(root);
    });
  });
  appendAll(root, head("Settings"), form, await offsiteSection(), await panelBackupSection(), await webhooksSection());
}

async function offsiteSection() {
  const body = h("div", { class: "section-body" });
  const draw = async () => {
    const o = await get("/admin/offsite");
    const inp = (id, value, extra = {}) => h("input", { id, class: "mono", value, autocomplete: "off", ...extra });
    const endpoint = inp("s3-endpoint", o.endpoint, { placeholder: "https://s3.us-west-002.backblazeb2.com", required: true });
    const region = inp("s3-region", o.region, { placeholder: "us-east-1" });
    const bucket = inp("s3-bucket", o.bucket, { placeholder: "my-hearth-backups", required: true });
    const access = inp("s3-access", o.access_key, { required: true });
    const secret = h("input", { id: "s3-secret", type: "password", class: "mono", autocomplete: "off",
      placeholder: o.secret_set ? "Saved — paste to replace" : "", required: !o.secret_set });
    const prefix = inp("s3-prefix", o.prefix, { placeholder: "hearth/" });
    const pathStyle = h("input", { type: "checkbox", checked: o.path_style });
    const save = h("button", { class: "btn primary", type: "submit" }, o.configured ? "Test and save" : "Test and turn on");
    const off = o.configured ? h("button", { class: "btn ghost", type: "button" }, "Turn off") : null;
    off?.addEventListener("click", () => busy(off, async () => {
      if (!(await confirmDialog({ title: "Turn off off-site copies?", body: "New backups stay on their node only. Copies already in storage are left there.", confirm: "Turn off" }))) return;
      await put("/admin/offsite", { endpoint: "" }); toast("Off-site copies turned off."); draw();
    }));
    const form = h("form", { class: "grid-2" },
      field("Endpoint", endpoint, { hint: "Any S3-compatible storage: AWS S3, Backblaze B2, Cloudflare R2, Wasabi, MinIO…" }),
      field("Region", region, { hint: "auto for Cloudflare R2." }),
      field("Bucket", bucket), field("Folder in the bucket", prefix),
      field("Access key ID", access), field("Secret access key", secret),
      h("label", { class: "check" }, pathStyle, h("span", {}, "Path-style addressing (needed for MinIO; works with most providers)")),
      h("div", { class: "cell-flex", style: { gridColumn: "1 / -1" } }, save, off));
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      busy(save, async () => {
        const out = { endpoint: endpoint.value.trim(), region: region.value.trim(), bucket: bucket.value.trim(),
          access_key: access.value.trim(), prefix: prefix.value.trim(), path_style: pathStyle.checked };
        if (secret.value.trim()) out.secret_key = secret.value.trim();
        await put("/admin/offsite", out);
        toast("Storage works. New backups are copied there.");
        draw();
      });
    });
    replace(body,
      o.configured ? notice("info", "On. Every finished backup is copied here, and deleted from here when it's deleted or pruned.") : null,
      form);
  };
  await draw();
  return section("Off-site backups", "Keep a second copy of every backup somewhere else, so a dead disk or a stolen PC doesn't take your worlds with it. Nodes only get short-lived upload links; the keys stay in the panel.", body);
}

async function panelBackupSection() {
  const body = h("div", { class: "section-body" });
  const draw = async () => {
    const d = await get("/admin/panel-backups");
    if (!d.supported) return replace(body, h("p", { class: "muted" }, "The panel uses PostgreSQL; back it up with pg_dump."));
    const now = h("button", { class: "btn", type: "button" }, icon("archive"), "Back up now");
    now.addEventListener("click", () => busy(now, async () => {
      const r = await post("/admin/panel-backups");
      toast(r.offsite ? "Panel backed up (and copied off-site)." : "Panel backed up.");
      draw();
    }));
    const rows = d.backups.map((b) => {
      const dl = h("button", { class: "btn sm ghost icon-only", type: "button", title: "Download", "aria-label": `Download ${b.name}` }, icon("arrowdown"));
      dl.addEventListener("click", () => download(`/api/admin/panel-backups/${encodeURIComponent(b.name)}`));
      return h("tr", {}, h("td", { class: "mono" }, b.name), h("td", { class: "figure right" }, bytes(b.size)), h("td", { class: "right" }, dl));
    });
    replace(body,
      h("div", { class: "toolbar" }, now),
      h("table", { class: "table" }, h("tbody", {}, rows.length ? rows : emptyRow(3, "The first nightly copy is made a few minutes after the panel starts."))));
  };
  await draw();
  return section("Panel database", "Users, servers, schedules and settings are copied every night (the last 14 are kept, and copied off-site when that's on). To restore one, stop the panel and replace data/hearth.db with it.", body);
}

async function webhooksSection() {
  const list = h("tbody", {});
  const url = h("input", { id: "wh-url", class: "mono", placeholder: "https://billing.example.com/hearth", required: true });
  const add = h("button", { class: "btn", type: "submit" }, icon("plus"), "Add webhook");
  const test = h("button", { class: "btn ghost", type: "button" }, "Send test event");
  test.addEventListener("click", () => busy(test, async () => { await post("/admin/webhooks/test"); toast("Test event sent to every webhook."); }));
  const chosen = new Set();
  const eventsBox = h("div", { class: "perm-grid" });
  async function load() {
    const d = await get("/admin/webhooks");
    if (!eventsBox.childNodes.length) {
      eventsBox.append(...d.events.map((ev) => {
        const box = h("input", { type: "checkbox" });
        box.addEventListener("change", () => (box.checked ? chosen.add(ev) : chosen.delete(ev)));
        return h("label", { class: "check" }, box, h("span", { class: "mono", style: { fontSize: "12.5px" } }, ev));
      }));
    }
    replace(list, d.webhooks.length ? d.webhooks.map((w) => {
      const rm = h("button", { class: "btn sm ghost icon-only", type: "button", "aria-label": `Delete webhook ${w.url}`, title: "Delete" }, icon("trash"));
      rm.addEventListener("click", () => busy(rm, async () => { await del(`/admin/webhooks/${w.id}`); load(); }));
      return h("tr", {},
        h("td", {}, h("div", { class: "mono", style: { fontSize: "13px", overflowWrap: "anywhere" } }, w.url),
          h("div", { class: "sub" }, w.events.length ? w.events.join(", ") : "all events")),
        h("td", { class: "hide-sm" }, h("div", { class: "cell-flex", style: { gap: "4px" } }, h("span", { class: "faint", style: { fontSize: "13px" } }, "Signing secret"), copyButton(w.secret, "Copy signing secret"))),
        h("td", { class: "right" }, rm));
    }) : emptyRow(3, "No webhooks."));
  }
  const form = h("form", { class: "section-body" }, field("URL", url),
    h("div", { class: "field" }, h("span", { class: "label" }, "Events"), h("span", { class: "hint" }, "None ticked = every event."), eventsBox),
    h("div", { class: "toolbar", style: { margin: 0 } }, add, test));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    busy(add, async () => { await post("/admin/webhooks", { url: url.value.trim(), events: [...chosen] }); url.value = ""; toast("Webhook added."); load(); });
  });
  await load();
  return section("Webhooks", "Send events as signed JSON to billing systems, bots or monitoring. Each request carries X-Hearth-Event and an X-Hearth-Signature (HMAC-SHA256 of the body with the signing secret).",
    form, h("div", { class: "table-wrap" }, h("table", { class: "board" }, list)));
}
