import { arr, num, obj, str } from "./core.mjs";

// How the Board tab words what /api/board holds. No DOM here: node imports this file.

export function postCount(forum) {
  return arr(forum).reduce(function (n, t) { return n + arr(obj(t).posts).length; }, 0);
}

// The empty-state test: nothing posted, written or delivered yet
export function boardEmpty(v) {
  v = obj(v);
  return !arr(v.forum).length && !arr(v.letters).length && !arr(v.writeups).length;
}

export function qualityClass(q) {
  return "q" + Math.max(0, Math.min(5, typeof q === "number" ? Math.floor(q) : 1));
}

export function itemText(it) {
  it = obj(it);
  var n = num(it.count);
  return str(it.name) + (n > 1 ? " x" + n : "");
}

export function minutes(w) {
  w = obj(w);
  return Math.max(0, Math.round((num(w.end) - num(w.start)) / 60));
}

export var REALM_OFF = "The Realm is off in mind.toml, so nothing new is posted.";
export var NOTHING = "the Realm has written no forum posts, letters or write-ups.";

// -- his replies: the text rule (the mind's board.clean_reply, the fixture board_reply_text.jsonl)
// and whether the box can save what it holds. The mind checks again and has the last word.

export var POST_MAX = 200;
export var LETTER_MAX = 400;
export var RAW_MAX = 1024;  // what the box may hold before it is cleaned
export var REPLY_WORDS = {
  control_off: "Replies are off: this console runs with --no-control.",
  realm_off: "The Realm is off, so nobody would answer.",
  waiting: "You already replied. The answer comes tonight.",
  sending: "Saving…",
  empty: "There's nothing to send."
};
export var REPLIES_OLD = "This mind.db predates Board replies: stop the mind and run db migrate.";

// The whitespace Python's str.isspace() knows that is not a control character (JS's \s differs:
// it takes U+FEFF and leaves out none of these, so it is not used)
var SPACE = /^[ \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]$/;

// "|" and control characters dropped (tab, CR and LF become spaces), lone surrogates dropped,
// whitespace collapsed to single spaces, none at either end: protocol.clean_say_text
export function cleanReply(text) {
  var out = "";
  Array.from(str(text)).forEach(function (c) {
    var n = c.codePointAt(0);
    if (c === "|") { return; }
    if (c === "\t" || c === "\r" || c === "\n") { out += " "; return; }
    if (n < 0x20 || (n >= 0x7f && n <= 0x9f) || (n >= 0xd800 && n <= 0xdfff)) { return; }
    out += SPACE.test(c) ? " " : c;
  });
  return out.split(" ").filter(function (w) { return w; }).join(" ");
}

export function replyMax(kind) { return kind === "letter" ? LETTER_MAX : POST_MAX; }

function mindDown(m) {
  var state = typeof m === "string" ? m : str(obj(m).state);
  return !state || state === "unknown" ? "The mind's state is unclear, so nothing can be saved."
       : "The mind is " + state + ", so nothing can be saved.";
}

// {enabled, reason, count, max} for o = {control (the strip's control, or false), mind (its
// mind, or a state), realm (the view's realm_on: only false is off), kind, text, waiting (a reply
// to it waits already), sending}. In order: control off, the mind not running, the Realm off, a
// reply already waiting, one being saved, nothing to send, too long. count is code points of the
// cleaned text.
export function replyState(o) {
  o = obj(o);
  var max = replyMax(o.kind), cleaned = cleanReply(o.text), count = Array.from(cleaned).length;
  function no(reason) { return { enabled: false, reason: reason, count: count, max: max }; }
  var mind = typeof o.mind === "string" ? o.mind : str(obj(o.mind).state);
  if (o.control === false || obj(o.control).enabled === false) { return no(REPLY_WORDS.control_off); }
  if (mind !== "running") { return no(mindDown(o.mind)); }
  if (o.realm === false) { return no(REPLY_WORDS.realm_off); }
  if (o.waiting) { return no(REPLY_WORDS.waiting); }
  if (o.sending) { return no(REPLY_WORDS.sending); }
  if (!cleaned) { return no(REPLY_WORDS.empty); }
  if (count > max) { return no("Too long: " + count + " of " + max + " characters."); }
  return { enabled: true, reason: "", count: count, max: max };
}

