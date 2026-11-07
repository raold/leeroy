import { $, arr, clear, el, obj, on, post, str } from "./core.mjs";
import { S } from "./state.mjs";
import { face } from "./faces.mjs";
import { notSetUp, pollTab, renderKpis } from "./tabkit.mjs";
import { NOTHING, RAW_MAX, REALM_OFF, REPLIES_OLD, REPLY_WORDS, answeredHere, answeredLetters, boardEmpty, cleanReply,
         hasWaiting, itemText, minutes, postCount, qualityClass, replyLabel, replyShows, replyState,
         withReply, withdrawState } from "./boardmodel.mjs";
import { when } from "./words.mjs";

// -- the board tab: mind.db read-only, and his replies through the mind's board op ---------------
//
// A Reply button under each post and letter opens one box at a time. While it is open the tab's
// poll keeps the newest view but does not redraw (the box and its text stay as they are); the
// view is drawn again when the box closes. A saved reply shows at once, waiting for tonight; a
// refusal keeps the text and says why under the box.

var BOARD_MS = 30000;
var NO_ANSWER = "The console did not answer. Look at the Board before sending it again.";
var view = null;  // the newest view from /api/board
var box = null;   // the open reply box: {kind, to, ta, count, send, cancel, why, sending, refused}

function nameOf(v, g) { return str(obj(v.names)[String(g)]) || "#" + g; }

function whoLine(guid, name) {
  var w = el("span", "who");
  w.appendChild(face(guid, name, "list"));
  w.appendChild(el("span", "", name));
  return w;
}

function findPost(v, id) {
  var found = null;
  arr(v.forum).forEach(function (t) {
    arr(obj(t).posts).forEach(function (p) { if (!found && obj(p).id === id) { found = p; } });
  });
  return found;
}
function findLetter(v, id) {
  return arr(v.letters).filter(function (m) { return obj(m).id === id; })[0] || null;
}
function repliesOf(v, kind, id) {
  var item = kind === "letter" ? findLetter(obj(v), id) : findPost(obj(v), id);
  return item ? arr(item.replies) : [];
}

// -- his replies under a post or letter ---------------------------------------------------------

function answerOf(v, r, kind) {
  var a = el("div", "answer");
  if (kind === "post") {
    var p = findPost(v, r.answer_post);
    if (!p) { a.appendChild(el("p", "note", "The answer is further back in the thread.")); return a; }
    a.appendChild(whoLine(p.author_kind === "member" ? p.author : 0, str(p.name)));
    if (p.guild) { a.appendChild(el("span", "chip", str(p.guild))); }
    a.appendChild(el("span", "muted", when(p.ts)));
    a.appendChild(el("p", "text", str(p.text)));
  } else {
    var m = findLetter(v, r.answer_mail);
    if (!m) { a.appendChild(el("p", "note", "The answer is on its way to your mailbox.")); return a; }
    a.appendChild(whoLine(m.sender, nameOf(v, m.sender)));
    a.appendChild(el("strong", "", str(m.subject)));
    a.appendChild(el("span", "muted", when(m.ts)));
    a.appendChild(el("p", "text", str(m.body)));
    arr(m.items).forEach(function (it) {
      a.appendChild(el("span", "chip item " + qualityClass(obj(it).quality), itemText(it)));
    });
  }
  return a;
}

function withdraw(r, btn, chip, why) {
  var w = withdrawState({ control: S.status.control, mind: S.status.mind });
  if (!w.enabled) { why.textContent = w.reason; why.hidden = false; return; }
  btn.disabled = true;
  why.hidden = true;
  post("/api/board", { action: "withdraw", id: r.id }).then(function (j) {
    if (j.ok !== true) {
      why.textContent = str(j.error) || "It was not withdrawn.";
      why.hidden = false;
      btn.disabled = false;
      return;
    }
    r.state = "withdrawn";
    if (box) {  // the board is held while a box is open: this reply changes in place
      btn.remove();
      chip.textContent = "withdrawn";
      chip.className = "chip";
      refreshBox();
    } else { draw(view); }
  }, function () {
    why.textContent = NO_ANSWER;
    why.hidden = false;
    btn.disabled = false;
  });
}

