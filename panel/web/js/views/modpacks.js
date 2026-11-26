// Modpack leaderboard: Modrinth and CurseForge, live from their APIs.

import { get } from "../api.js";
import { h, icon, replace, appendAll } from "../dom.js";
import { ago, compact, fail, notice, safeUrl, skeletonRows } from "../ui.js";

const SORTS = [["downloads", "Most downloaded"], ["updated", "Recently updated"], ["newest", "Newest"]];
const LOADERS = [["", "Any loader"], ["neoforge", "NeoForge"], ["forge", "Forge"], ["fabric", "Fabric"], ["quilt", "Quilt"]];
const PAGE = 25;

export async function renderModpacks(root, { me, setCrumbs }) {
  setCrumbs([{ label: "Modpacks" }]);
  const providers = await get("/modpacks/providers").catch(() => ({ modrinth: true, curseforge: false }));
  const state = { provider: "modrinth", sort: "downloads", query: "", loader: "", game: "", offset: 0, total: 0, hits: [] };
  let req = 0;
  let expanded = null;

  const providerTabs = h("div", { class: "tabs", role: "tablist", "aria-label": "Modpack source" });
  const sortSelect = h("select", { "aria-label": "Sort", style: { width: "auto" } }, SORTS.map(([v, l]) => h("option", { value: v }, l)));
  const loaderSelect = h("select", { "aria-label": "Loader", style: { width: "auto" } }, LOADERS.map(([v, l]) => h("option", { value: v }, l)));
  const gameInput = h("input", { placeholder: "MC version", "aria-label": "Minecraft version", class: "mono", style: { width: "120px" }, pattern: "\\d+\\.\\d+(\\.\\d+)?" });
  const search = h("input", { type: "search", placeholder: "Search modpacks...", "data-search": true, "aria-label": "Search modpacks" });
  const tbody = h("tbody", {});
  const more = h("div", { style: { display: "flex", justifyContent: "center", marginTop: "20px" } });
  const body = h("div", {});

  appendAll(root, 
    h("div", { class: "page-head" }, h("div", {},
      h("h1", {}, "Modpacks"),
      h("p", { class: "muted", style: { marginTop: "8px" } }, "Live from Modrinth and CurseForge. Deploy any pack at any version; Hearth tracks new releases."))),
    providerTabs,
    h("div", { class: "toolbar" },
      h("label", { class: "search" }, icon("search"), search, h("kbd", { "aria-hidden": "true" }, "/")),
      h("span", { class: "grow" }), gameInput, loaderSelect, sortSelect),
    body);

  let timer;
  const refetch = () => { state.offset = 0; state.hits = []; load(); };
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { state.query = search.value.trim(); refetch(); }, 300); });
  sortSelect.addEventListener("change", () => { state.sort = sortSelect.value; refetch(); });
  loaderSelect.addEventListener("change", () => { state.loader = loaderSelect.value; refetch(); });
  gameInput.addEventListener("change", () => {
    const v = gameInput.value.trim();
    if (v && !/^\d+\.\d+(\.\d+)?$/.test(v)) { gameInput.setAttribute("aria-invalid", "true"); return; }
    gameInput.removeAttribute("aria-invalid");
    state.game = v;
    refetch();
  });

  function drawTabs() {
    replace(providerTabs, [["modrinth", "Modrinth"], ["curseforge", "CurseForge"]].map(([key, label]) =>
      h("button", { class: "tab", role: "tab", type: "button", "aria-selected": String(state.provider === key),
        onclick: () => { state.provider = key; drawTabs(); refetch(); } },
        label, state.provider === key && state.total ? h("span", { class: "count" }, `(${state.total.toLocaleString()})`) : null)));
  }

  function table() {
    return h("div", { class: "table-wrap" }, h("table", { class: "board" },
      h("thead", {}, h("tr", {},
        h("th", { class: "rank", scope: "col" }, "#"),
        h("th", { scope: "col" }, "Modpack"),
        h("th", { scope: "col", class: "hide-md" }, "Minecraft"),
        h("th", { scope: "col", class: "hide-sm" }, "Updated"),
        h("th", { scope: "col", class: "right" }, "Downloads"))),
      tbody));
  }

  async function load() {
    const id = ++req;
    if (state.provider === "curseforge" && !providers.curseforge) {
      replace(body, notice("warn", h("span", {}, h("strong", {}, "CurseForge needs an API key. "),
        me.is_admin ? "Add one under Admin → Settings — get a free key at console.curseforge.com." : "Ask an admin to add one."),
        me.is_admin ? h("a", { class: "btn sm", href: "#/admin/settings" }, "Open settings") : null));
      state.total = 0;
      drawTabs();
      return;
    }
    if (!state.hits.length) {
      replace(body, table(), more);
      replace(tbody, skeletonRows(5, 8));
      replace(more);
    }
    try {
      const data = await get("/modpacks/search", { provider: state.provider, query: state.query, sort: state.query ? "relevance" : state.sort,
        loader: state.loader, game_version: state.game, offset: state.offset, limit: PAGE });
      if (id !== req) return;
      state.total = data.total;
      state.hits = state.hits.concat(data.hits);
      drawTabs();
      draw();
    } catch (err) {
      if (id !== req) return;
      if (state.hits.length) { fail(err); draw(); } else replace(body, notice("bad", err.message));
    }
  }

  function draw() {
    if (!state.hits.length) {
      replace(tbody, h("tr", {}, h("td", { colspan: 5, class: "muted", style: { padding: "28px 12px" } }, "No modpacks match these filters.")));
      replace(more);
      return;
    }
    replace(tbody, state.hits.flatMap((p, i) => [packRow(p, i + 1), expanded === p.id ? versionsRow(p) : null]));
    const remaining = state.total - state.hits.length;
    if (remaining > 0) {
      const btn = h("button", { class: "btn", type: "button", onclick: () => { state.offset += PAGE; btn.setAttribute("aria-busy", "true"); load(); } },
        `Show more`, h("span", { class: "faint mono" }, `+${compact(remaining)}`));
      replace(more, btn);
    } else replace(more);
  }

  function packRow(p, rank) {
    const toggle = () => { expanded = expanded === p.id ? null : p.id; draw(); };
    return h("tr", { class: "row", tabindex: "0", "aria-expanded": String(expanded === p.id), onclick: toggle, onkeydown: (e) => e.key === "Enter" && toggle() },
      h("td", { class: "rank" }, rank),
      h("td", {}, h("div", { class: "cell-flex" },
        safeUrl(p.icon_url) ? h("img", { class: "pack-icon", src: safeUrl(p.icon_url), alt: "", loading: "lazy" }) : h("span", { class: "pack-icon" }),
        h("div", { style: { minWidth: 0 } },
          h("div", { class: "name" }, p.name),
          h("div", { class: "sub" }, `${p.author || p.provider}/${p.slug || p.id}`),
          h("div", { class: "faint hide-sm", style: { fontSize: "13px", marginTop: "3px", maxWidth: "64ch", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, p.summary)))),
      h("td", { class: "hide-md" }, h("div", { class: "chips" },
        (p.game_versions || []).slice(0, 3).map((v) => h("span", { class: "chip" }, v)),
        (p.loaders || []).slice(0, 2).map((l) => h("span", { class: "chip", style: { color: "var(--fg-faint)" } }, l)))),
      h("td", { class: "figure hide-sm faint" }, ago(p.updated)),
      h("td", { class: "right figure" }, compact(p.downloads)));
  }

  function versionsRow(p) {
    const cell = h("td", { colspan: 5 }, h("div", { style: { padding: "16px 20px" } }, h("div", { class: "skeleton", style: { width: "40%" } })));
    get(`/modpacks/${p.provider}/${p.id}/versions`).then((versions) => {
      const deploy = (v) => {
        const qs = new URLSearchParams({ provider: p.provider, project: p.id, name: p.name, slug: p.slug || "", icon: p.icon_url || "" });
        if (v) qs.set("version", v.id);
        return `#/servers/new?${qs}`;
      };
      replace(cell, h("div", { style: { padding: "16px 20px 20px" } },
        h("div", { class: "toolbar" },
          h("span", { class: "muted" }, `${versions.length} versions`),
          h("span", { class: "grow" }),
          safeUrl(p.url) ? h("a", { class: "btn sm ghost", href: safeUrl(p.url), target: "_blank", rel: "noopener noreferrer" }, "View page", icon("external")) : null,
          me.is_admin || me.max_servers > 0 ? h("a", { class: "btn sm primary", href: deploy(null) }, "Deploy latest") : null),
        h("table", { class: "board" }, h("tbody", {}, versions.slice(0, 12).map((v) => h("tr", {},
          h("td", {}, h("div", { class: "name" }, v.name), h("div", { class: "sub" }, (v.game_versions || []).slice(0, 3).join(", "), v.loaders?.length ? ` · ${v.loaders.join(", ")}` : "")),
          h("td", { class: "hide-sm" }, v.type !== "release" ? h("span", { class: `badge ${v.type === "beta" ? "warn" : "bad"}` }, v.type) : h("span", { class: "badge" }, "release")),
          h("td", { class: "figure faint hide-sm" }, ago(v.published)),
          h("td", { class: "right" }, me.is_admin || me.max_servers > 0 ? h("a", { class: "btn sm", href: deploy(v) }, "Deploy") : null))))),
        versions.length > 12 ? h("p", { class: "faint", style: { fontSize: "13px", marginTop: "10px" } }, `Older versions are selectable when you deploy.`) : null));
    }).catch((err) => replace(cell, h("div", { style: { padding: "16px 20px" } }, notice("bad", err.message))));
    return h("tr", { class: "expand" }, cell);
  }

  drawTabs();
  load();
}
