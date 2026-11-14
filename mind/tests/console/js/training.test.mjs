import test from "node:test";
import assert from "node:assert/strict";
import { adaptText, gateText, changeText, deathsText, coverageText, evidenceNotes, trialText, scopeText, playbookGroups, playbookTotals }
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
test("the raid playbooks read as raids, bosses and mechanics", () => {
  const v = { totals: { raids: 1, bosses: 1, mechanics: 2, playerbots: 1, checked: 1 },
    raids: [{ name: "Naxxramas", difficulties: ["10", "25"], bosses: [{ name: "Gluth", call: null, mechanics: [
      { name: "Mortal Wound", kind: "tank_swap_at_stacks", roles: ["tank"], only: [], handled_by: "playerbots", explain: "Swap." },
      { name: "Enrage", kind: "dispel", roles: ["ranged"], only: ["25"], handled_by: "none", explain: "Soothe." }] }] }] };
  const g = playbookGroups(v);
  assert.equal(g[0].title, "Naxxramas (10, 25)");
  assert.equal(g[0].bosses[0].detail, "2 mechanics, 1 handled by playerbots");
  assert.deepEqual(g[0].bosses[0].mechanics.map(m => [m.tag, m.who, m.stock]),
    [["tank swap", "tank", true], ["dispel", "ranged · only 25", false]]);
  assert.equal(playbookTotals(v), "1 raid · 1 boss · 2 mechanics · 1 handled by playerbots · 1 scored by the training ground");
  assert.deepEqual(playbookGroups(null), []);
});
