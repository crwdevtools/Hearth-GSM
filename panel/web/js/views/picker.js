// Minecraft source editor: server software + version, or a modpack + version from Modrinth/CurseForge.

import { get } from "../api.js";
import { h, icon, replace } from "../dom.js";
import { compact, field, notice, safeUrl } from "../ui.js";

export const SOFTWARE = [
  ["PAPER", "Paper"], ["VANILLA", "Vanilla"], ["PURPUR", "Purpur"], ["FABRIC", "Fabric"],
  ["QUILT", "Quilt"], ["FORGE", "Forge"], ["NEOFORGE", "NeoForge"],
];
const SNAPSHOT_TYPES = new Set(["VANILLA", "FABRIC", "QUILT"]);

function segmented(options, current, onPick, label) {
  const wrap = h("div", { class: "segmented", role: "group", "aria-label": label });
  // Update pressed state in place so the clicked button keeps keyboard focus.
  const buttons = options.map(([v, text, disabled]) =>
    h("button", { type: "button", "aria-pressed": String(v === current), disabled: !!disabled, title: disabled || null,
      onclick: () => {
        buttons.forEach((b, i) => b.setAttribute("aria-pressed", String(options[i][0] === v)));
        onPick(v);
      } }, text));
  wrap.append(...buttons);
  return wrap;
}

function versionLabel(v) {
  const tag = v.type && v.type !== "release" ? ` · ${v.type}` : "";
  const mc = v.game_versions?.length ? ` · MC ${v.game_versions[0]}` : "";
  return `${v.name}${mc}${tag}`;
}

/**
 * initial: { kind: "software", server_type, version } | { kind: "modpack", modpack: {...}, auto_update }
 * allowLatest: show "Latest release" as the default modpack version (create flow)
 */
