import test from "node:test";
import assert from "node:assert/strict";
import { cardText, moodText, youText } from "../../../leeroy_mind/console/static/js/words.mjs";

// The member card's words from /api/persona's member view. Invented members only.

function view(member, card, missing) {
  return { human: 1000, names: {}, missing: missing || [], recent_days: 7,
           member: Object.assign({ guid: 1101, name: "Arkon", "class": 5, role: "healer", stance: "teasing",
                                   mood: { octant: "relaxed", p: 0.2, a: -0.1, d: 0.1, updated: 0 },
                                   you: { affinity: 12, attraction: 40, romance_stage: "none", trust: 50,
                                          respect: 40, familiarity: 30 } }, member || {}),
           card: card === undefined ? { identity: { age: 28, player_gender: "f", region: "Leeds" } } : card };
}

test("the note is age and region, then the stance; the facts are role, mood and toward you", () => {
  var v = view(), c = cardText(v);
  assert.equal(c.name, "Arkon");
  assert.equal(c.note, "28, Leeds · teasing");
  assert.deepEqual(c.facts, [{ k: "Role", v: "Priest · healer" },
                             { k: "Mood", v: moodText(v.member.mood) },
                             { k: "Toward you", v: youText(v.member.you) }]);
  assert.equal(c.facts[1].v, "relaxed");
  assert.equal(c.facts[2].v, "affinity +12, attraction 40");
});

test("with no persona card the note says so", () => {
  var c = cardText(view({ stance: null, mood: null, you: null }, null));
  assert.equal(c.note, "No persona yet.");
  assert.deepEqual(c.facts, [{ k: "Role", v: "Priest · healer" }, { k: "Mood", v: "not recorded yet" },
                             { k: "Toward you", v: "no row yet" }]);
});

test("a stance alone, or age and region alone, without a stray separator", () => {
  assert.equal(cardText(view({}, { identity: { age: null, region: "" } })).note, "teasing");
  assert.equal(cardText(view({ stance: null }, { identity: { age: 31, region: "" } })).note, "31");
  assert.equal(cardText(view({ stance: "" })).note, "28, Leeds");
});

test("a class without a role, a role without a class and neither", () => {
  assert.equal(cardText(view({ role: null })).facts[0].v, "Priest");
  assert.equal(cardText(view({ "class": 0 })).facts[0].v, "healer");
  assert.equal(cardText(view({ "class": 0, role: null })).facts[0].v, "-");
});

test("a table an older mind.db lacks reads unavailable", () => {
  var c = cardText(view({}, undefined, ["pad_mood", "relationship"]));
  assert.equal(c.facts[1].v, "unavailable");
  assert.equal(c.facts[2].v, "unavailable");
});

test("a member mind.db does not know keeps the name the page knows", () => {
  assert.deepEqual(cardText(null, "Bexley"), { name: "Bexley", note: "Not in mind.db.", facts: [] });
  assert.equal(cardText(view({ name: "" }), "Bexley").name, "Bexley");
  assert.equal(cardText(view({ name: "" })).name, "#1101");
});
