import { del, get, post } from "../api.js";
import { h, icon, replace } from "../dom.js";
import { ago, busy, confirmDialog, copyButton, emptyRow, field, notice, section, toast } from "../ui.js";
import { currentLanguage, currentTheme, LANGUAGES, setLanguage, setTheme } from "../i18n.js";

export async function renderAccount(root, { me, setCrumbs, refreshMe }) {
  setCrumbs([{ label: "Account" }]);
  root.append(
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, me.username),
      h("p", { class: "muted", style: { marginTop: "8px" } }, me.is_admin ? "Administrator"
        : me.max_servers ? `Member · can create up to ${me.max_servers} server${me.max_servers === 1 ? "" : "s"}` : "Member"))),
    appearanceSection(), passwordSection(), await twoFactorSection(me, refreshMe), await tokensSection(), sessionsSection());
}

function appearanceSection() {
  const theme = h("select", { id: "pref-theme" },
    [["dark", "Dark"], ["light", "Light"], ["system", "Match system"]].map(([v, label]) => h("option", { value: v, selected: currentTheme() === v }, label)));
  theme.addEventListener("change", () => setTheme(theme.value));
  const lang = h("select", { id: "pref-lang" },
    LANGUAGES.map(([v, label]) => h("option", { value: v, selected: currentLanguage() === v }, label)));
  lang.addEventListener("change", () => setLanguage(lang.value));
  return section("Appearance", "Saved in this browser.",
    h("div", { class: "grid-2", style: { maxWidth: "520px" } }, field("Theme", theme), field("Language", lang)));
}

function sessionsSection() {
  const out = h("button", { class: "btn", type: "button" }, icon("out"), "Sign out everywhere");
  out.addEventListener("click", () => busy(out, async () => {
    if (!(await confirmDialog({ title: "Sign out everywhere?", body: "Every browser signed in to your account, including this one, is signed out. API tokens keep working.", confirm: "Sign out everywhere" }))) return;
    await post("/auth/logout-everywhere");
    location.hash = "#/";
    location.reload();
  }));
  return section("Sessions", "Lost a device, or signed in on a shared computer? End every session at once.", h("div", {}, out));
}

function passwordSection() {
  const current = h("input", { id: "pw-cur", type: "password", autocomplete: "current-password", required: true });
  const next = h("input", { id: "pw-new", type: "password", autocomplete: "new-password", minlength: 10, required: true });
  const save = h("button", { class: "btn primary", type: "submit" }, "Change password");
  const form = h("form", { class: "section-body", style: { maxWidth: "420px" } },
    field("Current password", current), field("New password", next, { hint: "At least 10 characters." }), h("div", {}, save));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    busy(save, async () => {
      await post("/auth/password", { current_password: current.value, new_password: next.value });
      form.reset();
      toast("Password changed. Other sessions were signed out.");
    });
  });
  return section("Password", "Changing it signs you out everywhere else.", form);
}

