// First-run setup: shown once to the admin who just created the panel. Each step can be skipped;
// everything here can be changed later in Admin.

import { get, put } from "../api.js";
import { h, icon, replace } from "../dom.js";
import { busy, field, notice } from "../ui.js";

export async function renderWelcome(root, { navigate, setCrumbs }) {
  setCrumbs([{ label: "Get started" }]);
  const [settings, nodes] = await Promise.all([get("/admin/settings"), get("/nodes")]);
  const body = h("div", { class: "wizard-body" });
  const steps = h("ol", { class: "wizard-steps", "aria-label": "Setup steps" });
  const STEPS = ["Time zone", "Your machine", "Extras", "First server"];
  let at = 0;

  const zones = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  const guess = Intl.DateTimeFormat().resolvedOptions().timeZone || settings.timezone;
  const tz = h("input", { id: "w-tz", class: "mono", value: settings.timezone === "UTC" ? guess : settings.timezone, list: "w-tz-list", autocomplete: "off" });
  const hook = h("input", { id: "w-hook", class: "mono", value: settings.alerts_discord_webhook || "", placeholder: "https://discord.com/api/webhooks/…", autocomplete: "off" });
  const cf = h("input", { id: "w-cf", type: "password", class: "mono", autocomplete: "off", placeholder: settings.curseforge_api_key_set ? "Saved" : "$2a$10$…" });

  const pages = [
    () => [h("h2", {}, "Which time zone are you in?"),
      h("p", { class: "muted" }, "Scheduled restarts and backups run on this clock."),
      field("Time zone", tz, { hint: "Detected from this browser." }), h("datalist", { id: "w-tz-list" }, zones.map((z) => h("option", { value: z })))],
    () => [h("h2", {}, "Your machine"),
      nodes.length ? nodes.map((n) => h("div", { class: "wizard-node" },
        h("span", { class: "status", "data-state": n.online ? "running" : "node_offline" }, h("span", { class: "dot" }), n.online ? "Online" : "Offline"),
        h("div", {}, h("strong", {}, n.name), h("div", { class: "muted" }, n.online ? `${n.public_host} · ${n.allocations_free} free ports` : (n.error || "The agent isn't responding.")))))
        : notice("warn", "No machine is connected yet. Add one under Admin → Nodes, then come back."),
      h("p", { class: "muted" }, "Players connect to these addresses. Behind a home router? Forward the ports, or set up a free playit.gg tunnel under Admin → Nodes.")],
    () => [h("h2", {}, "Extras"),
      h("p", { class: "muted" }, "Both optional. Leave them empty to skip."),
      field("Discord webhook for alerts", hook, { hint: "Crashes, failed backups, full disks and offline machines are posted here." }),
      field("CurseForge API key", cf, { hint: h("span", {}, "Lets you install CurseForge modpacks. Free from ", h("a", { class: "link", href: "https://console.curseforge.com/", target: "_blank", rel: "noopener noreferrer" }, "console.curseforge.com"), ".") }),
      h("p", { class: "muted" }, "Want a copy of every backup somewhere else? Set up off-site backups under Admin → Settings.")],
    () => [h("h2", {}, "You're set"),
      h("p", { class: "muted" }, "Create your first server: plain Minecraft, a modpack, or one of the other games."),
      h("div", { class: "toolbar", style: { margin: 0 } },
        h("a", { class: "btn primary", href: "#/servers/new", onclick: done }, icon("plus"), "Create a server"),
        h("a", { class: "btn ghost", href: "#/", onclick: done }, "Go to the dashboard"))],
  ];

  function done() { try { localStorage.setItem("hearth.welcomed", "1"); } catch { /* fine */ } }

  async function saveStep() {
    if (at === 0 && tz.value.trim() && tz.value.trim() !== settings.timezone) await put("/admin/settings", { timezone: tz.value.trim() });
    if (at === 2) {
      const out = {};
      if (hook.value.trim() !== (settings.alerts_discord_webhook || "")) out.alerts_discord_webhook = hook.value.trim();
      if (cf.value.trim()) out.curseforge_api_key = cf.value.trim();
      if (Object.keys(out).length) await put("/admin/settings", out);
    }
  }

  function draw() {
    replace(steps, STEPS.map((label, i) => h("li", { "aria-current": i === at ? "step" : null, class: i < at ? "done" : "" },
      h("span", { class: "num" }, i < at ? icon("check") : String(i + 1)), label)));
    const back = at > 0 && at < 3 ? h("button", { class: "btn ghost", type: "button", onclick: () => { at -= 1; draw(); } }, "Back") : null;
    const next = at < 3 ? h("button", { class: "btn primary", type: "submit" }, at === 2 ? "Save and continue" : "Next") : null;
    const skip = at < 3 ? h("button", { class: "btn ghost", type: "button", onclick: () => { done(); navigate("/"); } }, "Skip for now") : null;
    const form = h("form", { class: "wizard-card" }, ...pages[at](), at < 3 ? h("div", { class: "form-foot" }, skip, h("span", { class: "grow" }), back, next) : null);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      busy(next, async () => { await saveStep(); at += 1; draw(); });
    });
    replace(body, form);
    form.querySelector("input")?.focus();
  }

  root.append(h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Welcome to Hearth"),
    h("p", { class: "muted", style: { marginTop: "8px" } }, "Four quick steps. You can change all of this later in Admin."))),
  h("div", { class: "wizard" }, steps, body));
  draw();
}