export function sourceEditor({ initial, allowLatest = true, onChange = () => {} }) {
  const value = {
    kind: initial?.kind || "software",
    server_type: initial?.server_type || "PAPER",
    version: initial?.version || "LATEST",
    snapshots: false,
    pack: initial?.modpack || null, // {provider, project_id, name, icon_url, slug, version_id}
    provider: initial?.modpack?.provider || "modrinth",
    auto_update: initial?.auto_update ?? true,
  };
  let providers = { modrinth: true, curseforge: false };
  const initialVersion = value.version;
  const changed = () => onChange(value);

  const root = h("div", { class: "section-body" });
  const kindSwitch = segmented([["software", "Server software"], ["modpack", "Modpack"]], value.kind,
    (k) => { value.kind = k; drawBody(); changed(); }, "Source type");
  const body = h("div", { class: "section-body" });
  root.append(kindSwitch, body);

  // ----- software ------------------------------------------------------------------------------
  const versionSelect = h("select", { "aria-label": "Minecraft version" });
  const versionHint = h("span", { class: "hint" });
  const snapshotBox = h("input", { type: "checkbox" });
  const snapshotRow = h("label", { class: "check" }, snapshotBox, h("span", {}, "Include snapshots"));
  snapshotBox.addEventListener("change", () => { value.snapshots = snapshotBox.checked; loadVersions(); });
  versionSelect.addEventListener("change", () => { value.version = versionSelect.value; changed(); });
  let versionReq = 0;

  async function loadVersions() {
    const req = ++versionReq;
    snapshotRow.style.display = SNAPSHOT_TYPES.has(value.server_type) ? "" : "none";
    replace(versionSelect, h("option", { value: value.version }, value.version === "LATEST" ? "Latest" : value.version));
    versionSelect.disabled = true;
    versionHint.textContent = "Loading versions…";
    try {
      const list = await get("/minecraft/versions", { type: value.server_type, snapshots: value.snapshots });
      if (req !== versionReq) return;
      const ids = list.map((v) => v.id);
      // A version missing from this list (a snapshot, an old build) stays selectable rather than
      // being silently swapped for LATEST — that would change the server on the next save.
      const keep = value.version !== "LATEST" && !ids.includes(value.version) && value.version === initialVersion;
      if (value.version !== "LATEST" && !ids.includes(value.version) && !keep) value.version = "LATEST";
      replace(versionSelect,
        h("option", { value: "LATEST" }, `Latest${ids[0] ? ` (${ids[0]})` : ""}`),
        keep ? h("option", { value: value.version }, `${value.version} (current)`) : null,
        list.map((v) => h("option", { value: v.id }, v.type === "snapshot" ? `${v.id} · snapshot` : v.id)));
      versionSelect.value = value.version;
      versionHint.textContent = `${list.length} versions available for ${SOFTWARE.find(([k]) => k === value.server_type)[1]}.`;
    } catch (err) {
      if (req !== versionReq) return;
      versionHint.textContent = `Couldn't load the version list (${err.message}). Latest will be used.`;
      value.version = "LATEST";
    } finally {
      if (req === versionReq) versionSelect.disabled = false;
      changed();
    }
  }

  function drawSoftware() {
    replace(body,
      h("div", { class: "field" }, h("span", { class: "label" }, "Server software"),
        segmented(SOFTWARE, value.server_type, (t) => { value.server_type = t; loadVersions(); changed(); }, "Server software"),
        h("span", { class: "hint" }, "Paper and Purpur run plugins. Fabric, Quilt, Forge and NeoForge run mods.")),
      h("div", { class: "field" }, h("label", { for: "mc-version" }, "Minecraft version"), versionSelect, versionHint, snapshotRow),
    );
    versionSelect.id = "mc-version";
    loadVersions();
  }

  // ----- modpack -------------------------------------------------------------------------------
  const searchInput = h("input", { type: "search", placeholder: "Search modpacks…", "aria-label": "Search modpacks" });
  const results = h("div", { class: "picker-results", role: "radiogroup", "aria-label": "Modpacks" });
  const packVersion = h("select", { id: "pack-version", "aria-label": "Modpack version" });
  const packVersionHint = h("span", { class: "hint" });
  const autoBox = h("input", { type: "checkbox", checked: value.auto_update });
  autoBox.addEventListener("change", () => { value.auto_update = autoBox.checked; changed(); });
  packVersion.addEventListener("change", () => {
    value.pack.version_id = packVersion.value || null;
    changed();
  });
  let searchTimer = null;
  let searchReq = 0;
  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(search, 300);
  });

  async function search() {
    const req = ++searchReq;
    replace(results, h("div", { class: "radio-item" }, h("div", { class: "skeleton", style: { width: "50%" } })));
    try {
      const data = await get("/modpacks/search", { provider: value.provider, query: searchInput.value.trim(),
        sort: searchInput.value.trim() ? "relevance" : "downloads", limit: 15 });
      if (req !== searchReq) return;
      if (!data.hits.length) {
        replace(results, h("div", { class: "radio-item muted" }, "No modpacks match that search."));
        return;
      }
      replace(results, data.hits.map((p) => h("label", { class: "radio-item" },
        h("input", { type: "radio", name: "pack", value: p.id, onchange: () => choosePack(p) }),
        safeUrl(p.icon_url) ? h("img", { class: "pack-icon", src: safeUrl(p.icon_url), alt: "", loading: "lazy" }) : h("span", { class: "pack-icon" }),
        h("span", { class: "body" },
          h("span", { class: "t" }, p.name),
          h("span", { class: "d", style: { display: "block" } }, p.summary)),
        h("span", { class: "figure mono faint", style: { fontSize: "12.5px", whiteSpace: "nowrap" } },
          p.game_versions?.[0] ? `${p.game_versions[0]} · ` : "", `${compact(p.downloads)}`))));
    } catch (err) {
      if (req !== searchReq) return;
      replace(results, h("div", { class: "radio-item" }, notice("bad", err.message)));
    }
  }

  function choosePack(p) {
    value.pack = { provider: value.provider, project_id: p.id, name: p.name, icon_url: p.icon_url, slug: p.slug, version_id: null };
    drawModpack();
    changed();
  }

  async function loadPackVersions() {
    replace(packVersion, h("option", {}, "Loading…"));
    packVersion.disabled = true;
    try {
      const versions = await get(`/modpacks/${value.pack.provider}/${value.pack.project_id}/versions`);
      const latest = versions.find((v) => v.type === "release") || versions[0];
      replace(packVersion,
        allowLatest ? h("option", { value: "" }, `Latest release${latest ? ` (${latest.name})` : ""}`) : null,
        versions.map((v) => h("option", { value: v.id }, versionLabel(v))));
      if (value.pack.version_id && !versions.some((v) => v.id === value.pack.version_id)) {
        packVersion.prepend(h("option", { value: value.pack.version_id }, `${value.pack.version_name || value.pack.version_id} (current, no longer listed)`));
      }
      packVersion.value = value.pack.version_id || (allowLatest ? "" : versions[0]?.id || "");
      if (!allowLatest && !value.pack.version_id) value.pack.version_id = packVersion.value || null;
      packVersionHint.textContent = `${versions.length} versions. Any version can be selected; older ones stay pinned unless auto-update is on.`;
    } catch (err) {
      packVersionHint.textContent = err.message;
    } finally {
      packVersion.disabled = false;
      changed();
    }
  }

  function drawModpack() {
    const providerSwitch = segmented(
      [["modrinth", "Modrinth"], ["curseforge", "CurseForge", providers.curseforge ? null : "Add a CurseForge API key in Admin → Settings"]],
      value.provider, (p) => { value.provider = p; value.pack = null; drawModpack(); changed(); }, "Modpack source");
    if (value.pack) {
      replace(body,
        h("div", { class: "picked" },
          safeUrl(value.pack.icon_url) ? h("img", { class: "pack-icon lg", src: safeUrl(value.pack.icon_url), alt: "" }) : h("span", { class: "pack-icon lg" }),
          h("div", { class: "body" },
            h("div", { class: "t" }, value.pack.name),
            h("div", { class: "faint mono", style: { fontSize: "12.5px" } }, `${value.pack.provider}/${value.pack.slug || value.pack.project_id}`)),
          h("button", { class: "btn sm", type: "button", onclick: () => { value.pack = null; drawModpack(); changed(); } }, "Change")),
        field("Modpack version", packVersion, {}),
        packVersionHint,
        h("label", { class: "check" }, autoBox,
          h("span", {}, "Update automatically",
            h("small", {}, "Hearth checks for new releases on a schedule and restarts the server onto them."))),
      );
      loadPackVersions();
      return;
    }
    replace(body,
      providerSwitch,
      value.provider === "curseforge" && !providers.curseforge
        ? notice("warn", "CurseForge needs an API key. An admin can add one under Admin → Settings.")
        : h("div", { class: "section-body" },
          h("label", { class: "search", style: { maxWidth: "none" } }, icon("search"), searchInput),
          results),
    );
    if (value.provider !== "curseforge" || providers.curseforge) search();
  }

  function drawBody() {
    if (value.kind === "software") drawSoftware();
    else drawModpack();
  }

  get("/modpacks/providers").then((p) => { providers = p; if (value.kind === "modpack" && !value.pack) drawModpack(); }).catch(() => {});
  drawBody();

  return {
    el: root,
    value() {
      if (value.kind === "software") return { kind: "software", server_type: value.server_type, version: value.version || "LATEST" };
      if (!value.pack) return null;
      return {
        kind: "modpack",
        modpack: { provider: value.pack.provider, project_id: value.pack.project_id, version_id: value.pack.version_id || null },
        auto_update: value.auto_update,
      };
    },
  };
}
