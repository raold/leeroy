// Godmode in a member's detail: her edits (the tune op's "edit" action) beside her dials, and her
// strings (the string op) in their own panel. Drafts stay in this page's memory only.
import { $, arr, clear, el, emit, obj, post, setText, str } from "./core.mjs";
import { codeText, editArgs, editsFor, editText, EDIT_KINDS, stringArgs, stringState, stringText,
         STRING_KINDS } from "./godmodemodel.mjs";

function form(parent, specs, live, why, draft) {
  const kind = el("select");
  kind.setAttribute("aria-label", "Kind");
  Object.entries(specs).forEach(([k, s]) => { const o = el("option", "", s.label); o.value = k; kind.appendChild(o); });
  kind.value = draft.kind && specs[draft.kind] ? draft.kind : Object.keys(specs)[0];
  const fields = el("div", "gm-fields"), inputs = {};
  function draw() {
    clear(fields);
    draft.kind = kind.value;
    specs[kind.value].fields.forEach(f => {
      let input;
      if (f.type === "select") {
        input = el("select");
        if (f.optional) input.appendChild(el("option", "", "-"));
        f.options.forEach(v => { const o = el("option", "", v.replace(/_/g, " ")); o.value = v; input.appendChild(o); });
        if (f.optional) input.firstChild.value = "";
      } else {
        input = el("input");
        input.type = f.type === "number" || f.type === "member" ? "number" : "text";
        if (f.step !== undefined) input.step = f.step;
      }
      const key = kind.value + ":" + f.name;
      if (draft[key] !== undefined) input.value = draft[key];
      input.addEventListener("input", () => { draft[key] = input.value; });
      input.disabled = !live;
      inputs[f.name] = input;
      const wrap = el("label", "godmode-field", f.label);
      wrap.appendChild(input);
      fields.appendChild(wrap);
    });
  }
  kind.addEventListener("change", draw);
  kind.disabled = !live;
  if (!live) kind.title = why;
  parent.appendChild(kind);
  parent.appendChild(fields);
  draw();
  return { kind, values: () => Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.value])) };
}

const editDrafts = new Map();
export function godmodePanel(box, view, guid, live, why, ask, enabled, names) {
  // Its own region beside the nudges panel: never a second ".nudges" (the nudges stay
  // addressable on their own, by the page and by its browser tests)
  const section = el("section", "godmode-edits");
  section.setAttribute("role", "region");
  section.setAttribute("aria-label", "Godmode edits");
  section.appendChild(el("h4", "", "Godmode edits"));
  const active = editsFor(view, guid);
  active.forEach(e => {
    const row = el("div", "godmode-row");
    row.appendChild(el("p", "", editText(e, names)));
    row.appendChild(el("p", "muted", `#${e.id} · ${str(e.kind).replace(/_/g, " ")}` + (e.ends ? " · until " + new Date(e.ends * 1000).toLocaleString() : "")));
    const b = el("button", "", "Retire");
    b.type = "button"; b.disabled = !live; if (!live) b.title = why;
    b.addEventListener("click", () => ask({ action: "edit", verb: "retire", id: e.id }, "Edit #" + e.id));
    row.appendChild(b);
    section.appendChild(row);
  });
  if (!active.length) section.appendChild(el("p", "muted", "No edits in force."));
  if (!enabled) {
    section.appendChild(el("p", "why", "Godmode is off: [godmode] enabled = true in mind.toml, then restart the mind and console."));
    box.appendChild(section);
    return;
  }
  const draft = editDrafts.get(guid) || {};
  editDrafts.set(guid, draft);
  const f = form(section, EDIT_KINDS, live, why, draft);
  const status = el("p", "why"); status.setAttribute("role", "status");
  const acts = el("div", "acts");
  [["Check", "preview"], ["Make it so", "set"]].forEach(([label, verb]) => {
    const b = el("button", "", label);
    b.type = "button"; b.disabled = !live; if (!live) b.title = why;
    b.addEventListener("click", () => {
      let args;
      try { args = editArgs(f.kind.value, guid, f.values(), verb); }
      catch (e) { status.textContent = e.message; return; }
      status.textContent = verb === "set" ? "Saving…" : "Checking…";
      ask(args, EDIT_KINDS[f.kind.value].label).then(r => {
        r = obj(r);
        status.textContent = r.ok ? (verb === "set" ? str(r.message) || "Saved." : "It fits: " + str(r.touches))
                                  : codeText(r.code, r.error);
        if (r.ok && verb === "set") editDrafts.delete(guid);
      });
    });
    acts.appendChild(b);
  });
  section.appendChild(acts);
  section.appendChild(status);
  box.appendChild(section);
}

const stringDrafts = new Map();
export function renderStrings(j, guid, live, why) {
  const panel = $("p-strings"), box = $("p-strings-box");
  if (!panel || !box) return;
  panel.hidden = j.strings === undefined;
  if (panel.hidden) return;
  clear(box);
  const rows = arr(j.strings);
  setText("p-strings-n", rows.filter(i => i.status === "pending").length ? rows.filter(i => i.status === "pending").length + " waiting" : "");
  if (j.strings === null) { box.appendChild(el("p", "muted", "Unavailable in this mind.db (run db migrate).")); return; }
  rows.forEach(i => {
    const row = el("div", "godmode-row");
    row.appendChild(el("p", "", stringText(i)));
    row.appendChild(el("p", "muted", `#${i.id} · ${stringState(i)}`));
    if (i.status === "pending") {
      const b = el("button", "", "Cancel");
      b.type = "button"; b.disabled = !live; if (!live) b.title = why;
      b.addEventListener("click", () => send({ action: "cancel", id: i.id }, "String #" + i.id));
      row.appendChild(b);
    }
    box.appendChild(row);
  });
  if (!rows.length) box.appendChild(el("p", "muted", "No strings yet."));
  const draft = stringDrafts.get(guid) || {};
  stringDrafts.set(guid, draft);
  const f = form(box, STRING_KINDS, live, why, draft);
  const status = el("p", "why"); status.setAttribute("role", "status");
  const b = el("button", "", "Ask her");
  b.type = "button"; b.disabled = !live; if (!live) b.title = why;
  b.addEventListener("click", () => {
    let args;
    try { args = stringArgs(f.kind.value, guid, f.values()); }
    catch (e) { status.textContent = e.message; return; }
    status.textContent = "Saving…";
    send(args, STRING_KINDS[f.kind.value].label).then(r => {
      status.textContent = r.ok ? str(r.message) || "Saved." : codeText(r.code, r.error);
      if (r.ok) stringDrafts.delete(guid);
    });
  });
  box.appendChild(b);
  box.appendChild(status);
}

function send(args, what) {
  return post("/api/string", args).then(r => {
    if (r.status === 403 && !r.error) emit("key-lost");
    else if (r.ok !== true) emit("note", what + " refused: " + codeText(r.code, r.error));
    else emit("note", what + ": " + str(r.message));
    return r;
  });
}
