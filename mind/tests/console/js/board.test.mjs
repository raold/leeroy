import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { postCount, boardEmpty, qualityClass, itemText, minutes, POST_MAX, LETTER_MAX, RAW_MAX, cleanReply,
         replyState, replyLabel, replyShows, hasWaiting, answeredHere, withReply, withdrawState,
         answeredLetters } from "../../../leeroy_mind/console/static/js/boardmodel.mjs";

test("posts are counted over every thread", () => {
  assert.equal(postCount([{ posts: [1, 2] }, { posts: [3] }, {}]), 3);
  assert.equal(postCount(undefined), 0);
});
test("the board is empty only with nothing in all three lists", () => {
  assert.equal(boardEmpty({ forum: [], letters: [], writeups: [] }), true);
  assert.equal(boardEmpty({ forum: [], letters: [{}], writeups: [] }), false);
  assert.equal(boardEmpty(null), true);
});
test("quality classes are clamped and default to common", () => {
  assert.equal(qualityClass(4), "q4");
  assert.equal(qualityClass(9), "q5");
  assert.equal(qualityClass(-1), "q0");
  assert.equal(qualityClass("x"), "q1");
});
test("an item chip shows its count only above one", () => {
  assert.equal(itemText({ name: "Blade", count: 2 }), "Blade x2");
  assert.equal(itemText({ name: "Blade", count: 1 }), "Blade");
});
test("minutes of an event", () => {
  assert.equal(minutes({ start: 0, end: 1800 }), 30);
  assert.equal(minutes({ start: 100, end: 50 }), 0);
});

// -- his replies: the text rule (the fixture the mind's tests read too) and the box's state ------

const FIXTURE = readFileSync(new URL("../../fixtures/board_reply_text.jsonl", import.meta.url), "utf8")
  .split("\n").filter((t) => t.trim()).map((t) => JSON.parse(t));
const OPEN = { control: { enabled: true }, mind: { state: "running" }, realm: true, waiting: false, sending: false };
function st(o) { return replyState(Object.assign({}, OPEN, o)); }