export function replyLabel(kind, name) {
  return kind === "letter" ? "Write back to " + str(name) + " (only they read it)"
                           : "Reply on the forum (everyone, rivals too, can read it)";
}

export function hasWaiting(replies) {
  return arr(replies).some(function (r) { return obj(r).state === "waiting"; });
}

// What a reply of his shows beside its text: a chip, a note (bad: a refusal), and whether it can
// still be withdrawn. An answered one shows its answer under it (board.mjs).
export function replyShows(r) {
  r = obj(r);
  var out = { chip: "", note: "", bad: false, withdraw: false };
  if (r.state === "waiting") { out.chip = "waiting for tonight"; out.withdraw = true; }
  else if (r.state === "answered") { out.chip = "answered"; }
  else if (r.state === "withdrawn") { out.chip = "withdrawn"; }
  else if (r.state === "posted") { out.note = "Nobody answered"; }
  else if (r.state === "refused") {
    out.note = "Not posted: " + (str(r.reason).replace(/_/g, " ") || "no reason given");
    out.bad = true;
  }
  return out;
}

// The posts of one thread that answer a reply shown under another of its posts ({id: true}):
// each is drawn under that reply, not again in the thread. One with replies of its own stays.
export function answeredHere(posts) {
  var shown = {}, out = {};
  arr(posts).forEach(function (p) {
    arr(obj(p).replies).forEach(function (r) { shown[obj(r).id] = true; });
  });
  arr(posts).forEach(function (p) {
    p = obj(p);
    if (p.answers !== null && p.answers !== undefined && shown[p.answers] && !arr(p.replies).length) { out[p.id] = true; }
  });
  return out;
}

// A copy of the view with a reply he just saved under its post or letter, waiting, so it shows
// before the next poll; the view it was given is left as it was
export function withReply(v, kind, parent, reply) {
  v = obj(v);
  var placed = false;
  function add(item) {
    item = obj(item);
    if (placed || item.id !== parent) { return item; }
    placed = true;
    return Object.assign({}, item, { replies: arr(item.replies).concat([reply]) });
  }
  var out = Object.assign({}, v);
  if (kind === "letter") { out.letters = arr(v.letters).map(add); }
  else {
    out.forum = arr(v.forum).map(function (t) {
      return Object.assign({}, obj(t), { posts: arr(obj(t).posts).map(add) });
    });
  }
  out.waiting = num(v.waiting) + (placed ? 1 : 0);
  return out;
}

// Whether a waiting reply can be withdrawn now: only the console and the mind can stand in the way
// (the Realm off or not, he may take a reply back)
export function withdrawState(o) {
  o = obj(o);
  var mind = typeof o.mind === "string" ? o.mind : str(obj(o.mind).state);
  if (o.control === false || obj(o.control).enabled === false) { return { enabled: false, reason: REPLY_WORDS.control_off }; }
  if (mind !== "running") { return { enabled: false, reason: mindDown(o.mind) }; }
  return { enabled: true, reason: "" };
}

// Letters that answer a reply shown under another letter ({id: true}): each is drawn under that
// reply, not again in the list. One with replies of its own stays in the list.
export function answeredLetters(letters) {
  var shown = {}, out = {};
  arr(letters).forEach(function (m) {
    arr(obj(m).replies).forEach(function (r) {
      if (obj(r).state === "answered" && obj(r).answer_mail) { shown[r.answer_mail] = true; }
    });
  });
  arr(letters).forEach(function (m) {
    m = obj(m);
    if (shown[m.id] && !arr(m.replies).length) { out[m.id] = true; }
  });
  return out;
}
