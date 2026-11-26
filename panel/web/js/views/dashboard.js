import { get } from "../api.js";
import { h, icon, replace, appendAll } from "../dom.js";
import { activityBars, copyButton, fail, mb, bytes, safeUrl, skeletonRows, status, wordmark } from "../ui.js";

const FILTERS = [
  ["all", "All servers", () => true],
  ["running", "Running", (s) => ["running", "starting"].includes(s.status.state)],
  ["offline", "Offline", (s) => !["running", "starting"].includes(s.status.state)],
];

export async function renderDashboard(root, { me, navigate }) {
  let servers = null;
  let filter = "all";
  let query = "";

  const tabs = h("div", { class: "tabs", role: "tablist", "aria-label": "Filter servers" });
  const search = h("input", { type: "search", placeholder: "Search servers...", "data-search": true, "aria-label": "Search servers" });
  const tbody = h("tbody", {}, skeletonRows(6));
  const table = h("div", { class: "table-wrap" },
    h("table", { class: "board" },
      h("thead", {}, h("tr", {},
        h("th", { class: "rank", scope: "col" }, "#"),
        h("th", { scope: "col" }, "Server"),
        h("th", { scope: "col", class: "hide-md" }, "Activity"),
        h("th", { scope: "col", class: "hide-sm" }, "Memory"),
        h("th", { scope: "col", class: "hide-md" }, "Address"),
        h("th", { scope: "col", class: "right" }, "Status"),
      )),
      tbody,
    ),
  );
  const body = h("div", {}, table);

  search.addEventListener("input", () => {
    query = search.value.trim().toLowerCase();
    draw();
  });

  appendAll(root, 
    h("section", { class: "hero" },
      wordmark(),
      h("p", { class: "hero-tag" }, "Every game server you run, in one place."),
      h("p", { class: "hero-sub" }, "Local machines and remote hosts side by side. Start, stop, configure and update from here."),
    ),
    tabs,
    h("div", { class: "toolbar" },
      h("label", { class: "search" }, icon("search"), search, h("kbd", { "aria-hidden": "true" }, "/")),
      h("span", { class: "grow" }),
      me.is_admin || me.max_servers > 0 ? h("a", { class: "btn primary", href: "#/servers/new" }, icon("plus"), "New server") : null,
    ),
    body,
  );

  function drawTabs() {
    replace(tabs, FILTERS.map(([key, label, test]) =>
      h("button", { class: "tab", role: "tab", type: "button", "aria-selected": String(filter === key),
        onclick: () => { filter = key; draw(); } },
        label, servers ? h("span", { class: "count" }, `(${servers.filter(test).length})`) : null)));
  }

  function draw() {
    drawTabs();
    if (!servers) return;
    if (!servers.length) {
      replace(body, emptyState(me));
      return;
    }
    if (!body.contains(table)) replace(body, table);
    const test = FILTERS.find(([k]) => k === filter)[2];
    const rows = servers.filter(test).filter((s) =>
      !query || [s.name, s.node.name, s.template.name, s.owner.username, s.address, s.modpack?.name]
        .some((v) => v && v.toLowerCase().includes(query)));
    if (!rows.length) {
      replace(tbody, h("tr", {}, h("td", { colspan: 6, class: "muted", style: { padding: "28px 12px" } },
        query ? `No servers match “${query}”.` : "No servers in this view.")));
      return;
    }
    replace(tbody, rows.map((s, i) => row(s, i + 1)));
  }

  function row(s, rank) {
    const st = s.status;
    const used = st.memory || 0;
    const tr = h("tr", { class: "row", tabindex: "0", onclick: () => navigate(`/servers/${s.id}`),
      onkeydown: (e) => e.key === "Enter" && navigate(`/servers/${s.id}`) },
      h("td", { class: "rank" }, rank),
      h("td", {},
        h("div", { class: "cell-flex" },
          safeUrl(s.modpack?.icon_url) ? h("img", { class: "pack-icon", src: safeUrl(s.modpack.icon_url), alt: "", loading: "lazy" }) : null,
          h("div", { style: { minWidth: 0 } },
            h("div", { class: "name" }, s.name),
            h("div", { class: "sub" }, `${s.node.name}/${s.modpack ? s.modpack.name : s.template.name}`,
              s.modpack?.update ? h("span", { class: "badge info", style: { marginLeft: "8px", height: "18px" } }, "update") : null),
          ),
        ),
      ),
      h("td", { class: "hide-md" }, activityBars(st.history || [])),
      h("td", { class: "figure hide-sm" }, st.state === "running" ? `${bytes(used)} / ${mb(s.memory_mb)}` : h("span", { class: "faint" }, mb(s.memory_mb))),
      h("td", { class: "figure hide-md" }, s.address ? h("span", { class: "cell-flex", style: { gap: "4px" } }, s.address, copyButton(s.address, "Copy address")) : "—"),
      h("td", { class: "right" }, status(st.state)),
    );
    return tr;
  }

  async function load() {
    try {
      servers = await get("/servers");
      draw();
    } catch (err) {
      fail(err);
    }
  }

  drawTabs();
  await load();
  const timer = setInterval(() => document.visibilityState === "visible" && load(), 10_000);
  return () => clearInterval(timer);
}

function emptyState(me) {
  if (!me.is_admin) {
    return h("div", { class: "empty" },
      h("h2", {}, "No servers yet"),
      h("p", {}, "When an admin gives you a server, it shows up here with its status and console."));
  }
  return h("div", { class: "empty" },
    h("h2", {}, "Start your first server"),
    h("p", {}, "Hearth runs servers on nodes — any machine with Docker and the Hearth agent, at home or rented."),
    h("div", { class: "steps" },
      h("span", {}, "1  Add a node and its ports"),
      h("span", {}, "2  Create a server on it"),
      h("span", {}, "3  Share the address with friends")),
    h("div", { style: { display: "flex", gap: "8px", justifyContent: "center" } },
      h("a", { class: "btn primary", href: "#/admin/nodes/new" }, icon("plus"), "Add a node"),
      h("a", { class: "btn", href: "#/servers/new" }, "Create a server")),
  );
}
