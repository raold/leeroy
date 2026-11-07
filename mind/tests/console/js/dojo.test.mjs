import test from "node:test";
import assert from "node:assert/strict";
import { shortHead, stampText, realmKpi, buildKpi, gateKpi, episodesKpi, movedChip, gateChip, okText, buildFacts, episodeText }
  from "../../../leeroy_mind/console/static/js/dojomodel.mjs";

test("stamps and heads", () => {
  assert.equal(stampText("20261105_181500"), "2026-11-05 18:15");
  assert.equal(stampText("odd"), "odd");
  assert.equal(stampText(null), "-");
  assert.equal(shortHead("c0ffee1234567890"), "c0ffee12");
  assert.equal(shortHead(5), "");
});
test("the realm key figure", () => {
  assert.deepEqual(realmKpi({ state: "running", pid: 4242, port: 18085 }), { v: "Running", d: "pid 4242 on port 18085", cls: "ok" });
  assert.equal(realmKpi({ state: "stopped", port: 1 }).cls, "warn");
  assert.equal(realmKpi({ state: "unknown" }).v, "Unknown");
  assert.equal(realmKpi(null).v, "Unknown");
});
test("build, G1 and episodes figures", () => {
  assert.deepEqual(buildKpi({ variant: "training", ok: true }), { v: "training", d: "ok" });
  assert.equal(buildKpi({ variant: "training", ok: false }).d, "not clean");
  assert.equal(buildKpi(null).d, "no build receipt");
  assert.equal(buildKpi({ file: "x.json", why: "unreadable" }).d, "unreadable");
  assert.deepEqual(gateKpi([{ gate: "G0", state: "evidence" }, { gate: "G1", state: "passed", run: "20261105_181500" }]),
    { v: "Passed", d: "2026-11-05 18:15" });
  assert.equal(gateKpi([]).d, "no G1 run");
  const runs = [{ kind: "realm-acceptance" }, { kind: "g1", episodes: [{ usable: true }, { usable: false }, {}] }];
  assert.deepEqual(episodesKpi(runs), { v: "3", d: "1 usable" });
  assert.equal(episodesKpi([]).v, "-");
});
test("chips", () => {
  assert.deepEqual(movedChip("past"), { text: "moved past", cls: "warn" });
  assert.equal(movedChip("same").cls, "ok");
  assert.equal(movedChip("diverged").text, "diverged");
  assert.equal(movedChip("whatever").text, "unknown");
  assert.equal(gateChip("failed"), "bad");
  assert.equal(gateChip("not yet"), "");
  assert.equal(okText(true), "ok");
  assert.equal(okText(false), "failed");
  assert.equal(okText(null), "-");
});
test("build facts and episode words", () => {
  const f = buildFacts({ created_utc: "t", variant: "v", exit_code: 0, compiler_errors: 2, verified: { passed: 3, total: 4 }, core: "abcdef0123", playerbots: "" });
  assert.deepEqual(f.map(function (x) { return x[1]; }), ["t", "v", "0", "2", "3 of 4", "abcdef01", "-"]);
  assert.equal(episodeText({ policies: ["a", "b"], outcome: "death", winner: 0 }), "a vs b · death · team 0 won");
  assert.equal(episodeText({ policies: [], winner: null }), " · no outcome · no winner");
});
