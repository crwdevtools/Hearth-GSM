// Shell + hash router.

import { get, post, setUnauthorizedHandler } from "./api.js";
import { clear, h, icon, replace } from "./dom.js";
import { fail } from "./ui.js";
import { renderAuth } from "./views/auth.js";
import { renderDashboard } from "./views/dashboard.js";
import { renderServer } from "./views/server.js";
import { renderCreate } from "./views/create.js";
import { renderModpacks } from "./views/modpacks.js";
import { renderAdmin } from "./views/admin.js";
import { renderAccount } from "./views/account.js";
import { renderWelcome } from "./views/welcome.js";
import { currentTheme, setTheme, startTranslation } from "./i18n.js";

startTranslation();
matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", () => currentTheme() === "system" && setTheme("system"));

const app = document.getElementById("app");
const state = { me: null, cleanup: null, crumbs: [] };

const routes = [
  [/^\/$/, renderDashboard, "servers"],
  [/^\/servers\/new$/, renderCreate, "servers", { creator: true }],
  [/^\/servers\/(\d+)(?:\/(\w+))?$/, renderServer, "servers"],
  [/^\/modpacks$/, renderModpacks, "modpacks"],
  [/^\/admin(?:\/(\w+))?(?:\/(\w+))?$/, renderAdmin, "admin", { admin: true }],
  [/^\/account$/, renderAccount, "account"],
  [/^\/welcome$/, renderWelcome, "admin", { admin: true }],
];

export function navigate(path) {
  if (location.hash.slice(1) === path) route();
  else location.hash = path;
}

function parseHash() {
  const raw = location.hash.slice(1) || "/";
  const [path, qs] = raw.split("?");
  return { path, query: new URLSearchParams(qs || "") };
}

function topbar(section) {
  const me = state.me;
  const nav = h("nav", { class: "nav", "aria-label": "Main" },
    h("a", { href: "#/", "aria-current": section === "servers" ? "page" : null }, icon("server"), "Servers"),
    h("a", { href: "#/modpacks", "aria-current": section === "modpacks" ? "page" : null }, icon("box"), "Modpacks"),
    me.is_admin ? h("a", { href: "#/admin", "aria-current": section === "admin" ? "page" : null }, icon("sliders"), "Admin") : null,
    h("span", { class: "divider", "aria-hidden": "true" }),
    h("a", { href: "#/account", class: "who", "aria-current": section === "account" ? "page" : null, title: "Account" }, me.username),
    h("button", { class: "navbtn", type: "button", onclick: logout, title: "Sign out" }, icon("out"), h("span", { class: "sr-only" }, "Sign out")),
  );
  const toggle = h("button", { class: "btn ghost icon-only menu-toggle", type: "button", "aria-label": "Menu", "aria-expanded": "false" }, icon("menu"));
  toggle.addEventListener("click", () => {
    const open = nav.classList.toggle("open");
    toggle.setAttribute("aria-expanded", String(open));
  });
  nav.addEventListener("click", (e) => e.target.closest("a") && nav.classList.remove("open"));
  const crumbs = h("div", { class: "crumbs", id: "crumbs" });
  return h("header", { class: "topbar" },
    h("div", { class: "topbar-inner" },
      h("a", { href: "#/", class: "brand", "aria-label": "Hearth home" }, h("img", { class: "brand-mark", src: "/assets/favicon.svg", alt: "", width: 22, height: 22 }), "Hearth"),
      crumbs,
      toggle,
      nav,
    ),
  );
}

export function setCrumbs(items) {
  const el = document.getElementById("crumbs");
  if (!el) return;
  replace(el, items.flatMap((item, i) => [
    h("span", { class: "sep", "aria-hidden": "true" }, "/"),
    item.href && i < items.length - 1 ? h("a", { href: item.href }, item.label) : h("span", { class: "current" }, item.label),
  ]));
}

async function logout() {
  try {
    await post("/auth/logout");
  } finally {
    state.me = null;
    boot();
  }
}

async function route() {
  if (!state.me) return;
  if (typeof state.cleanup === "function") state.cleanup();
  state.cleanup = null;
  const { path, query } = parseHash();
  const match = routes.find(([re]) => re.test(path));
  const [re, render, section, opts] = match || routes[0];
  const params = (path.match(re) || []).slice(1);
  if (opts?.admin && !state.me.is_admin) return navigate("/");
  if (opts?.creator && !state.me.is_admin && !state.me.max_servers) return navigate("/");

  clear(app);
  const main = h("main", { id: "main", class: "view" });
  app.append(topbar(section), main);
  setCrumbs([]);
  window.scrollTo(0, 0);
  try {
    state.cleanup = await render(main, { params, query, me: state.me, navigate, setCrumbs, refreshMe: boot });
  } catch (err) {
    fail(err);
  }
}

async function boot() {
  let auth;
  try {
    auth = await get("/auth/state");
  } catch (err) {
    replace(app, h("main", {}, h("div", { class: "empty" }, h("h2", {}, "Hearth is unreachable"), h("p", {}, err.message))));
    return;
  }
  state.me = auth.user;
  if (!state.me) {
    if (typeof state.cleanup === "function") state.cleanup();
    clear(app);
    renderAuth(app, { needsSetup: auth.needs_setup, setupAllowed: auth.setup_allowed !== false, onDone: boot });
    return;
  }
  route();
}

setUnauthorizedHandler(() => {
  if (state.me) {
    state.me = null;
    boot();
  }
});

// "/" focuses the page's search field, like skills.sh.
document.addEventListener("keydown", (e) => {
  if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
  const tag = document.activeElement?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  const search = document.querySelector("[data-search]");
  if (search) {
    e.preventDefault();
    search.focus();
  }
});

window.addEventListener("hashchange", route);
boot();
