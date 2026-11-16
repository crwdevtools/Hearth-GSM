import { post } from "../api.js";
import { h } from "../dom.js";
import { busy, field, wordmark } from "../ui.js";

export function renderAuth(root, { needsSetup, setupAllowed = true, onDone }) {
  const username = h("input", { name: "username", autocomplete: "username", required: true, minlength: 3, maxlength: 32,
    pattern: "[A-Za-z0-9_.\\-]+", spellcheck: "false", autocapitalize: "none" });
  const password = h("input", { name: "password", type: "password", required: true,
    autocomplete: needsSetup ? "new-password" : "current-password", minlength: needsSetup ? 10 : 1 });
  const code = h("input", { name: "totp", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 8,
    pattern: "[0-9 ]{6,8}", class: "mono", placeholder: "123 456" });
  const codeField = field("Authentication code", code, { hint: "From your authenticator app." });
  codeField.hidden = true;
  const error = h("p", { class: "error", role: "alert", style: { color: "var(--bad)", fontSize: "13px" } });
  const submit = h("button", { class: "btn primary", type: "submit" }, needsSetup ? "Create admin account" : "Sign in");

  if (needsSetup && !setupAllowed) {
    // The panel only accepts the first admin account from this machine or the local network.
    error.textContent = "Create the admin account from the computer running Hearth (http://localhost:8000) " +
      "or from your local network. To allow it from anywhere, set HEARTH_ALLOW_REMOTE_SETUP=true and restart.";
    submit.disabled = true;
  }
  const form = h("form", { novalidate: false },
    field("Username", username),
    field("Password", password, { hint: needsSetup ? "At least 10 characters." : null }),
    codeField,
    error,
    submit,
  );
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    error.textContent = "";
    const body = { username: username.value.trim(), password: password.value };
    if (!codeField.hidden) body.totp_code = code.value.replace(/\s/g, "");
    busy(submit, async () => {
      try {
        await post(needsSetup ? "/auth/setup" : "/auth/login", body);
        onDone();
      } catch (err) {
        if (err.body?.totp_required) {
          const first = !("totp_code" in body);  // password step passed; now ask for the code
          codeField.hidden = false;
          error.textContent = first ? "" : err.message;
          code.value = "";
          code.focus();
          return;
        }
        error.textContent = err.message;
        password.select();
      }
    });
  });

  root.append(
    h("div", { class: "auth view" },
      h("div", { class: "auth-card" },
        wordmark(),
        h("h1", {}, needsSetup ? "Set up Hearth" : "Sign in"),
        h("p", { class: "muted" }, needsSetup
          ? "Create the first account. It becomes the admin who adds nodes and servers."
          : "Game servers for you and your friends, on every machine you own."),
        form,
      ),
    ),
  );
  username.focus();
}
