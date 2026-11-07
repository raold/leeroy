import test from "node:test";
import assert from "node:assert/strict";
import { tileState } from "../../../leeroy_mind/console/static/js/words.mjs";

test("a member out of the cast is dimmed and says so", () => {
  const s = tileState({ name: "Ana", in_cast: false, persona: { version: 1 }, class: 5, role: "healer" });
  assert.equal(s.off, true);
  assert.equal(s.label, "Ana (not in the cast)");
});
test("an offline cast member is not dimmed", () => {
  const s = tileState({ name: "Bo", in_cast: true, online: false, persona: { version: 1 } });
  assert.equal(s.off, false);
  assert.equal(s.label, "Bo");
});
test("no persona carries the chip", () => {
  assert.equal(tileState({ name: "Cy", in_cast: true }).chip, "no persona");
  assert.equal(tileState({ name: "Cy", in_cast: true, persona: {} }).chip, "");
});
test("missing class or role shows a dash or only the class", () => {
  assert.equal(tileState({ name: "Di", in_cast: true }).sub, "-");
  assert.equal(tileState({ name: "Di", in_cast: true, class: 5 }).sub, "Priest");
  assert.equal(tileState({ name: "Di", in_cast: true, class: 5, role: "healer" }).sub, "Priest · healer");
});
