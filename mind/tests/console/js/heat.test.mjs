import test from "node:test";
import assert from "node:assert/strict";
import { changeText, memberHeat, overlayText, photoText, rowText, sessionArgs, sessionText, worldText } from "../../../leeroy_mind/console/static/js/heatmodel.mjs";

const view = {
  enabled: true, lo: 0, hi: 10, world: 9, set: false, follows: 9,
  rows: Array.from({ length: 11 }, (_, h) => "row " + h),
  changes: Array.from({ length: 11 }, (_, h) => h === 9 ? "" : "9 to " + h + ": something."),
  sessions: { all: null, members: { 102: { mode: "clean", ends: "logout" } } },
  members: [
    { guid: 102, applies: true, title: "Heat 2 (world 9, hers +0)", lines: ["Wording: warm."], caps: ["clean for this session"], offset: 0, pinned: null, own_dial: null, session: "clean" },
    { guid: 104, applies: false, title: "stance none: heat does not apply", lines: [], caps: [], offset: 0, pinned: null, own_dial: null, session: null },
    { guid: 105, applies: true, title: "Heat 10 (world 9, hers +1)", lines: [], caps: [], offset: 1, pinned: null, own_dial: 10, session: null },
  ],
};

test("the world line says whether heat is set or the romance dial is still in force", () => {
  const unset = { ...view, world: null, set: false, follows: 10, suggest: 10, note: "Heat is on but not set: the romance dial (10) is in force." };
  assert.equal(worldText(unset), "Heat is on but not set: the romance dial (10) is in force.");
  assert.equal(worldText({ ...unset, note: "" }), "Heat is not set: the romance dial (10) is in force exactly as before.");
  assert.equal(worldText({ ...view, world: 6, set: true, follows: null }), "Heat 6, set here.");
  assert.equal(worldText({ world: null, set: false, follows: null }), "Heat is not set, and mind.toml could not be read.");
});
test("a value's words and what changes come from the server's rows, never rebuilt here", () => {
  assert.equal(rowText(view, 6), "row 6");
  assert.equal(changeText(view, 6), "9 to 6: something.");
  assert.equal(changeText(view, 9), "");
  for (const bad of [-1, 11, 2.5, NaN, "6"]) { assert.equal(rowText(view, bad), ""); assert.equal(changeText(view, bad), ""); }
});
test("a session reads as its mode and how it ends", () => {
  assert.equal(sessionText({ mode: "clean", ends: "logout" }), "clean until logout");
  assert.equal(sessionText({ mode: "open", ends: "morning" }), "no limits until the morning");
  assert.equal(sessionText(null), "");
});
test("the session action's arguments are exactly the tune op's", () => {
  assert.deepEqual(sessionArgs("clean", null), { action: "heat", session: "clean" });
  assert.deepEqual(sessionArgs("open", "102"), { action: "heat", session: "open", guid: 102 });
  assert.deepEqual(sessionArgs("clear"), { action: "heat", session: "clear" });
  assert.throws(() => sessionArgs("wild", null));
  assert.throws(() => sessionArgs("constructor", null));
});
test("a member's card carries her resolved lines, caps and tonight's override", () => {
  const m = memberHeat(view, 102);
  assert.equal(m.title, "Heat 2 (world 9, hers +0)");
  assert.deepEqual(m.caps, ["clean for this session"]);
  assert.equal(m.session, "clean until logout");
  assert.equal(memberHeat(view, 104).applies, false);
  assert.equal(memberHeat(view, 105).ownDial, 10);
  assert.equal(memberHeat(view, 999), null);
  const everyone = { ...view, sessions: { all: { mode: "open", ends: "morning" }, members: {} },
                     members: [{ ...view.members[0], session: "open" }] };
  assert.equal(memberHeat(everyone, 102).session, "no limits until the morning");
});
test("an overlay is listed by name, variants and size, or by what is read without it", () => {
  assert.equal(overlayText({ file: "heat/frank.txt", present: true, variants: 2, tokens: 180 }), "heat/frank.txt: 2 variants, about 180 tokens");
  assert.equal(overlayText({ file: "heat/voice.txt", present: true, variants: 1, tokens: 12 }), "heat/voice.txt: 1 variant, about 12 tokens");
  assert.equal(overlayText({ file: "heat/explicit.txt", present: false, reads: "romance_explicit.txt" }), "heat/explicit.txt: not there; reads romance_explicit.txt");
});
test("the photo side previews as lists of the vetted strings", () => {
  assert.equal(photoText({ outfits: ["satin midi dress"], settings: ["a cafe terrace"], views: ["front", "side"] }),
               "Outfits: satin midi dress. Places: a cafe terrace. Poses: front, side.");
  assert.equal(photoText(null), "");
});
