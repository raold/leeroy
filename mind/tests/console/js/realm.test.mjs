import test from "node:test";
import assert from "node:assert/strict";
import { roleWord, raidersText, standing, poachText, tradeCount, firstKills } from "../../../leeroy_mind/console/static/js/realmmodel.mjs";

test("roles read as words", () => {
  assert.equal(roleWord("progression"), "progression rival");
  assert.equal(roleWord("gankers"), "gankers");
  assert.equal(roleWord("drama"), "drama farm");
});
test("raiders read by role with the item level", () => {
  assert.equal(raidersText({ tank: 1, healer: 2, melee: 0, ranged: 3, ilvl: 247 }),
    "1 tank, 2 healers, 0 melee, 3 ranged, item level 247");
  assert.equal(raidersText({ tank: 0, healer: 0, melee: 0, ranged: 0, ilvl: 0 }), "no raiders");
});
test("a standing is a date, a percentage with a bar, or nothing", () => {
  const d = s => "D" + s;
  assert.deepEqual(standing({ state: "killed", killed_at: 5 }, d), { text: "killed D5", pct: null });
  assert.deepEqual(standing({ state: "progressing", best_pct: 40 }, d), { text: "best 40%", pct: 40 });
  assert.deepEqual(standing(undefined, d), { text: "not started", pct: null });
});
test("the poaching offer, or none", () => {
  const n = g => "N" + g;
  assert.equal(poachText(null, n), "No offer this week.");
  assert.match(poachText({ officer: 1, member: 2, guild: 3 }, n), /^N1 has offered N2 a place in guild #3\.$/);
  assert.equal(poachText({ officer: 1, member: 2, guild: 3, officer_name: "Offa", guild_name: "Rival One" }, n),
    "Offa has offered N2 a place in Rival One.");
});
test("trade lines of the last seven days are counted", () => {
  assert.equal(tradeCount([{ ts: 100 }, { ts: 100 - 8 * 86400 }], 100), 1);
});
test("first kills count bosses a rival killed that the guild has not", () => {
  assert.equal(firstKills([{ rivals: { 1: { state: "killed" } } }, { rivals: { 1: { state: "progressing" } } }, {}]), 1);
  assert.equal(firstKills([{ guild_at: 1788235200, rivals: { 1: { state: "killed" } } },
                           { guild_at: null, rivals: { 2: { state: "killed" } } }]), 1);
});
