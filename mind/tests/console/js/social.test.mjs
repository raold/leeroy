import test from "node:test";
import assert from "node:assert/strict";
import { stageText, romanceText, feelings, feudText, memberStageText, jealousyShare, breakupText } from "../../../leeroy_mind/console/static/js/socialmodel.mjs";

test("a stage reads as words and none is empty", () => {
  assert.equal(stageText("whispers"), "whispers");
  assert.equal(stageText("none"), "");
  assert.equal(stageText(undefined), "");
});
test("a pair's two directions read as one line", () => {
  assert.equal(romanceText({ ab: "whispers", ba: "banter" }, "Ana", "Bo"), "Ana: whispers · Bo: banter");
  assert.equal(romanceText({ ab: "none", ba: "ex" }, "Ana", "Bo"), "Ana: - · Bo: ex");
});
test("feelings toward you list only what is set", () => {
  assert.deepEqual(feelings({ together_since: null, hurt_until: 0, rush_until: 0 }, 100), []);
  const c = feelings({ together_since: 50, hurt_until: 200, hurt_deep: 1, rush_until: 150 }, 100);
  assert.deepEqual(c.map(x => x.kind), ["together", "hurt", "rush"]);
  assert.equal(c[1].text, "deeply hurt");
  assert.equal(feelings({ hurt_until: 200, hurt_deep: 0 }, 100)[0].text, "hurt");
});
test("an ended hurt or rush is not shown", () => {
  assert.deepEqual(feelings({ hurt_until: 90, rush_until: 100 }, 100), []);
});
test("a feud line falls back to the guid", () => {
  assert.equal(feudText({ a: 1, b: 2, cause: "loot" }, { 1: "Ana", 2: "Bo" }), "Ana vs Bo: loot");
  assert.equal(feudText({ a: 1, b: 3, cause: "" }, { 1: "Ana" }), "Ana vs #3");
});
test("a member romance reads in the guild's words", () => {
  assert.equal(memberStageText("late_nights"), "dating");
  assert.equal(memberStageText("confession"), "together");
  assert.equal(memberStageText("ex"), "broke up");
  assert.equal(memberStageText("planning"), "planning");
  assert.equal(memberStageText("none"), "");
});
test("jealousy fills its meter by its 0-100 level", () => {
  assert.equal(jealousyShare(85), 0.85);
  assert.equal(jealousyShare(250), 1);
  assert.equal(jealousyShare(-3), 0);
  assert.equal(jealousyShare(undefined), 0);
});
test("a breakup says who left whom for whom", () => {
  assert.equal(breakupText({ woman: 2, partner: 3, for: 1 }, { 1: "Rex", 2: "Ana", 3: "Bo" }), "Ana left Bo for Rex");
  assert.equal(breakupText({ woman: 2, partner: 4, for: 1 }, { 2: "Ana" }), "Ana left #4 for #1");
});
