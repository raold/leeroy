import test from "node:test";
import assert from "node:assert/strict";
import { BUST_UNSET, GROUPS, SETS, SHAPES, STYLE, bustText, canUndoLooks, looksArgs, looksForm,
         looksReply, looksUndoArgs, optionText, SAVE_WORDS, shapeText, styleRows } from "../../../leeroy_mind/console/static/js/looksmodel.mjs";

const LIMITS = { weight_kg: [40, 180], busts: ["small", "medium", "large", "very_large"],
                 glutes: ["small", "medium", "large", "very_large"],
                 waists: ["very_narrow", "narrow", "average", "wide"],
                 hips: ["narrow", "average", "wide", "very_wide"] };
const her = { identity: { player_gender: "female" },
              appearance: { weight_kg: 88, bust: "large", glutes: "large", hips: "very_wide" } };
const him = { identity: { player_gender: "male" }, appearance: { weight_kg: 95 } };
const shapesOf = (g, w, h) => ({ glutes: { value: g, options: LIMITS.glutes },
                                waist: { value: w, options: LIMITS.waists },
                                hips: { value: h, options: LIMITS.hips } });

// an older mind's view lists no hair, wardrobe or accessory values: nothing to pick, nothing sent
const NO_STYLE = Object.fromEntries(STYLE.map(st => [st.name, { value: "", options: [] }]));
const NO_SETS = { wardrobe: { values: [], options: [], max: 4 }, palette: { values: [], options: [], max: 3 } };
const FULL = { ...LIMITS,
               hair_colours: ["black", "dark_brown", "pastel_pink"], hair_lengths: ["buzzed", "long"],
               hair_styles: ["wavy", "bangs", "crew_cut", "fade", "quiff"],
               men_hair_styles: ["crew_cut", "fade", "quiff"],
               wardrobes: ["casual", "sporty", "edgy", "glam", "workwear", "swim"],
               palette: ["black", "red", "navy", "cobalt blue"],
               makeups: ["none", "natural", "defined", "glam"], glasses: ["never", "always"],
               jewellery: ["none", "minimal", "statement"], tattoos: ["none", "small", "sleeve"],
               piercings: ["none", "ears", "nose", "lip"] };

test("the form takes her values and the view's limits; no looks, no form", () => {
  assert.deepEqual(looksForm(her, LIMITS),
    { weight: 88, bust: "large", min: 40, max: 180, busts: LIMITS.busts, bustable: true,
      shapes: shapesOf("large", "", "very_wide"), style: NO_STYLE, sets: NO_SETS });
  assert.deepEqual(looksForm(him, LIMITS),
    { weight: 95, bust: "", min: 40, max: 180, busts: LIMITS.busts, bustable: false,
      shapes: shapesOf("", "", ""), style: NO_STYLE, sets: NO_SETS });
  assert.equal(looksForm({ identity: {}, appearance: null }, LIMITS), null);
  assert.equal(looksForm(null, LIMITS), null);
  assert.equal(looksForm(her, {}).max, 180);  // an older console view without limits
});

test("the op's arguments: a whole number of kg within the bounds and a listed bust or none", () => {
  const f = looksForm(her, LIMITS);
  assert.deepEqual(looksArgs(102, f, " 70 ", "very_large"),
    { action: "set", guid: 102, weight_kg: 70, bust: "very_large" });
  assert.deepEqual(looksArgs("102", f, "40", ""), { action: "set", guid: 102, weight_kg: 40, bust: null });
  assert.throws(() => looksArgs(102, f, "39", ""), /from 40 to 180/);
  assert.throws(() => looksArgs(102, f, "181", ""), /from 40 to 180/);
  assert.throws(() => looksArgs(102, f, "70.5", ""), /whole number/);
  assert.throws(() => looksArgs(102, f, "", ""), /whole number/);
  assert.throws(() => looksArgs(102, f, "-5", ""), /whole number/);
  assert.throws(() => looksArgs(102, f, "70", "huge"), /from the list/);
  // a man's bust is never sent, whatever the select held
  assert.equal(looksArgs(101, looksForm(him, LIMITS), "95", "large").bust, null);
});