async function twoFactorSection(me, refreshMe) {
  const body = h("div", { class: "section-body" });
  const draw = (enabled) => {
    if (enabled) {
      const password = h("input", { id: "tf-pw", type: "password", autocomplete: "current-password" });
      const code = h("input", { id: "tf-code", inputmode: "numeric", class: "mono", maxlength: 8, autocomplete: "one-time-code" });
      const off = h("button", { class: "btn danger", type: "submit" }, "Turn off");
      const form = h("form", { class: "section-body", style: { maxWidth: "420px" } },
        notice("info", h("span", {}, h("strong", {}, "On. "), "Signing in asks for a code from your authenticator app.")),
        h("div", { class: "grid-2" }, field("Password", password), field("Current code", code)), h("div", {}, off));
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        busy(off, async () => {
          await post("/account/2fa/disable", { password: password.value, code: code.value.replace(/\s/g, "") });
          toast("Two-factor sign-in is off.");
          draw(false);
        });
      });
      return replace(body, form);
    }
    const start = h("button", { class: "btn primary", type: "button" }, icon("key"), "Set up two-factor sign-in");
    start.addEventListener("click", () => busy(start, async () => {
      const setup = await post("/account/2fa/setup");
      const code = h("input", { id: "tf-confirm", inputmode: "numeric", class: "mono", maxlength: 8, autocomplete: "one-time-code", required: true });
      const confirm = h("button", { class: "btn primary", type: "submit" }, "Confirm and turn on");
      const form = h("form", { class: "section-body" },
        h("ol", { class: "steps-list" },
          h("li", {}, "Open an authenticator app (Google Authenticator, Authy, 1Password, Bitwarden…)."),
          h("li", {}, "Scan this code, or enter the key by hand."),
          h("li", {}, "Type the 6-digit code it shows.")),
        h("div", { class: "totp-setup" },
          setup.qr_svg ? h("img", { class: "qr", alt: "QR code for your authenticator app", width: 180, height: 180,
            src: `data:image/svg+xml;base64,${btoa(setup.qr_svg)}` }) : null,
          h("div", { class: "section-body" },
            h("div", { class: "field" }, h("span", { class: "label" }, "Key"),
              h("div", { class: "cell-flex", style: { gap: "4px" } }, h("code", { class: "totp-key" }, setup.secret.replace(/(.{4})/g, "$1 ").trim()), copyButton(setup.secret, "Copy key"))),
            field("Code from the app", code), h("div", {}, confirm))));
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        busy(confirm, async () => {
          await post("/account/2fa/enable", { code: code.value.replace(/\s/g, "") });
          toast("Two-factor sign-in is on.");
          refreshMe?.();
          draw(true);
        });
      });
      replace(body, form);
      code.focus();
    }));
    replace(body, h("p", { class: "muted" }, "Protect your account with a code from your phone in addition to your password."), h("div", {}, start));
  };
  draw(me.totp_enabled);
  return section("Two-factor sign-in", "Recommended for admins: whoever controls the panel controls every server.", body);
}

async function tokensSection() {
  const tbody = h("tbody", {});
  const name = h("input", { id: "tok-name", placeholder: "e.g. billing, Discord bot", maxlength: 64, required: true });
  const create = h("button", { class: "btn", type: "submit" }, icon("plus"), "Create token");
  const reveal = h("div", {});
  const form = h("form", { class: "toolbar", style: { margin: 0, alignItems: "end" } }, field("Name", name), create);

  async function load() {
    const rows = await get("/account/tokens");
    replace(tbody, rows.length ? rows.map((t) => {
      const rm = h("button", { class: "btn sm ghost icon-only", type: "button", title: "Revoke", "aria-label": `Revoke ${t.name}` }, icon("trash"));
      rm.addEventListener("click", () => busy(rm, async () => {
        if (!(await confirmDialog({ title: `Revoke “${t.name}”?`, body: "Anything using it stops working immediately.", confirm: "Revoke", danger: true }))) return;
        await del(`/account/tokens/${t.id}`);
        load();
      }));
      return h("tr", {}, h("td", {}, h("div", { class: "name" }, t.name), h("div", { class: "sub" }, `hearth_${t.prefix}_…`)),
        h("td", { class: "faint hide-sm" }, `created ${ago(t.created_at)}`),
        h("td", { class: "faint" }, t.last_used ? `used ${ago(t.last_used)}` : "never used"),
        h("td", { class: "right" }, rm));
    }) : emptyRow(4, "No tokens."));
  }
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    busy(create, async () => {
      const r = await post("/account/tokens", { name: name.value.trim() });
      name.value = "";
      replace(reveal, notice("warn", h("div", { class: "section-body", style: { gap: "8px" } },
        h("strong", {}, "Copy it now: it won't be shown again."),
        h("div", { class: "cell-flex", style: { gap: "4px" } }, h("code", { class: "totp-key" }, r.token), copyButton(r.token, "Copy token")))));
      load();
    });
  });
  await load();
  return section("API tokens", "For scripts, bots and billing systems. A token acts as you: send it as “Authorization: Bearer <token>”. API docs are at /api/docs.",
    form, reveal, h("div", { class: "table-wrap" }, h("table", { class: "board" }, tbody)));
}
