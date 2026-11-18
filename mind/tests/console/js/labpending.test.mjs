import test from "node:test";
import assert from "node:assert/strict";
import { armTitle, familyTitle, pendingView, ratedWords } from "../../../leeroy_mind/console/static/js/labmodel.mjs";
import { cardModel } from "../../../leeroy_mind/console/static/js/tonightmodel.mjs";

test("what waits for his decision is listed with its count", () => {
  const view = pendingView({ count: 3, lessons_total: 4,
    arms: [{ id: "tease.9.v1", family: "tease" }, { nope: 1 }, null],
    lessons: [{ id: 7, guid: 101, name: "Arkon", scope: "guild", text: "She keeps replies brief." }],
    variants: [{ id: "lab.rules.v7", family: "prompt.rules", created: 1800000000 }] });
  assert.equal(view.count, 3);
  assert.deepEqual(view.arms, [{ id: "tease.9.v1", words: "Arm tease.9.v1 (tease) · proposed" }]);
  assert.equal(view.lessons[0].words, "Lesson for Arkon · guild: She keeps replies brief.");
  assert.match(view.more, /^3 more proposed lessons/);
  assert.match(view.variants[0].words, /^Arm lab\.rules\.v7 \(prompt\.rules\) came from the conversation lab on /);
});

test("an older console's answer has nothing pending", () => {
  assert.deepEqual(pendingView(undefined), { arms: [], lessons: [], variants: [], count: 0, more: "" });
  assert.equal(pendingView({ count: -2 }).count, 0);
});

test("rated lines are said quietly, or not at all before the tables", () => {
  assert.equal(ratedWords(0), "You rated 0 lines this week.");
  assert.equal(ratedWords(1), "You rated 1 line this week.");
  assert.equal(ratedWords(12), "You rated 12 lines this week.");
  assert.equal(ratedWords(null), "");
  assert.equal(ratedWords(undefined), "");
});

test("folded panels say what is inside", () => {
  const arms = [{ id: "a", family: "length", status: "active" }, { id: "b", family: "length", status: "proposed" },
                { id: "c", family: "tease", status: "control" }];
  assert.equal(familyTitle("length", arms), "length (2 arms, 1 proposed)");
  assert.equal(familyTitle("tease", arms), "tease (1 arm)");
  assert.equal(familyTitle("none", arms), "none (0 arms)");
  assert.equal(armTitle(arms[0], [{ arm_id: "a" }, { arm_id: "a" }, { arm_id: "b" }]), "a · active (2 contexts)");
  assert.equal(armTitle(arms[2], []), "c · control (0 contexts)");
});

test("Tonight's waiting card has the Lab's line when the console sends it", () => {
  const texts = v => cardModel("waiting", v).lines.map(l => [l.text, l.href]);
  const base = { status: "ok", dms: 0, letters: 0, selfies: 0, polls: 0 };
  assert.ok(texts({ ...base, lab: 2 }).some(([t, h]) => t === "Lab proposals to decide: 2" && h === "#lab"));
  assert.ok(texts({ ...base, lab: null }).some(([t]) => t === "Lab proposals to decide: not set up yet"));
  assert.ok(!texts(base).some(([t]) => t.startsWith("Lab proposals")));
});