test("undo goes only to his weight and bust rows not undone yet", () => {
  assert.deepEqual(looksUndoArgs("7"), { action: "undo", ledger: 7 });
  assert.equal(canUndoLooks({ id: 7, kind: "looks", undone: false }), true);
  assert.equal(canUndoLooks({ id: 7, kind: "looks", undone: true }), false);
  assert.equal(canUndoLooks({ id: 7, kind: "stance_band", undone: false }), false);
  assert.equal(canUndoLooks({ kind: "looks" }), false);
});

test("the answer's words are the mind's own, and the bust is said in words", () => {
  assert.equal(looksReply({ ok: true, message: "Saved (ledger #4). A new set renders tonight." }),
    "Saved (ledger #4). A new set renders tonight.");
  assert.equal(looksReply({ ok: false, error: "Bust is for women only." }), "Bust is for women only.");
  assert.equal(looksReply({}), "The mind refused it.");
  assert.equal(bustText("very_large"), "very large");
  assert.equal(bustText(undefined), BUST_UNSET);
  assert.equal(bustText("huge"), BUST_UNSET);
});

test("glutes, waist and hips go with her bust, as values or null, never for a man", () => {
  const f = looksForm(her, LIMITS);
  assert.deepEqual(SHAPES.map(sh => sh.name), ["glutes", "waist", "hips"]);
  assert.deepEqual(looksArgs(102, f, "88", "very_large", { glutes: "very_large", waist: "", hips: "wide" }),
    { action: "set", guid: 102, weight_kg: 88, bust: "very_large", glutes: "very_large", waist: null,
      hips: "wide" });
  assert.deepEqual(looksArgs(102, f, "88", "large"),  // an older page: the shapes stay as stored
    { action: "set", guid: 102, weight_kg: 88, bust: "large" });
  assert.throws(() => looksArgs(102, f, "88", "", { waist: "tiny" }), /waist value from the list/);
  assert.deepEqual(looksArgs(101, looksForm(him, LIMITS), "95", "", { glutes: "large", hips: "wide" }),
    { action: "set", guid: 101, weight_kg: 95, bust: null });
  assert.equal(shapeText("glutes", undefined), "not said");
  assert.equal(shapeText("waist", ""), "the build's own");
  assert.equal(shapeText("hips", "very_wide"), "very wide");
});

test("hair, makeup and accessories are selects in three groups; a man's styles and a woman's makeup stay theirs", () => {
  assert.deepEqual(GROUPS, ["Body", "Hair", "Style"]);
  assert.deepEqual(STYLE.map(st => st.name), ["owner_hair_colour", "owner_hair_length", "owner_hair_style",
    "makeup", "glasses", "jewellery", "tattoos", "piercings"]);
  assert.ok(STYLE.every(st => GROUPS.includes(st.group) && st.unset));
  const styled = { identity: { player_gender: "female" },
                   appearance: { weight_kg: 60, owner_hair_colour: "pastel_pink", makeup: "glam",
                                 wardrobe: ["edgy", "glam"], palette: ["black"] } };
  const f = looksForm(styled, FULL);
  assert.equal(f.style.owner_hair_colour.value, "pastel_pink");
  assert.deepEqual(f.style.owner_hair_style.options, ["wavy", "bangs"]);  // no crew cut for her
  assert.deepEqual(f.style.makeup.options, FULL.makeups);
  // swim is offered only to a woman who has the lust style now, or who has it set already
  const noSwim = FULL.wardrobes.filter(v => v !== "swim");
  assert.deepEqual(f.sets.wardrobe, { values: ["edgy", "glam"], options: noSwim, max: 4 });
  assert.deepEqual(looksForm({ ...styled, lust_style: true }, FULL).sets.wardrobe.options, FULL.wardrobes);
  const had = { ...styled, appearance: { ...styled.appearance, wardrobe: ["swim"] } };
  assert.deepEqual(looksForm(had, FULL).sets.wardrobe.options, FULL.wardrobes);
  assert.deepEqual(looksForm({ ...him, lust_style: false }, FULL).sets.wardrobe.options, noSwim);
  assert.deepEqual(f.sets.palette, { values: ["black"], options: FULL.palette, max: 3 });
  const m = looksForm(him, FULL);
  assert.deepEqual(m.style.owner_hair_style.options, FULL.hair_styles);
  assert.deepEqual(m.style.makeup.options, []);  // never offered, never sent
});