test("every fixture row cleans as the mind cleans it and is offered as the mind takes it", () => {
  assert.ok(FIXTURE.length >= 12);
  for (const row of FIXTURE) {
    assert.equal(cleanReply(row.text), row.clean, row.name);
    for (const kind of ["post", "letter"]) {
      const s = st({ kind: kind, text: row.text });
      assert.equal(s.enabled, row[kind + "_ok"], row.name + " " + kind);
      assert.equal(s.count, Array.from(row.clean).length, row.name);
      assert.equal(s.max, kind === "post" ? POST_MAX : LETTER_MAX);
    }
  }
  assert.equal(POST_MAX, 200);
  assert.equal(LETTER_MAX, 400);
  assert.equal(RAW_MAX, 1024);
});
test("a reply's length is code points of the cleaned text", () => {
  assert.equal(st({ kind: "post", text: "gg \u{1F389}" }).count, 4);
  assert.deepEqual(st({ kind: "post", text: "a".repeat(201) }),
                   { enabled: false, reason: "Too long: 201 of 200 characters.", count: 201, max: 200 });
  assert.equal(st({ kind: "letter", text: "a".repeat(401) }).reason, "Too long: 401 of 400 characters.");
  assert.deepEqual(st({ kind: "letter", text: "  see you  " }), { enabled: true, reason: "", count: 7, max: 400 });
  assert.equal(st({ kind: "post", text: " | " }).reason, "There's nothing to send.");
  assert.equal(cleanReply(undefined), "");
  assert.equal(cleanReply("a\ud800b"), "ab");  // a lone surrogate, as the mind drops it
});
test("control off wins over everything", () => {
  const s = st({ control: { enabled: false }, mind: { state: "stopped" }, realm: false, waiting: true,
                 sending: true, kind: "post", text: "" });
  assert.deepEqual(s, { enabled: false, reason: "Replies are off: this console runs with --no-control.",
                        count: 0, max: 200 });
  assert.equal(st({ control: false, kind: "post", text: "hi" }).enabled, false);
});
test("then the mind, the Realm, a waiting reply and one already saving, in that order", () => {
  assert.equal(st({ mind: { state: "stopped" }, realm: false, kind: "post", text: "hi" }).reason,
               "The mind is stopped, so nothing can be saved.");
  assert.equal(st({ mind: "stopped", kind: "post", text: "hi" }).reason, "The mind is stopped, so nothing can be saved.");
  for (const m of [{ state: "unknown" }, {}, null]) {
    assert.equal(st({ mind: m, kind: "post", text: "hi" }).reason, "The mind's state is unclear, so nothing can be saved.");
  }
  assert.equal(st({ realm: false, waiting: true, kind: "post", text: "hi" }).reason,
               "The Realm is off, so nobody would answer.");
  assert.equal(st({ realm: null, kind: "post", text: "hi" }).enabled, true);  // unreadable: the mind says
  assert.equal(st({ waiting: true, sending: true, kind: "letter", text: "hi" }).reason,
               "You already replied. The answer comes tonight.");
  const saving = st({ sending: true, kind: "post", text: "" });
  assert.equal(saving.enabled, false);
  assert.equal(saving.reason, "Saving…");
});
test("the box's words and what each reply's state shows", () => {
  assert.equal(replyLabel("post", "Inventa"), "Reply on the forum (everyone, rivals too, can read it)");
  assert.equal(replyLabel("letter", "Inventa"), "Write back to Inventa (only they read it)");
  assert.deepEqual(replyShows({ state: "waiting" }), { chip: "waiting for tonight", note: "", bad: false, withdraw: true });
  assert.deepEqual(replyShows({ state: "refused", reason: "screen_name" }),
                   { chip: "", note: "Not posted: screen name", bad: true, withdraw: false });
  assert.deepEqual(replyShows({ state: "posted" }), { chip: "", note: "Nobody answered", bad: false, withdraw: false });
  assert.deepEqual(replyShows({ state: "withdrawn" }), { chip: "withdrawn", note: "", bad: false, withdraw: false });
  assert.deepEqual(replyShows({ state: "answered" }), { chip: "answered", note: "", bad: false, withdraw: false });
  assert.equal(hasWaiting([{ state: "posted" }, { state: "waiting" }]), true);
  assert.equal(hasWaiting([{ state: "withdrawn" }]), false);
  assert.equal(hasWaiting(undefined), false);
});
test("an answer shown under its reply is not shown again in the thread", () => {
  const posts = [{ id: 1, replies: [{ id: 9, state: "answered", answer_post: 2 }], answers: null },
                 { id: 2, replies: [], answers: 9 }, { id: 3, replies: [], answers: 44 },
                 { id: 4, replies: [{ id: 50, state: "waiting" }], answers: 9 }];  // replied to: listed
  assert.deepEqual(answeredHere(posts), { 2: true });
  assert.deepEqual(answeredHere([]), {});
});
test("a saved reply shows at once under its post or letter, waiting", () => {
  const v = { waiting: 0, forum: [{ thread: "a", posts: [{ id: 7, replies: [] }] }], letters: [{ id: 2, replies: [] }] };
  const r = { id: 12, ts: 5, text: "hi", state: "waiting", reason: null, answer_post: null, answer_mail: null };
  const post = withReply(v, "post", 7, r);
  assert.deepEqual(post.forum[0].posts[0].replies, [r]);
  assert.equal(post.waiting, 1);
  assert.deepEqual(v.forum[0].posts[0].replies, []);  // the view it was given is left as it was
  assert.deepEqual(withReply(v, "letter", 2, r).letters[0].replies, [r]);
  assert.equal(withReply(v, "letter", 99, r).waiting, 0);  // nowhere to put it: nothing counted
});
test("a waiting reply can be withdrawn unless the console or the mind is in the way", () => {
  assert.deepEqual(withdrawState({ control: { enabled: true }, mind: { state: "running" } }), { enabled: true, reason: "" });
  assert.equal(withdrawState({ control: { enabled: false }, mind: { state: "stopped" } }).reason,
               "Replies are off: this console runs with --no-control.");
  assert.equal(withdrawState({ control: { enabled: true }, mind: { state: "stopped" } }).reason,
               "The mind is stopped, so nothing can be saved.");
});
test("an answer letter shown under its reply is not listed again, unless he replied to it too", () => {
  const letters = [{ id: 1, replies: [{ id: 9, state: "answered", answer_mail: 5 }] },
                   { id: 5, replies: [] }, { id: 6, replies: [{ id: 10, state: "waiting" }] },
                   { id: 7, replies: [{ id: 11, state: "answered", answer_mail: 6 }] }];
  assert.deepEqual(answeredLetters(letters), { 5: true });
  assert.deepEqual(answeredLetters(undefined), {});
});
