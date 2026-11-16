// Form fields generated from a template's variable definitions.

import { h } from "../dom.js";

const HIDDEN = new Set(["TYPE", "VERSION"]); // edited through the source editor

export function variableFields(template, values, { groups = ["properties", "startup"], secretsSet = [], lockAdminOnly = false } = {}) {
  const vars = template.variables.filter((v) => groups.includes(v.group) && !(template.features.includes("minecraft_java") && HIDDEN.has(v.key)));
  const controls = new Map();

  const bools = [];
  const others = [];
  for (const v of vars) {
    const current = values?.[v.key] ?? v.default;
    const id = `var-${v.key}`;
    let control;
    if (v.type === "bool") {
      control = h("input", { type: "checkbox", id, checked: !!current });
      bools.push(h("label", { class: "check", for: id }, control,
        h("span", {}, v.label, v.description ? h("small", {}, v.description) : null)));
    } else {
      if (v.type === "select") {
        control = h("select", { id }, v.options.map((o) => h("option", { value: o, selected: o === current }, prettyOption(o))));
      } else if (v.type === "int") {
        control = h("input", { id, type: "number", inputmode: "numeric", value: current, min: v.min ?? undefined, max: v.max ?? undefined, step: 1 });
      } else if (v.type === "text") {
        control = h("textarea", { id, class: "mono", rows: 5, spellcheck: "false" }, current || "");
      } else {
        const isSecret = v.secret;
        control = h("input", {
          id, type: isSecret ? "password" : "text", value: isSecret ? "" : current ?? "",
          autocomplete: isSecret ? "new-password" : "off", spellcheck: "false",
          placeholder: isSecret && secretsSet.includes(v.key) ? "Unchanged — type to replace" : v.key === "SEED" ? "Random" : "",
          maxlength: v.max ?? undefined, class: /IMAGE|PATH/.test(v.key) ? "mono" : undefined,
        });
      }
      others.push(h("div", { class: "field" },
        h("label", { for: id }, v.label),
        control,
        v.description ? h("span", { class: "hint" }, v.description) : null));
    }
    if (lockAdminOnly && v.admin_only) control.disabled = true;
    controls.set(v.key, { v, control });
  }

  const el = h("div", { class: "section-body" },
    others.length ? h("div", { class: "grid-2" }, others) : null,
    bools.length ? h("div", { class: "grid-2" }, bools) : null);

  return {
    el,
    empty: vars.length === 0,
    values() {
      const out = {};
      for (const [key, { v, control }] of controls) {
        if (v.type === "bool") out[key] = control.checked;
        else if (v.type === "int") out[key] = control.value === "" ? v.default : Number(control.value);
        else out[key] = control.value;
      }
      return out;
    },
  };
}

function prettyOption(o) {
  return o.replace(/^minecraft:/, "").replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}
