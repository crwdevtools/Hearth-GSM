import { get, post } from "../api.js";
import { h, icon, replace } from "../dom.js";
import { busy, field, mb, notice, suffixed, toast } from "../ui.js";
import { sourceEditor } from "./picker.js";
import { variableFields } from "./vars.js";

const MEMORY_PRESETS = [1024, 2048, 4096, 6144, 8192, 12288, 16384];

export async function renderCreate(root, { query, navigate, setCrumbs, me }) {
  setCrumbs([{ label: "Servers", href: "#/" }, { label: "New server" }]);
  const admin = me.is_admin;
  const [templates, nodes, users] = await Promise.all([
    get("/templates"),
    admin ? get("/nodes") : get("/nodes/choices"),
    admin ? get("/users") : Promise.resolve([me]),
  ]);

  root.append(h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, "New server"),
      h("p", { class: "muted", style: { marginTop: "8px" } }, "Pick a game and a node. Everything here can be changed later."))));

  if (!nodes.length) {
    root.append(h("div", { class: "empty" },
      h("h2", {}, admin ? "Add a node first" : "No machines available"),
      h("p", {}, admin ? "Servers run on nodes. Add the machine you want to host on, then come back." : "Ask an admin to add a node."),
      admin ? h("a", { class: "btn primary", href: "#/admin/nodes/new" }, icon("plus"), "Add a node") : null));
    return;
  }

  const prefillPack = query.get("project")
    ? { provider: query.get("provider") || "modrinth", project_id: query.get("project"), name: query.get("name") || query.get("project"),
        icon_url: query.get("icon") || null, slug: query.get("slug") || null, version_id: query.get("version") || null }
    : null;

  // ----- basics --------------------------------------------------------------------------------
  const name = h("input", { required: true, maxlength: 64, value: prefillPack?.name || "", placeholder: "Survival with friends" });
  const description = h("input", { maxlength: 500, placeholder: "Optional" });
  const owner = h("select", {}, users.map((u) => h("option", { value: u.id, selected: u.id === me.id }, u.username)));
  const node = h("select", {}, nodes.map((n) => h("option", { value: n.id, disabled: !n.allocations_free },
    `${n.name}${n.location ? ` · ${n.location}` : ""} — ${n.allocations_free} free port${n.allocations_free === 1 ? "" : "s"}${n.online ? "" : " · offline"}`)));
  const firstUsable = nodes.find((n) => n.allocations_free && n.online) || nodes.find((n) => n.allocations_free);
  if (firstUsable) node.value = firstUsable.id;

  // ----- game ----------------------------------------------------------------------------------
  if (!admin) templates.splice(0, templates.length, ...templates.filter((x) => !x.features.includes("custom_image")));
  let template = templates.find((t) => t.key === "minecraft-java") || templates[0];
  const templateList = h("div", { class: "radio-list", role: "radiogroup", "aria-label": "Game" },
    templates.map((t) => h("label", { class: "radio-item" },
      h("input", { type: "radio", name: "template", value: t.key, checked: t.key === template.key,
        onchange: () => { template = t; onTemplate(); } }),
      h("span", { class: "body" }, h("span", { class: "t" }, t.name), h("span", { class: "d", style: { display: "block" } }, t.description)),
      h("span", { class: "mono faint", style: { fontSize: "12.5px" } }, t.ports.map((p) => p.protocols.join("+")).join(" · ")))));

  // ----- resources -----------------------------------------------------------------------------
  const memory = h("input", { id: "mem", type: "number", min: 128, step: 256, required: true, inputmode: "numeric" });
  const presetWrap = h("div", { class: "presets" });
  const cpu = h("input", { id: "cpu", type: "number", min: 0, max: 10000, step: 25, value: 0, inputmode: "numeric" });
  const disk = h("input", { id: "disk", type: "number", min: 0, step: 1024, value: 0, inputmode: "numeric" });
  const swap = h("input", { id: "swap", type: "number", min: 0, step: 256, value: 0, inputmode: "numeric" });
  const memHint = h("span", { class: "hint" });
  function drawPresets() {
    replace(presetWrap, MEMORY_PRESETS.filter((m) => m >= template.min_memory_mb).map((m) =>
      h("button", { type: "button", class: "btn sm", "aria-pressed": String(Number(memory.value) === m),
        onclick: () => { memory.value = m; drawPresets(); updateMemHint(); } }, mb(m))));
  }
  function updateMemHint() {
    const n = nodes.find((x) => String(x.id) === node.value);
    const java = template.features.includes("minecraft_java");
    const m = Number(memory.value);
    const heap = Math.max(512, m - Math.min(Math.max(Math.floor(m * 0.25), 768), 2048));  // matches jvm_heap_mb()
    const left = n?.memory_mb ? ` ${mb(n.memory_mb - n.memory_assigned_mb)} left on ${n.name}.` : "";
    memHint.textContent = (java ? `Java heap gets ${mb(heap)}; the rest is headroom Java needs outside the heap.` : "Hard limit for the container.") + left;
  }
  memory.addEventListener("input", () => { drawPresets(); updateMemHint(); });

  // ----- port ----------------------------------------------------------------------------------
  const port = h("select", {});
  async function loadPorts() {
    replace(port, h("option", { value: "" }, "Automatic — first free port"));
    if (!admin) return;
    try {
      const detail = await get(`/nodes/${node.value}`);
      const free = detail.allocations.filter((a) => a.server_id === null);
      port.append(...free.slice(0, 300).map((a) => h("option", { value: a.id }, `${detail.public_host}:${a.port}${a.ip !== "0.0.0.0" ? ` (${a.ip})` : ""}`)));
    } catch {
      /* automatic still works */
    }
  }
  node.addEventListener("change", () => { loadPorts(); updateMemHint(); });

  // ----- dynamic sections ----------------------------------------------------------------------
  const sourceSection = h("section", { class: "section" });
  const settingsSection = h("section", { class: "section" });
  const eulaWrap = h("div", {});
  let source = null;
  let vars = null;
  const eula = h("input", { type: "checkbox" });
  const start = h("input", { type: "checkbox", checked: true });
  const presetList = await get("/presets").catch(() => []);
  const starter = h("select", { id: "starter" }, h("option", { value: "" }, "None"),
    presetList.map((p) => h("option", { value: p.key }, `${p.name} (${p.software.map((x) => x[0] + x.slice(1).toLowerCase()).join("/")})`)));
  const starterHint = h("span", { class: "hint" }, "Plugins or mods installed from Modrinth before the first start.");
  starter.addEventListener("change", () => replace(starterHint, presetList.find((p) => p.key === starter.value)?.description || "Plugins or mods installed from Modrinth before the first start."));

  function onTemplate() {
    memory.value = template.default_memory_mb;
    memory.min = template.min_memory_mb;
    drawPresets();
    updateMemHint();
    if (template.features.includes("minecraft_java")) {
      source = sourceEditor({ initial: prefillPack ? { kind: "modpack", modpack: prefillPack, auto_update: true } : { kind: "software" } });
      replace(sourceSection,
        h("header", {}, h("h2", {}, "Source"), h("p", {}, "Run plain Minecraft at any version, or install a modpack. Hearth picks the right Java for it.")),
        source.el,
        presetList.length ? h("div", { class: "field", style: { maxWidth: "420px", marginTop: "16px" } }, h("label", { for: "starter" }, "Starter pack"), starter, starterHint) : null);
      sourceSection.hidden = false;
    } else {
      source = null;
      sourceSection.hidden = true;
    }
    vars = variableFields(template, {});
    replace(settingsSection,
      h("header", {}, h("h2", {}, "Settings"),
        h("p", {}, "Defaults are sensible. Change them now or later under Properties.")),
      vars.el);
    settingsSection.hidden = vars.empty;
    replace(eulaWrap, template.features.includes("eula")
      ? h("label", { class: "check" }, eula, h("span", {}, "I agree to the Minecraft End User License Agreement",
        h("small", {}, h("a", { class: "link", href: "https://aka.ms/MinecraftEULA", target: "_blank", rel: "noopener noreferrer" }, "Read the EULA"), " — required by Mojang to run a server.")))
      : null);
  }

  const submit = h("button", { class: "btn primary", type: "submit" }, "Create server");
  const form = h("form", {},
    h("section", { class: "section" },
      h("header", {}, h("h2", {}, "Basics"), h("p", {}, "Who owns it and which machine runs it.")),
      h("div", { class: "section-body" },
        field("Name", name),
        field("Description", description),
        h("div", { class: "grid-2" }, admin ? field("Owner", owner, { hint: "Owners can control and configure the server." }) : null, field("Node", node)))),
    h("section", { class: "section" },
      h("header", {}, h("h2", {}, "Game")),
      h("div", { class: "section-body" }, templateList)),
    sourceSection,
    h("section", { class: "section" },
      h("header", {}, h("h2", {}, "Resources"), h("p", {}, "Limits enforced by Docker on the node.")),
      h("div", { class: "section-body" },
        h("div", { class: "field" }, h("label", { for: "mem" }, "Memory"),
          h("div", { class: "input-suffix", style: { maxWidth: "200px" } }, memory, h("span", { "aria-hidden": "true" }, "MB")),
          presetWrap, memHint),
        h("div", { class: "grid-3" },
          suffixed("CPU limit", cpu, "%", "100% = one core. 0 = no limit."),
          suffixed("Disk", disk, "MB", "Counted against the node. 0 = untracked."),
          suffixed("Swap", swap, "MB", "0 disables swap.")))),
    h("section", { class: "section" },
      h("header", {}, h("h2", {}, "Port"), h("p", {}, "Players connect to the node's public address on this port.")),
      h("div", { class: "section-body" }, field("Primary port", port))),
    settingsSection,
    h("div", { class: "form-foot" },
      h("div", { class: "note section-body", style: { gap: "10px" } }, eulaWrap,
        h("label", { class: "check" }, start, h("span", {}, "Start when installed"))),
      h("a", { class: "btn ghost", href: "#/" }, "Cancel"),
      submit),
  );

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!name.value.trim()) { name.focus(); return toast("Give the server a name.", "warn"); }
    if (template.features.includes("eula") && !eula.checked) { eula.focus(); return toast("Accept the Minecraft EULA to continue.", "warn"); }
    const body = {
      name: name.value.trim(), description: description.value, owner_id: admin ? Number(owner.value) : null, node_id: Number(node.value),
      template: template.key, memory_mb: Number(memory.value), cpu_percent: Number(cpu.value) || 0,
      disk_mb: Number(disk.value) || 0, swap_mb: Number(swap.value) || 0,
      allocation_id: port.value ? Number(port.value) : null, variables: vars.values(),
      accept_eula: eula.checked, start: start.checked,
    };
    if (source) {
      const s = source.value();
      if (!s) return toast("Choose a modpack, or switch back to server software.", "warn");
      if (s.kind === "software") Object.assign(body.variables, { TYPE: s.server_type, VERSION: s.version });
      else Object.assign(body, { modpack: s.modpack, auto_update: s.auto_update });
      if (starter.value) {
        const p = presetList.find((x) => x.key === starter.value);
        if (s.kind !== "software" || !p.software.includes(s.server_type)) {
          return toast(`${p.name} needs ${p.software.map((x) => x[0] + x.slice(1).toLowerCase()).join(" or ")} as the server software.`, "warn");
        }
        body.preset = starter.value;
      }
    }
    busy(submit, async () => {
      const created = await post("/servers", body);
      toast(`${created.name} is installing on ${created.node.name}.`);
      if (created.warning) toast(created.warning, "warn", 9000);
      navigate(`/servers/${created.id}`);
    });
  });

  root.append(form);
  if (!firstUsable) root.prepend(notice("warn", "Every node is out of free ports. Add allocations under Admin → Nodes."));
  onTemplate();
  loadPorts();
}