test("one save carries the hair, style and sets with the body; unset is null or an empty list", () => {
  const f = looksForm(her, FULL);
  assert.deepEqual(
    looksArgs(102, f, "88", "large", { glutes: "large", waist: "", hips: "very_wide" },
      { owner_hair_colour: "dark_brown", owner_hair_length: "", owner_hair_style: "bangs", makeup: "natural",
        glasses: "never", jewellery: "", tattoos: "sleeve", piercings: "" },
      { wardrobe: ["casual", "edgy", "casual"], palette: [] }),
    { action: "set", guid: 102, weight_kg: 88, bust: "large", glutes: "large", waist: null, hips: "very_wide",
      owner_hair_colour: "dark_brown", owner_hair_length: null, owner_hair_style: "bangs", makeup: "natural",
      glasses: "never", jewellery: null, tattoos: "sleeve", piercings: null,
      wardrobe: ["casual", "edgy"], palette: [] });
  assert.throws(() => looksArgs(102, f, "88", "", {}, { owner_hair_colour: "green" }), /hair colour from the list/);
  assert.throws(() => looksArgs(102, f, "88", "", {}, { owner_hair_style: "crew_cut" }), /hair style from the list/);
  assert.throws(() => looksArgs(102, f, "88", "", {}, {}, { wardrobe: ["casual", "tweed"] }), /wardrobe from the list/);
  assert.throws(() => looksArgs(102, f, "88", "", {}, {},
    { wardrobe: ["casual", "sporty", "edgy", "glam", "workwear"] }), /Wardrobe: up to 4/);
  assert.throws(() => looksArgs(102, f, "88", "", {}, {},
    { palette: ["black", "red", "navy", "cobalt blue"] }), /Palette: up to 3/);
  // a man's makeup is never sent; his hair and wardrobe are
  const m = looksArgs(101, looksForm(him, FULL), "95", "", {}, { makeup: "glam", owner_hair_style: "fade" },
    { wardrobe: ["sporty"] });
  assert.deepEqual(m, { action: "set", guid: 101, weight_kg: 95, bust: null, owner_hair_style: "fade",
                        wardrobe: ["sporty"] });
  // an older mind's view has no lists: nothing of them is sent, so its exact-shape check passes
  assert.deepEqual(looksArgs(102, looksForm(her, LIMITS), "88", "large", undefined,
    { owner_hair_colour: "black" }, { wardrobe: ["casual"] }),
    { action: "set", guid: 102, weight_kg: 88, bust: "large" });
});

test("the options and the list's rows are said in words", () => {
  assert.equal(optionText("dark_brown"), "dark brown");
  assert.equal(optionText("edgy"), "edgy / alt");
  assert.equal(optionText("glam"), "glam / going out");
  assert.equal(optionText("workwear"), "workwear (their job)");
  assert.equal(optionText("bangs"), "bangs / fringe");
  assert.equal(optionText("swim"), "swim / beach");
  assert.match(SAVE_WORDS, /one new persona version/);
  assert.match(SAVE_WORDS, /may not show in back views/);
  assert.deepEqual(styleRows({ weight_kg: 60 }), []);
  assert.deepEqual(styleRows({ owner_hair_colour: "pastel_pink", glasses: "always", wardrobe: ["edgy", "cosy"],
                               palette: ["cobalt blue"] }),
    [["hair colour (set)", "pastel pink"], ["glasses (set)", "always"], ["wardrobe", "edgy / alt, cosy"],
     ["palette", "cobalt blue"]]);
  assert.deepEqual(SETS.map(st => [st.name, st.max]), [["wardrobe", 4], ["palette", 3]]);
});
