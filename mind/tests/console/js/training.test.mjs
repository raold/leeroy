import test from "node:test";
import assert from "node:assert/strict";
import { adaptText, gateText, changeText, deathsText, coverageText, evidenceNotes, trialText }
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
test("the gate passes only as the view says", () => {
  assert.deepEqual(gateText({ phase: "passed", verdict_ms: 0.7, passed: true }), { v: "Passed", d: "0.70 ms", ok: true });
  const g = gateText({ phase: "passed", verdict_ms: 1.2, passed: false });
  assert.equal(g.ok, false);
  assert.equal(g.v, "passed");
  assert.match(g.d, /1.20 ms/);
  assert.equal(gateText({ phase: "ab", verdict_ms: null, passed: false }).d.indexOf("no verdict"), 0);
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
