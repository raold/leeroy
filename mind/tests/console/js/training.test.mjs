import test from "node:test";
import assert from "node:assert/strict";
import { adaptText, gateText, changeText, deathsText, coverageText, evidenceNotes, trialText, scopeText,
  spellText, hurtHead, deathsBy, hitHardest, mechanicsText, avoidText }
  from "../../../leeroy_mind/console/static/js/trainmodel.mjs";

test("days until tactics may adapt", () => {
  assert.deepEqual(adaptText({ days_left: 18, days_observed: 10 }), { v: "10 of 28 days", d: "18 days to go" });
  assert.equal(adaptText({ days_left: 1, days_observed: 27 }).d, "1 day to go");
  assert.equal(adaptText({ days_left: null }).v, "-");
  const done = adaptText({ days_left: 0, days_observed: 40, four_weeks: true });
  assert.equal(done.v, "28 days observed");
  assert.match(done.d, /met/);
  assert.match(adaptText({ days_left: 0, four_weeks: false }).d, /not met/);
});
test("the gate reads each phase in words and passes as the view says", () => {
  assert.deepEqual(gateText({ phase: "passed", verdict_ms: 0.7, passed: true }), { v: "Passed", d: "0.70 ms", ok: true });
  assert.deepEqual(gateText({ phase: "passed", verdict_ms: 1.2, passed: true }), { v: "Passed", d: "1.20 ms", ok: true });
  const f = gateText({ phase: "failed", verdict_ms: 1.2, passed: false });
  assert.equal(f.ok, false);
  assert.equal(f.v, "Failed");
  assert.equal(f.d, "1.20 ms, over the gate's limit");
  const ab = gateText({ phase: "ab", raids: 2, verdict_ms: null, passed: false });
  assert.equal(ab.v, "Measuring");
  assert.equal(ab.d, "no verdict yet (2 raids measured)");
  assert.equal(ab.ok, false);
  assert.doesNotMatch(JSON.stringify([f, ab]), /1 ms/);
  assert.equal(gateText({}).v, "-");
});
test("change, deaths and coverage read as words", () => {
  assert.equal(changeText({ change: "better", deaths_change: -1.5 }), "better (-1.50 deaths a pull)");
  assert.equal(changeText({}), "too few");
  assert.equal(deathsText({ deaths_per_pull: 2 }), "2.00");
  assert.equal(deathsText({}), "-");
  assert.equal(coverageText({ checks_coverage: ["a", "b"] }), "not covered: a, b");
  assert.equal(coverageText({ checks_coverage: [], checks_complete_pulls: 3, pulls: 3 }), "fully covered");
  assert.equal(coverageText({ checks_coverage: [], checks_complete_pulls: 1, pulls: 3 }), "partly covered");
});
test("evidence becomes notes and trials count arms", () => {
  const n = evidenceNotes({ schema: "available", scope: "S", tactic: "T", truncated: ["pulls"], voice: { audible_delivery: "unverified", fact_delivery: "unverified", cast_coverage: "unverified" } });
  assert.deepEqual(n.slice(0, 2), ["S", "T"]);
  assert.ok(n.some(x => x.startsWith("shortened")) && n.some(x => x.startsWith("voice:")));
  assert.deepEqual(evidenceNotes(null), []);
  assert.equal(trialText({ arms: 2, observations: 1 }), "2 arms, 1 observation");
});
test("a trial's or a card group's scope reads as its boss and mode", () => {
  assert.equal(scopeText({ scope: "631:0:1", boss: "Lord Marrowgar", mode: "25-man" }), "Lord Marrowgar · 25-man");
  assert.equal(scopeText({ scope: "odd", boss: "odd", mode: "" }), "odd");
  assert.equal(scopeText({ scope: "631:0:1" }), "631:0:1");
});

test("what hurt them reads as short lines of numbers and spell names", () => {
  const c = {
    pulls: 7, kills: 2, wipes: 5, by_kind: { offline: 1, his: 6 }, incomplete_pulls: 1,
    deaths: { per_pull: 3.2857, by_spell: [{ spell: 900, name: "Vile Gas", deaths: 9, members: 6 },
                                           { spell: 0, name: "melee or untagged", deaths: 1, members: 1 },
                                           { spell: 77, name: null, deaths: 1, members: 1 }] },
    damage_taken: { top: [{ spell: 901, name: "Gas Spore", share: 0.3125 }, { spell: 5, share: null }] },
    mechanics: [{ kind: "debuff", key: "Gas Spore", count: 12, worst: [{ guid: 101, name: "Testa", count: 4 }] },
                { kind: "spread", key: "spread", count: 2, worst: [] }],
    avoidable: [{ names: ["Vile Gas"], resolved: true, hits: 5, members: [{ guid: 101, name: "Testa", hits: 4, over_allowance: 2 },
                                                                         { guid: 102, hits: 1, over_allowance: 0 }] },
                { names: ["Malleable Goo", "Slime Puddle"], resolved: false, hits: 0, members: [] },
                { names: ["Coldflame"], resolved: true, hits: 0, members: [] }],
  };
  assert.equal(hurtHead(c), "7 pulls (2 kills, 5 wipes) · his 6, offline 1 · 3.29 deaths a pull · 1 incomplete pull not read");
  assert.equal(deathsBy(c), "Vile Gas ×9 (6 members), melee or untagged ×1 (1 member), spell 77 ×1 (1 member)");
  assert.equal(hitHardest(c), "Gas Spore 31%, spell 5");
  assert.equal(mechanicsText(c), "Gas Spore (debuff) ×12, most Testa ×4; spread (spread) ×2");
  assert.deepEqual(avoidText(c), ["Vile Gas: Testa 4 hits (2 over), #102 1 hit", "Malleable Goo / Slime Puddle: spell not named yet"]);
  assert.equal(spellText({ spell: 3 }), "spell 3");
  assert.equal(hurtHead({}), "0 pulls (0 kills, 0 wipes) · - deaths a pull");
  assert.equal(deathsBy({}), "");
  assert.deepEqual(avoidText({}), []);
});