function replyRow(v, r, kind) {
  var row = el("div", "reply"), head = el("div", "rhead"), shows = replyShows(r);
  head.appendChild(el("span", "you", "You"));
  head.appendChild(el("span", "muted", when(r.ts)));
  var chip = el("span", "chip" + (r.state === "waiting" ? " warn" : r.state === "answered" ? " ok" : ""), shows.chip);
  chip.hidden = !shows.chip;
  head.appendChild(chip);
  var why = el("p", "why bad");
  why.hidden = true;
  if (shows.withdraw) {
    var b = el("button", "btn", "Withdraw");
    b.type = "button";
    b.addEventListener("click", function () { withdraw(r, b, chip, why); });
    head.appendChild(b);
  }
  row.appendChild(head);
  row.appendChild(el("p", "text", str(r.text)));
  if (shows.note) { row.appendChild(el("p", shows.bad ? "why bad" : "note", shows.note)); }
  if (r.state === "answered") { row.appendChild(answerOf(v, r, kind)); }
  row.appendChild(why);
  return row;
}

// The replies under one post or letter, and its Reply button with the place its box opens
function repliesBlock(v, kind, id, name) {
  var block = el("div", "replies");
  arr(repliesOf(v, kind, id)).forEach(function (r) { block.appendChild(replyRow(v, obj(r), kind)); });
  if (v.replies_on === true) {
    var b = el("button", "btn replybtn", "Reply");
    b.type = "button";
    b.addEventListener("click", function () { openBox(kind, id, name, block); });
    block.appendChild(b);
  }
  return block;
}

// -- the reply box ------------------------------------------------------------------------------

function boxState() {
  return replyState({ control: S.status.control, mind: S.status.mind, realm: obj(view).realm_on,
                      kind: box.kind, text: box.ta.value, sending: box.sending,
                      waiting: hasWaiting(repliesOf(view, box.kind, box.to)) });
}

function refreshBox() {
  if (!box) { return; }
  var s = boxState(), hint = "", bad = false;
  box.count.textContent = s.count + " / " + s.max;
  box.count.classList.toggle("bad", s.count > s.max);
  box.send.disabled = !s.enabled;
  box.cancel.disabled = box.sending;
  box.ta.readOnly = box.sending;  // its text stays as sent, and the focus stays in it
  if (box.refused) { hint = box.refused; bad = true; }
  else if (!s.enabled && (box.ta.value || s.reason !== REPLY_WORDS.empty)) {  // not on a fresh box
    hint = s.reason;
    bad = s.count > s.max;
  }
  box.why.textContent = hint;
  box.why.classList.toggle("bad", bad);
}

function setReplyButtons(disabled) {
  Array.prototype.forEach.call(document.querySelectorAll("#tab-board button.replybtn"), function (b) {
    b.disabled = disabled;
  });
}

function openBox(kind, to, name, block) {
  if (box) { return; }
  var root = el("div", "replybox"), label = el("label", "to", replyLabel(kind, name));
  var ta = el("textarea");
  ta.id = "board-reply-text";
  ta.rows = kind === "letter" ? 5 : 3;
  ta.maxLength = RAW_MAX;
  label.htmlFor = ta.id;
  var foot = el("div", "rfoot"), count = el("span", "count mono");
  var send = el("button", "btn primary", "Send"), cancel = el("button", "btn", "Cancel");
  send.type = "button";
  cancel.type = "button";
  var why = el("p", "why");
  foot.appendChild(count);
  foot.appendChild(send);
  foot.appendChild(cancel);
  root.appendChild(label);
  root.appendChild(ta);
  root.appendChild(foot);
  root.appendChild(why);
  block.appendChild(root);
  box = { kind: kind, to: to, ta: ta, count: count, send: send, cancel: cancel, why: why,
          sending: false, refused: "" };
  ta.addEventListener("input", function () { if (box) { box.refused = ""; refreshBox(); } });
  ta.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && box && !box.sending) { closeBox(); }
    else if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey) && !ev.repeat) { ev.preventDefault(); sendBox(); }
  });
  send.addEventListener("click", sendBox);
  cancel.addEventListener("click", function () { if (box && !box.sending) { closeBox(); } });
  setReplyButtons(true);
  refreshBox();
  ta.focus();
}

function closeBox() {
  box = null;
  if (view) { draw(view); }
}

function sendBox() {
  if (!box || box.sending) { return; }
  if (!boxState().enabled) { refreshBox(); return; }
  var b = box, text = b.ta.value;
  b.sending = true;
  b.refused = "";
  refreshBox();
  post("/api/board", { action: "reply", kind: b.kind, to: b.to, text: text }).then(function (j) {
    if (box !== b) { return; }
    b.sending = false;
    if (j.ok !== true) { b.refused = str(j.error) || "It was not saved."; refreshBox(); return; }
    view = withReply(view, b.kind, b.to, { id: typeof j.id === "number" ? j.id : null, ts: Date.now() / 1000,
                                           text: cleanReply(text), state: "waiting", reason: null,
                                           answer_post: null, answer_mail: null });
    closeBox();
    poll();
  }, function () {
    if (box !== b) { return; }
    b.sending = false;
    b.refused = NO_ANSWER;
    refreshBox();
  });
}

