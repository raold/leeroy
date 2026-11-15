import test from "node:test";
import assert from "node:assert/strict";
import { BUST_UNSET, bustText, canUndoLooks, looksArgs, looksForm, looksReply,
         looksUndoArgs } from "../../../leeroy_mind/console/static/js/looksmodel.mjs";

const LIMITS = { weight_kg: [40, 180], busts: ["small", "medium", "large", "very_large"] };
const her = { identity: { player_gender: "female" }, appearance: { weight_kg: 88, bust: "large" } };
const him = { identity: { player_gender: "male" }, appearance: { weight_kg: 95 } };

test("the form takes her values and the view's limits; no looks, no form", () => {
  assert.deepEqual(looksForm(her, LIMITS),
    { weight: 88, bust: "large", min: 40, max: 180, busts: LIMITS.busts, bustable: true });
  assert.deepEqual(looksForm(him, LIMITS),
    { weight: 95, bust: "", min: 40, max: 180, busts: LIMITS.busts, bustable: false });
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
