import test from "node:test";
import assert from "node:assert/strict";
import { activeFor, nudgeArgs, timeLeft } from "../../../leeroy_mind/console/static/js/nudgemodel.mjs";

const fields = { text: "  Be |keen\n tonight ", scope: "game", strength: "light", hours: 24 };
test("nudge text uses Board cleaning and duration boundaries", () => {
  assert.deepEqual(nudgeArgs("member", 102, fields), { action: "nudge", verb: "set", target_kind: "member", target: "102", text: "Be keen tonight", scope: "game", strength: "light", seconds: 86400 });
  assert.equal(nudgeArgs("group", "everyone", { ...fields, hours: 336 }, "preview").seconds, 1209600);
  for (const hours of [0, 337, NaN]) assert.throws(() => nudgeArgs("member", 102, { ...fields, hours }));
  for (const text of ["|\x00", "x".repeat(201)]) assert.throws(() => nudgeArgs("member", 102, { ...fields, text }));
});
test("members see direct and inherited nudges, groups only their own", () => {
  const view = { members: [{ guid: 102, nudges: [1, 2] }], nudges: { active: [
    { id: 1, target_kind: "member", target: "102" }, { id: 2, target_kind: "group", target: "everyone" },
    { id: 3, target_kind: "member", target: "103" }] } };
  assert.deepEqual(activeFor(view, "member", 102).map(n => n.id), [1, 2]);
  assert.deepEqual(activeFor(view, "group", "everyone").map(n => n.id), [2]);
});
test("expiry words do not claim a stale nudge still applies", () => {
  assert.equal(timeLeft(100, 100), "Expired");
  assert.equal(timeLeft(160, 100), "1 min left");
  assert.equal(timeLeft(3700, 100), "1 h left");
});
