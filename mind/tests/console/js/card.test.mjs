import test from "node:test";
import assert from "node:assert/strict";
import { bioText, birthdayText, cardText, moodText, whoText, youText } from "../../../leeroy_mind/console/static/js/words.mjs";

// The member card's words from /api/persona's member view. Invented members only.

function view(member, card, missing) {
  return { human: 1000, names: {}, missing: missing || [], recent_days: 7,
           member: Object.assign({ guid: 1101, name: "Arkon", "class": 5, role: "healer", stance: "teasing",
                                   mood: { octant: "relaxed", p: 0.2, a: -0.1, d: 0.1, updated: 0 },
                                   you: { affinity: 12, attraction: 40, romance_stage: "none", trust: 50,
                                          respect: 40, familiarity: 30 } }, member || {}),
           card: card === undefined ? { identity: { age: 28, player_gender: "female", region: "UK", home: null },
                                        status: "single" } : card };
}

test("the note is age, place, stance and status; the facts are role, mood and toward you", () => {
  var v = view(), c = cardText(v);
  assert.equal(c.name, "Arkon");
  assert.equal(c.note, "28 · UK · teasing · single");
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

test("a US member reads her state, or US before she has one; a couple names the partner", () => {
  var us = { identity: { age: 38, player_gender: "female", region: "US", home: "Texas" }, status: "married" };
  assert.equal(cardText(view({ stance: "lust" }, us)).note, "38 · Texas · lust · married");
  us = { identity: { age: 41, player_gender: "female", region: "US", home: null }, status: "divorced" };
  assert.equal(cardText(view({ stance: "fond" }, us)).note, "41 · US · fond · divorced");
  var uk = { identity: { age: 29, player_gender: "female", region: "UK" }, status: "dating Teodor" };
  assert.equal(cardText(view({ stance: "drawn" }, uk)).note, "29 · UK · drawn · dating Teodor");
});

test("each part is left out when empty, without a stray separator", () => {
  assert.equal(cardText(view({}, { identity: { age: null, region: "" } })).note, "teasing");
  assert.equal(cardText(view({ stance: null }, { identity: { age: 31, region: "" } })).note, "31");
  assert.equal(cardText(view({ stance: "" })).note, "28 · UK · single");
  assert.equal(cardText(view({ stance: "none" }, { identity: { age: 52, region: "UK" }, status: "married" })).note,
               "52 · UK · married");  // a stance of none is no attitude
  assert.equal(cardText(view({ stance: null }, { identity: {}, status: "with Teodor" })).note, "with Teodor");
  assert.equal(cardText(view({ stance: null }, { identity: {} })).note, "");
});

test("the Personas player line puts the gender after the age", () => {
  assert.equal(whoText({ age: 38, region: "US", home: "Ohio" }, "lust", "married", "female"),
               "38 · female · Ohio · lust · married");
  assert.equal(whoText({ age: 47, region: "UK" }, "none", null, "male"), "47 · male · UK");
  assert.equal(whoText(null, "", "", ""), "");
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
  assert.deepEqual(cardText(null, "Bexley"), { name: "Bexley", note: "Not in mind.db.", facts: [],
                                                bio: { about: "", rows: [] } });
  assert.equal(cardText(view({ name: "" }), "Bexley").name, "Bexley");
  assert.equal(cardText(view({ name: "" })).name, "#1101");
});

// The bio (owner, 2026-11-14): the persona card's life, likes, dislikes, values, habits and game, in words
function bioCard(over) {
  return Object.assign({
    identity: { age: 38, player_gender: "female", region: "US" },
    life: { job: "drainage inspector", household: "lives with her husband and two kids", rhythm: "6-to-2 shift.",
            birthday: "01-10", night_owl: true, life_thread: "treats the game like a novel" },
    likes: ["lore threads", "cheap IPA"], dislikes: ["lag"], values: ["directness", "reliability"],
    habits: [{ substance: "alcohol", when: "raid_nights" }, { substance: "tobacco", when: "after_midnight" }],
    game: { wants: ["finish the fanfic", "win the healing argument"], years_played: 8,
            outside_raids: ["auction_house", "collecting"], favourite_spot: "the Apothecarium" },
    play: { main_spec: "Shadow", off_spec: "Holy" } }, over || {});
}

test("the bio is the life thread, then job, home, days, birthday, habits, likes, dislikes, values and game", () => {
  var b = bioText(bioCard());
  assert.equal(b.about, "treats the game like a novel");
  assert.deepEqual(b.rows, [
    { k: "Job", v: "drainage inspector" },
    { k: "Home", v: "lives with her husband and two kids" },
    { k: "Days", v: "6-to-2 shift · night owl" },
    { k: "Birthday", v: "January 10" },
    { k: "Habits", v: "drinks on raid nights, smokes after midnight" },
    { k: "Likes", v: "lore threads, cheap IPA" },
    { k: "Dislikes", v: "lag" },
    { k: "Values", v: "directness, reliability" },
    { k: "Wants", v: "finish the fanfic; win the healing argument" },
    { k: "Plays", v: "8 years, Shadow (off: Holy)" },
    { k: "Off raids", v: "auction house, collecting" },
    { k: "Hangs out", v: "the Apothecarium" }]);
  assert.deepEqual(cardText(view({}, bioCard())).bio, b);
});

test("empty parts of the bio are left out, and no card means no bio", () => {
  var b = bioText(bioCard({ life: { job: "", household: "", rhythm: "", birthday: "", night_owl: false, life_thread: " " },
                           likes: [], dislikes: [], values: [], habits: [], game: {}, play: {} }));
  assert.deepEqual(b, { about: "", rows: [] });
  assert.deepEqual(bioText(null), { about: "", rows: [] });
  assert.deepEqual(bioText(bioCard({ habits: [{ substance: "glue", when: "daily" }] })).rows.find(function (r) {
    return r.k === "Habits"; }), undefined);
  assert.equal(bioText(bioCard({ life: { night_owl: true } })).rows[0].v, "night owl");
  assert.equal(bioText(bioCard({ play: { main_spec: "", off_spec: "Holy" } })).rows.find(function (r) {
    return r.k === "Plays"; }).v, "8 years");
});

test("a birthday reads as a month and day; anything else is kept as it is", () => {
  assert.equal(birthdayText("12-03"), "December 3");
  assert.equal(birthdayText("13-01"), "13-01");
  assert.equal(birthdayText(""), "");
  assert.equal(birthdayText(null), "");
});
