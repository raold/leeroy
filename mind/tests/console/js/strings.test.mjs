import test from "node:test";
import assert from "node:assert/strict";
import { cancelArgs, canCancel, codeText, GAME_EMOTES, GAME_KINDS, gameArgs, gameButtons, gameOutcome,
         stringState, stringText } from "../../../leeroy_mind/console/static/js/godmodemodel.mjs";

test("the in-game strings are buttons, all off and saying why unless the mind can take them", () => {
  const off = gameButtons(false, "The mind is not running.");
  assert.deepEqual(off.map(b => b.kind), ["come_to_me", "join_group", "follow", "duel_me", "sit", "emote"]);
  assert.ok(off.every(b => b.disabled && b.title === "The mind is not running."));
  const on = gameButtons(true, "ignored");
  assert.ok(on.every(b => !b.disabled && b.title === ""));
  assert.deepEqual(on.map(b => b.label), [...Object.values(GAME_KINDS).map(k => k.label), "Emote"]);
  assert.equal(gameButtons(false, "")[0].title, "Unavailable");
});

test("a string in game takes the string op's shape; an emote is one of the game's list", () => {
  assert.deepEqual(gameArgs("come_to_me", "102"), { action: "create", member: 102, kind: "come_to_me", args: {} });
  assert.deepEqual(gameArgs("emote", 103, "wave"), { action: "create", member: 103, kind: "emote", args: { emote: "wave" } });
  assert.deepEqual(gameArgs("sit", 104, "wave").args, {});
  assert.throws(() => gameArgs("emote", 103, "spit"), /emote/);
  assert.throws(() => gameArgs("queue_arena", 103));
  assert.equal(GAME_EMOTES.length, 16);
  assert.ok(!GAME_EMOTES.includes("spit") && GAME_EMOTES.includes("kiss"));
});

test("the answer says what she did, or why not, from codes", () => {
  assert.equal(gameOutcome({ ok: true, state: "taken", reason: "came", status: 200 }), "She's on her way to you.");
  assert.equal(gameOutcome({ ok: true, state: "taken", reason: "stopped" }), "She stopped following you and waits there.");
  assert.equal(gameOutcome({ ok: true, state: "not_taken", reason: "combat" }), "Not taken: Someone is in a fight.");
  assert.equal(gameOutcome({ ok: true, state: "not_taken", reason: "in_instance" }), "Not taken: Not in or into an instance.");
  assert.equal(gameOutcome({ ok: true, state: "not_taken", reason: "busy:raid_owner" }), "Not taken: Your raid holds her.");
  assert.equal(gameOutcome({ ok: true, state: "not_taken", reason: "busy:training_owner" }),
    "Not taken: Someone else is using her just now.");
  assert.equal(gameOutcome({ ok: true, state: "not_taken", reason: "unsupported" }),
    "Not taken: The game server needs the rebuild that adds strings.");
  assert.equal(gameOutcome({ ok: false, code: "off", error: "Godmode is off" }), "Godmode is off in mind.toml.");
  assert.equal(gameOutcome({ ok: false, error: "The mind's own words" }), "The mind's own words");
  assert.equal(codeText("too_far"), "She is too far from you.");
});

test("her strings list reads game strings in words, and only a waiting one can be cancelled", () => {
  assert.equal(stringText({ kind: "come_to_me", args: {} }), "Come to you");
  assert.equal(stringText({ kind: "emote", args: { emote: "bow" } }), "Emote at you: bow");
  assert.equal(stringState({ status: "taken", reason: "joined" }), "done: She joined your group.");
  assert.equal(stringState({ status: "not_taken", reason: "group_full" }), "not taken: Your party is full.");
  assert.equal(stringState({ status: "pending", due: 700 }, 100), "waiting (10 min left)");
  assert.equal(stringState({ status: "pending", due: 7300 }, 100), "waiting (2 h left)");
  assert.ok(canCancel({ status: "pending" }));
  assert.ok(!canCancel({ status: "taken" }) && !canCancel(null));
  assert.deepEqual(cancelArgs("7"), { action: "cancel", id: 7 });
});
