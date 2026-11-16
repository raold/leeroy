// His weight and figure edit in a member's Looks panel (personas.looks_edit through the mind's
// looks op): the form's values, the op's arguments and the ledger rows an Undo can take. Pure,
// so node --test checks it without a page.

export const BUST_WORDS = { small: "small", medium: "medium", large: "large", very_large: "very large" };
export const BUST_UNSET = "the build's own";
// Her other figure words beside the bust (owner, 2026-11-15), women only: name, label, the
// persona view's limits key, the words for "not set".
export const SHAPES = [
  { name: "glutes", label: "Glutes", key: "glutes", unset: "not said" },
  { name: "waist", label: "Waist", key: "waists", unset: "the build's own" },
  { name: "hips", label: "Hips", key: "hips", unset: "the build's own" },
];

function obj(x) { return x && typeof x === "object" && !Array.isArray(x) ? x : {}; }

// The form for a member: null without looks (nothing to edit yet). `limits` is the persona
// view's looks_limits: {weight_kg: [min, max], busts: [...]}.
export function looksForm(card, limits) {
  const c = obj(card), a = c.appearance ? obj(c.appearance) : null, l = obj(limits);
  if (!a) return null;
  const w = Array.isArray(l.weight_kg) && l.weight_kg.length === 2 ? l.weight_kg : [40, 180];
  const busts = Array.isArray(l.busts) && l.busts.length ? l.busts : Object.keys(BUST_WORDS);
  const shapes = {};
  SHAPES.forEach(sh => {
    shapes[sh.name] = { value: typeof a[sh.name] === "string" ? a[sh.name] : "",
                        options: Array.isArray(l[sh.key]) ? l[sh.key] : [] };
  });
  return { weight: a.weight_kg, bust: typeof a.bust === "string" ? a.bust : "", min: w[0], max: w[1],
           busts, bustable: obj(c.identity).player_gender === "female", shapes };
}

// The op's arguments, or an Error in words: weight a whole number of kg within the bounds, bust
// one of the form's or "" (not set: the build's own); `shapes` (glutes, waist, hips by name, as
// the selects hold them) the same way. A man's bust and shapes are never sent.
export function looksArgs(guid, form, weightText, bust, shapes) {
  const f = obj(form), text = String(weightText ?? "").trim();
  if (!/^[0-9]+$/.test(text)) throw new Error("Weight is a whole number of kg.");
  const kg = Number(text);
  if (kg < f.min || kg > f.max) throw new Error(`Weight is from ${f.min} to ${f.max} kg.`);
  let b = bust === "" || bust === null || bust === undefined ? null : String(bust);
  if (!f.bustable) b = null;
  if (b !== null && !(f.busts || []).includes(b)) throw new Error("Pick a bust from the list.");
  const out = { action: "set", guid: Number(guid), weight_kg: kg, bust: b };
  if (f.bustable && shapes && typeof shapes === "object") {
    SHAPES.forEach(sh => {
      if (!(sh.name in shapes)) return;
      const v = shapes[sh.name] === "" || shapes[sh.name] == null ? null : String(shapes[sh.name]);
      const options = obj(obj(f.shapes)[sh.name]).options || [];
      if (v !== null && !options.includes(v)) throw new Error(`Pick a ${sh.label.toLowerCase()} value from the list.`);
      out[sh.name] = v;
    });
  }
  return out;
}

export function looksUndoArgs(ledger) {
  return { action: "undo", ledger: Number(ledger) };
}

// A ledger row an Undo can take: a weight or bust change not undone yet.
export function canUndoLooks(row) {
  const r = obj(row);
  return r.kind === "looks" && !r.undone && Number.isInteger(r.id) && r.id > 0;
}

// The words for the op's answer: its message, or why it was refused.
export function looksReply(r) {
  const x = obj(r);
  if (x.ok === true) return String(x.message || "Saved.");
  return String(x.error || "The mind refused it.");
}

export function bustText(bust) {
  return typeof bust === "string" && BUST_WORDS[bust] ? BUST_WORDS[bust] : BUST_UNSET;
}

// A shape's value in words for the Looks list: "very large", "very narrow", or its unset words.
export function shapeText(name, value) {
  const sh = SHAPES.find(x => x.name === name);
  if (typeof value === "string" && value) return value.replace(/_/g, " ");
  return sh ? sh.unset : BUST_UNSET;
}
