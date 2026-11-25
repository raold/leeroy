import test from "node:test";
import assert from "node:assert/strict";
import { summary, grouped, featureTitle, featureLines } from "../../../leeroy_mind/console/static/js/verifymodel.mjs";

const ledger = {
  counts: { failed: 1, seen_working: 1, unverified: 1, switched_off: 1 },
  coverage: { server: { files: 2, bytes: 2097152, from: 1795262400, complete: false, issues: { truncated_file: 1 } } },
  database: "ok", basis: "Counts are lower bounds.",
  features: [
    { id: "a", name: "Unseen", status: "unverified", prs: [1], since: 1795262400, note: "No direct telemetry.", switches: [], evidence: [
      { id: "x", kind: "working", what: "a thing", count: 0, codes: [], available: true }] },
    { id: "b", name: "Broken", status: "failed", prs: [2, 3], switches: [], evidence: [
      { id: "y", kind: "failed", what: "a reset", count: 2, first: 1795262400, last: 1795266000, codes: [{ code: "reset", count: 2 }], available: true },
      { id: "z", kind: "working", what: "a kill", count: 0, codes: [], available: false, why: "not_in_this_database" }] },
    { id: "c", name: "Off", status: "switched_off", prs: [], switches: [{ key: "Playerbots.YoggBrainSupport", state: "off", configured: "unset", default: false }], evidence: [] },
    { id: "d", name: "Works", status: "seen_working", prs: [4], switches: [], evidence: [] }]
};

test("failed first, then working, unverified and switched off", () => {
  assert.deepEqual(grouped(ledger).map(f => f.id), ["b", "d", "a", "c"]);
  assert.match(summary(ledger)[0], /4 shipped features · 1 failed · 1 seen working · 1 unverified · 1 switched off/);
  assert.match(summary(ledger).join(" "), /server · 2 files · 2\.0 MB · back to .* · partial · truncated_file 1/);
  assert.equal(featureTitle(ledger.features[1]), "Failed · Broken · #2 #3");
});
test("evidence lines say counts, codes and what was not seen or not readable", () => {
  const broken = featureLines(ledger.features[1]).join("\n");
  assert.match(broken, /Failed · a reset · 2 · .* to .* · reset 2/);
  assert.match(broken, /Working · a kill · unavailable: not_in_this_database/);
  assert.match(featureLines(ledger.features[0]).join("\n"), /Working · a thing · none seen[\s\S]*No direct telemetry\./);
  assert.match(featureLines(ledger.features[2]).join("\n"), /Switch Playerbots\.YoggBrainSupport · off \(configured unset, default off\)/);
});
test("nothing outside the explicit projection is shown", () => {
  const privateText = "PRIVATE_SENTINEL";
  const p = { ...ledger, raw: privateText, features: [{ ...ledger.features[1], msg: privateText,
    evidence: [{ ...ledger.features[1].evidence[0], line: privateText, payload: privateText }] }] };
  const shown = [...summary(p), ...grouped(p).flatMap(f => [featureTitle(f), ...featureLines(f)])].join(" ");
  assert.ok(!shown.includes(privateText));
  assert.deepEqual(grouped({}), []);
  assert.match(summary({}).join(" "), /0 shipped features/);
});
