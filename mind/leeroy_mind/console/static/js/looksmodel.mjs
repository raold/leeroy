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

// His hair, wardrobe, makeup and accessories (owner, 2026-11-17), for women and men: the
// selects (name is the op's argument and the looks' field; key the persona view's limits key;
// unset the words for "not set") and the two multi-selects, grouped as the panel shows them.
export const STYLE = [
  { name: "owner_hair_colour", label: "Hair colour", key: "hair_colours", group: "Hair", unset: "the looks' own" },
  { name: "owner_hair_length", label: "Hair length", key: "hair_lengths", group: "Hair", unset: "the looks' own" },
  { name: "owner_hair_style", label: "Hair style", key: "hair_styles", group: "Hair", unset: "the looks' own" },
  { name: "makeup", label: "Makeup", key: "makeups", group: "Style", unset: "not said", women: true },
  { name: "glasses", label: "Glasses", key: "glasses", group: "Style", unset: "as the looks say" },
  { name: "jewellery", label: "Jewellery", key: "jewellery", group: "Style", unset: "not said" },
  { name: "tattoos", label: "Tattoos", key: "tattoos", group: "Style", unset: "as the looks say" },
  { name: "piercings", label: "Piercings", key: "piercings", group: "Style", unset: "as the looks say" },
];
export const SETS = [
  { name: "wardrobe", label: "Wardrobe", key: "wardrobes", max: 4 },
  { name: "palette", label: "Palette", key: "palette", max: 3 },
];
export const GROUPS = ["Body", "Hair", "Style"];
export const LUST_ONLY = "swim";
// What a save does, and where what he set may not show (the photo prompt has 75 tokens).
export const SAVE_WORDS = "One save makes one new persona version (undo it in the ledger) and queues one new set of 4 photos, "
  + "for tonight or the photo booth; pick one of them as their photo to keep the new looks. "
  + "Glasses, tattoos, piercings, makeup and jewellery are said before every other optional word, in that order; "
  + "with a full figure and a long outfit the last of them may not fit, and they may not show in back views.";
const OPTION_WORDS = { edgy: "edgy / alt", glam: "glam / going out", workwear: "workwear (their job)",
                       swim: "swim / beach", bangs: "bangs / fringe" };

// An option's words: "dark brown", "edgy / alt", "bangs / fringe".
export function optionText(value) {
  const v = String(value);
  return OPTION_WORDS[v] || v.replace(/_/g, " ");
}

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
  const woman = obj(c.identity).player_gender === "female", man = obj(c.identity).player_gender === "male";
  const mens = Array.isArray(l.men_hair_styles) ? l.men_hair_styles : [];
  const style = {}, sets = {};
  STYLE.forEach(st => {
    let options = Array.isArray(l[st.key]) ? l[st.key] : [];
    if (st.women && !woman) options = [];  // makeup is a woman's
    if (st.name === "owner_hair_style" && !man) options = options.filter(v => !mens.includes(v));
    style[st.name] = { value: typeof a[st.name] === "string" ? a[st.name] : "", options };
  });
  SETS.forEach(st => {
    const values = Array.isArray(a[st.name]) ? a[st.name].map(String) : [];
    let options = Array.isArray(l[st.key]) ? l[st.key] : [];
    // swim is the lust style's: offered only to a woman who has it now (or who has it set)
    if (st.name === "wardrobe" && c.lust_style !== true) options = options.filter(v => v !== LUST_ONLY || values.includes(v));
    sets[st.name] = { values, options, max: st.max };
  });
  return { weight: a.weight_kg, bust: typeof a.bust === "string" ? a.bust : "", min: w[0], max: w[1],
           busts, bustable: woman, shapes, style, sets };
}

// The op's arguments, or an Error in words: weight a whole number of kg within the bounds, bust
// one of the form's or "" (not set: the build's own); `shapes` (glutes, waist, hips by name, as
// the selects hold them) the same way. A man's bust and shapes are never sent. `style` (STYLE
// by name, as the selects hold them) and `sets` (wardrobe and palette as lists) go only where the
// form has options for them (an older mind's view has none: they stay as stored).
export function looksArgs(guid, form, weightText, bust, shapes, style, sets) {
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
  if (style && typeof style === "object") {
    STYLE.forEach(st => {
      const options = obj(obj(f.style)[st.name]).options || [];
      if (!(st.name in style) || !options.length) return;
      const v = style[st.name] === "" || style[st.name] == null ? null : String(style[st.name]);
      if (v !== null && !options.includes(v)) throw new Error(`Pick a ${st.label.toLowerCase()} from the list.`);
      out[st.name] = v;
    });
  }
  if (sets && typeof sets === "object") {
    SETS.forEach(st => {
      const options = obj(obj(f.sets)[st.name]).options || [];
      if (!(st.name in sets) || !options.length) return;
      const picked = [...new Set((Array.isArray(sets[st.name]) ? sets[st.name] : []).map(String))];
      if (picked.some(v => !options.includes(v))) throw new Error(`Pick the ${st.label.toLowerCase()} from the list.`);
      if (picked.length > st.max) throw new Error(`${st.label}: up to ${st.max}.`);
      out[st.name] = picked;
    });
  }
  return out;
}

// The Looks list's rows for what he set: [[label, words]], only the values that are set.
export function styleRows(appearance) {
  const a = obj(appearance), rows = [];
  STYLE.forEach(st => {
    if (typeof a[st.name] === "string" && a[st.name]) rows.push([st.label.toLowerCase() + " (set)", optionText(a[st.name])]);
  });
  SETS.forEach(st => {
    if (Array.isArray(a[st.name]) && a[st.name].length) rows.push([st.label.toLowerCase(), a[st.name].map(optionText).join(", ")]);
  });
  return rows;
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