// -- the board ----------------------------------------------------------------------------------

function draw(v) {
  var forum = arr(v.forum), letters = arr(v.letters), writeups = arr(v.writeups);
  var kpis = [
    { k: "Forum posts", v: String(postCount(forum)), d: forum.length + " threads" },
    { k: "Letters", v: String(letters.length), d: "delivered to you" },
    { k: "Write-ups", v: String(writeups.length), d: "of the Realm's events" }];
  if (v.replies_on === true) { kpis.push({ k: "Your replies", v: String(v.waiting || 0), d: "waiting for tonight" }); }
  renderKpis($("board-kpis"), kpis);
  var note = $("board-note"), fbox = $("board-forum"), lbox = $("board-letters"), wbox = $("board-writeups");
  var empty = $("board-empty"), panels = $("board-panels");
  var notes = [];
  if (v.realm_on === false) { notes.push(REALM_OFF); }
  if (v.replies_on === false && !boardEmpty(v)) { notes.push(REPLIES_OLD); }
  note.hidden = !notes.length;
  note.textContent = notes.join(" ");
  clear(fbox);
  clear(lbox);
  clear(wbox);
  if (boardEmpty(v)) {
    panels.hidden = true;
    empty.hidden = false;
    notSetUp(empty, NOTHING);
    return;
  }
  panels.hidden = false;
  empty.hidden = true;
  forum.forEach(function (t) {
    var tbox = el("div", "thread"), skip = answeredHere(t.posts);
    tbox.appendChild(el("h4", "", str(t.thread)));
    arr(t.posts).forEach(function (p) {
      if (skip[p.id]) { return; }  // drawn under the reply it answers
      var r = el("div", "srow");
      r.appendChild(whoLine(p.author_kind === "member" ? p.author : 0, str(p.name)));
      if (p.guild) { r.appendChild(el("span", "chip", str(p.guild))); }
      r.appendChild(el("span", "muted", when(p.ts)));
      r.appendChild(el("p", "text", str(p.text)));
      r.appendChild(repliesBlock(v, "post", p.id, str(p.name)));
      tbox.appendChild(r);
    });
    fbox.appendChild(tbox);
  });
  if (!forum.length) { fbox.appendChild(el("p", "note", "No forum posts yet.")); }
  var skipLetters = answeredLetters(letters);
  letters.forEach(function (m) {
    if (skipLetters[m.id]) { return; }  // drawn under the reply it answers
    var r = el("div", "srow letter"), name = nameOf(v, m.sender);
    r.appendChild(whoLine(m.sender, name));
    r.appendChild(el("strong", "", str(m.subject)));
    r.appendChild(el("span", "muted", when(m.ts)));
    if (m.state === "taken") { r.appendChild(el("span", "chip ok", "taken")); }
    r.appendChild(el("p", "text", str(m.body)));
    arr(m.items).forEach(function (it) {
      r.appendChild(el("span", "chip item " + qualityClass(obj(it).quality), itemText(it)));
    });
    r.appendChild(repliesBlock(v, "letter", m.id, name));
    lbox.appendChild(r);
  });
  if (!letters.length) { lbox.appendChild(el("p", "note", "No letter has reached you yet.")); }
  writeups.forEach(function (w) {
    var r = el("div", "srow");
    r.appendChild(el("span", "chip", str(w.kind)));
    r.appendChild(el("span", "", str(w.place)));
    r.appendChild(whoLine(w.organiser, nameOf(v, w.organiser)));
    r.appendChild(el("span", "muted", when(w.start) + ", " + minutes(w) + " min"));
    r.appendChild(el("p", "text", str(w.text)));
    wbox.appendChild(r);
  });
  if (!writeups.length) { wbox.appendChild(el("p", "note", "No write-up yet.")); }
}

function renderBoard(v) {
  view = v;
  if (box) { refreshBox(); return; }  // held while he writes: drawn when the box closes
  draw(v);
}

var poll = pollTab("board", { path: "/api/board", ms: BOARD_MS, render: renderBoard });
// A reply saved or withdrawn (from this page or another): the open tab asks again at once
on("board", function () { poll(); });
on("status", refreshBox);
