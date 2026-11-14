import test from "node:test";
import assert from "node:assert/strict";
import { codeText, editArgs, editsFor, editText, historyEdits, stringArgs, stringState,
         stringText } from "../../../leeroy_mind/console/static/js/godmodemodel.mjs";

test("edit arguments take the tune op's shape, mood in seconds and traits as their type", () => {
  assert.deepEqual(editArgs("mood", 102, { offset: "-0.5", hours: "2" }),
    { action: "edit", verb: "set", kind: "mood", args: { member: 102, offset: -0.5, seconds: 7200 } });
  assert.deepEqual(editArgs("trait", "101", { path: "likes", value: "fishing; tea " }, "preview").args,
    { member: 101, path: "likes", value: ["fishing", "tea"] });
  assert.equal(editArgs("trait", 101, { path: "big_five.openness.score", value: "7" }).args.value, 7);
  assert.deepEqual(editArgs("feeling", 102, { other: "1", affinity: "20", trust: "", romance_stage: "" }).args,
    { member: 102, other: 1, affinity: 20 });
  assert.deepEqual(editArgs("hangouts", 103, { channels: "irl:3; general:0", rooms: "" }).args,
    { member: 103, channels: { irl: 3, general: 0 } });
  assert.throws(() => editArgs("mood", 102, { offset: "3", hours: "1" }), /out of range/);
  assert.throws(() => editArgs("hangouts", 103, { channels: "irl:9" }), /0 to/);
  assert.throws(() => editArgs("memory_implant", 102, { text: "" }), /required/);
  assert.throws(() => editArgs("nope", 102, {}));
});

test("string arguments take the string op's shape", () => {
  assert.deepEqual(stringArgs("post", 102, { channel: " irl ", about: "the kitten" }),
    { action: "create", member: 102, kind: "post", args: { channel: "irl", about: "the kitten" } });
  assert.deepEqual(stringArgs("bring_up", 104, { topic: "the wipe", channel: "" }).args, { topic: "the wipe" });
  assert.deepEqual(stringArgs("react", 102, { message: "5", emoji: "🎉" }).args, { message: 5, emoji: "🎉" });
  assert.throws(() => stringArgs("thread", 102, { channel: "irl", name: "x".repeat(61), about: "a" }), /60/);
});

test("refusal and state words come from codes, never a bare code when known", () => {
  assert.equal(codeText("explicit"), "Keep it non-explicit.");
  assert.equal(codeText("unknown_code", "The mind's words"), "The mind's words");
  assert.equal(stringState({ status: "not_taken", reason: "feud" }), "not taken: She is feuding with them.");
  assert.equal(stringState({ status: "taken", reason: "sent" }), "done: Done.");
  assert.equal(stringState({ status: "pending", due: 7300 }, 100), "waiting (2 h left)");
  assert.equal(stringState({ status: "cancelled", reason: "owner" }), "cancelled: You cancelled it.");
});

test("a member sees edits about her and feuds she is in; history shows ids and states only", () => {
  const view = { edits: { active: [
    { id: 1, kind: "mood", member: 102, args: { member: 102, offset: 0.4 } },
    { id: 2, kind: "feud_start", member: 101, args: { member: 101, other: 102 } },
    { id: 3, kind: "trait", member: 103, args: { member: 103, path: "likes", value: ["tea"] } }] } };
  assert.deepEqual(editsFor(view, 102).map(e => e.id), [1, 2]);
  assert.equal(editText(view.edits.active[1], { 101: "Arkon", 102: "Belra" }), "Feud: Arkon and Belra");
  assert.equal(editText(view.edits.active[0]), "Mood +0.4");
  assert.deepEqual(historyEdits({ edits: [{ id: 3, kind: "memory_implant", from: "active", to: "none" }] }),
    ["edit #3 memory implant: active → none"]);
  assert.equal(stringText({ kind: "bring_up", args: { topic: "the wipe", channel: "strategy" } }),
    "Bring up the wipe in #strategy");
});
