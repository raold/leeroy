import { arr, el, obj } from "./core.mjs";
import { activeFor, nudgeArgs, SCOPES, timeLeft } from "./nudgemodel.mjs";

// Drafts stay only in this page's memory, across polling. Never localStorage or logs.
const drafts = new Map();
// A poll replaces the panel. Keep the editing position as well as its in-memory draft.
export function nudgeFocus(box) {
  const input = document.activeElement;
  const panel = input?.closest?.('.nudges');
  if (!panel || !box.contains(input) || !input.dataset.nudgeField) return () => {};
  const key = panel.dataset.nudgeKey, field = input.dataset.nudgeField;
  const start = input.selectionStart, end = input.selectionEnd, direction = input.selectionDirection;
  return () => {
    const next = Array.from(box.querySelectorAll('.nudges')).find(p => p.dataset.nudgeKey === key)
      ?.querySelector(`[data-nudge-field="${field}"]`);
    if (!next || next.disabled) return;
    next.focus({ preventScroll: true });
    if (typeof start === 'number' && typeof next.setSelectionRange === 'function')
      next.setSelectionRange(start, end, direction);
  };
}
export function nudgePanel(box, view, kind, target, live, why, ask) {
  const key = `${kind}:${target}`;
  const draft = drafts.get(key) || { text: "", strength: "light", scope: "everywhere", hours: 24 };
  const section = el("section", "nudges");
  section.dataset.nudgeKey = key;
  section.appendChild(el("h4", "", "Temporary nudges"));
  section.appendChild(el("p", "muted", "A temporary state of mind, in their own words. It expires automatically; safety and privacy rules still apply."));
  const status = el("p", "why"); status.setAttribute("role", "status");
  const controls = [];
  function button(parent, label, fn) {
    const b = el("button", "", label); b.type = "button"; b.disabled = !live;
    if (!live) b.title = why;
    b.addEventListener("click", fn); parent.appendChild(b); controls.push(b); return b;
  }
  async function act(args) {
    controls.forEach(b => { b.disabled = true; });
    status.textContent = args.verb === "preview" ? "Preparing preview…" : "Saving…";
    try {
      const reply = await ask(args, "Nudge");
      status.textContent = reply?.ok ? (args.verb === "preview" ? "Preview below." : "Saved.") : reply?.error || "The console could not be reached.";
      if (reply?.ok && args.verb === "set") { drafts.delete(key); text.value = ""; }
    } catch (_) {
      status.textContent = "The console could not be reached. Your draft is still here.";
    } finally { controls.forEach(b => { b.disabled = !live; }); }
  }
  const active = activeFor(view, kind, target);
  active.forEach(n => {
    const row = el("div", "nudge-row");
    row.appendChild(el("p", "", n.text));
    row.appendChild(el("p", "muted", `#${n.id} · ${n.strength} · ${SCOPES[n.scope]} · ${timeLeft(n.ends)}${n.target_kind === "group" ? " · group nudge" : ""}`));
    button(row, "Retire", () => act({ action: "nudge", verb: "retire", id: n.id }));
    section.appendChild(row);
  });
  if (!active.length) section.appendChild(el("p", "muted", "No active nudges."));
  function field(label, input, name) {
    const wrap = el("label", "nudge-field", label); wrap.appendChild(input); section.appendChild(wrap);
    input.dataset.nudgeField = name; input.disabled = !live; return input;
  }
  const text = field("Nudge (1–200 characters)", el("textarea"), "text");
  text.maxLength = 1024; text.rows = 3; text.value = draft.text;
  function select(label, values, chosen, name) {
    const s = el("select"); Object.entries(values).forEach(([value, label]) => { const o = el("option", "", label); o.value = value; s.appendChild(o); });
    s.value = chosen; return field(label, s, name);
  }
  const strength = select("Strength", { light: "Light", firm: "Firm" }, draft.strength, "strength");
  const scope = select("Where it applies", SCOPES, draft.scope, "scope");
  const hours = field("Duration in hours (1–336)", el("input"), "hours");
  hours.type = "number"; hours.min = 1; hours.max = 336; hours.step = 1; hours.value = draft.hours;
  function fields() { return { text: text.value, strength: strength.value, scope: scope.value, hours: Number(hours.value) }; }
  [text, strength, scope, hours].forEach(e => e.addEventListener("input", () => drafts.set(key, fields())));
  ["set", "preview"].forEach(verb => button(section, verb === "set" ? "Add nudge" : "Preview a line", () => {
    try { void act(nudgeArgs(kind, target, fields(), verb)); } catch (e) { status.textContent = e.message; }
  }));
  section.appendChild(status);
  const history = el("details"); history.appendChild(el("summary", "", "Nudge history"));
  arr(obj(view.nudges).history).forEach(c => {
    const moves = arr(c.moves).filter(m => { const n = m.after || m.before; return n?.target_kind === kind && n.target === String(target); });
    if (!moves.length) return;
    const row = el("div", "nudge-row");
    row.appendChild(el("p", "", `Change #${c.change} · ${c.label}`));
    moves.forEach(m => row.appendChild(el("p", "muted", (m.after || m.before).text)));
    button(row, "Undo", () => act({ action: "undo", change: c.change }));
    history.appendChild(row);
  });
  section.appendChild(history); box.appendChild(section);
}

export function nudgeBadges(view) {
  const counts = new Map(arr(view.members).map(m => [String(m.guid), arr(m.nudges).length]));
  document.querySelectorAll("#p-wall .tile[data-guid]").forEach(tile => {
    tile.querySelector(".nudge-badge")?.remove();
    const count = counts.get(tile.dataset.guid) || 0;
    if (count) tile.appendChild(el("span", "chip nudge-badge", `nudged (${count})`));
  });
}
