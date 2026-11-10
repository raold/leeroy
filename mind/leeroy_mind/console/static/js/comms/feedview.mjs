import { $, clear, clock, el, emit, num, on, pad, recall, remember, str } from "../core.mjs";
import { S, name, roomName } from "../state.mjs";
import { face, faceButton } from "../faces.mjs";
import { registerTab, setBadge } from "../tabs.mjs";
import { Feed, kpis, lineChips } from "./model.mjs";
import { channelOf } from "./discord.mjs";
import { DISCORD } from "./discordstate.mjs";

// The Comms middle column: the open view of the feed (# guild, # officer, # party / raid or one
// whisper thread), each line with its face, name, time, a chip for where it was said (and "not
// heard" on a whisper he missed while deafened or with the member muted) and its text;
// the KPI band above the columns; the read marks (this browser's, in guild-console-read) and the
// Comms tab's count of unread whispers. A "feed" event tells the other columns something changed.

var READ_KEY = "guild-console-read";
var TONIGHT_S = 12 * 3600;     // "tonight" while he is not in game
var STICK_PX = 40;             // this close to the bottom, new lines keep him at the bottom
var VIEW_RE = /^(?:guild|officer|group|w:[1-9]\d{0,9})$/;
var TITLES = { guild: "# guild", officer: "# officer", group: "# party / raid" };
var HINTS = { guild: "Typed in WoW and said in the voice rooms, one feed, each line with its face.",
              officer: "Officer chat typed in WoW and what was said in the Officers room.",
              group: "Your party or raid, as typed in WoW.",
              whisper: "Your whispers with them, typed in WoW or spoken on voice." };

export var FEED = new Feed();
FEED.loadRead(recall(READ_KEY, {}));
var VIEW = "guild";
var saved = "";

export function currentView() { return VIEW; }
export function openView(view) {
  if (!VIEW_RE.test(str(view)) && !channelOf(view)) { return; }
  VIEW = view;
  renderFeed(false);
}

// A view's title: "# guild", "# officer", "# party / raid", or the whisper partner's name
export function viewTitle(view) {
  if (channelOf(view)) { return DISCORD.title(view); }
  if (TITLES[view]) { return TITLES[view]; }
  var guid = Number(view.slice(2)), t = FEED.threads().filter(function (x) { return x.guid === guid; })[0];
  return t ? t.name : name(guid);
}

// The clock time, with the day before it when the line is not from today (history goes 3 days back)
function stamp(ms) {
  var d = new Date(ms);
  return (d.toDateString() === new Date().toDateString() ? "" : pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " ")
         + clock(ms);
}
function row(l) {
  var r = el("div", "msg" + (l.you ? " mine" : "")), at = num(l.ts) * 1000;
  r.dataset.id = String(l.id);
  var who = str(l.from) || "#" + l.from_guid;  // a member's face opens their card (rightview.mjs)
  r.appendChild(l.you === true || !(num(l.from_guid) > 0) ? face(l.from_guid, str(l.from), "feed", { you: l.you === true })
                : faceButton(l.from_guid, who, "feed"));
  var m = el("div"), h = el("div", "h");
  h.appendChild(el("span", "who", l.you ? "You" : who));
  var t = el("time", "t", stamp(at));
  t.dateTime = new Date(at).toISOString();
  t.title = new Date(at).toLocaleString();
  h.appendChild(t);
  lineChips(l, roomName).forEach(function (c) { h.appendChild(el("span", c.cls, c.text)); });
  m.appendChild(h);
  m.appendChild(el("div", "body", str(l.text)));
  r.appendChild(m);
  return r;
}

// The reader's place: the first row still in sight and how far below the top it sits
function place(box) {
  var top = box.getBoundingClientRect().top, rows = box.children;
  for (var i = 0; i < rows.length; i++) {
    var b = rows[i].getBoundingClientRect();
    if (b.bottom > top && rows[i].dataset.id) { return { id: rows[i].dataset.id, off: b.top - top }; }
  }
  return null;
}
function keep(box, at) {
  var rows = box.children;
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].dataset.id === at.id) {
      box.scrollTop += rows[i].getBoundingClientRect().top - box.getBoundingClientRect().top - at.off;
      return;
    }
  }
}

// The rows drawn, by line id: a row stays in the page while its line is the same object (a line
// the console sent again replaces it) and its day has not turned, so a redraw on each new line
// never takes out a face he is clicking or the one that has the focus
var ROWS = new Map();  // id -> {line, day, node}
var EMPTY = el("div", "empty muted", "Nothing said here yet.");

// The row (by id) that holds the focus, if any: a row drawn again gets it back
function focusedRow(box) {
  var a = document.activeElement, r = a && a !== box && box.contains(a) ? a.closest(".msg") : null;
  return r ? r.dataset.id : "";
}

// Redraw the open view. keepPlace: new lines leave the reader where he is unless he is at the
// bottom, where they keep him
export function renderFeed(keepPlace) {
  if (channelOf(VIEW)) { emit("discord-open"); changed(); return; }
  var box = $("feed");
  var bottom = box.scrollHeight - box.scrollTop - box.clientHeight < STICK_PX;
  var at = keepPlace && !bottom ? place(box) : null;
  $("feed-title").textContent = viewTitle(VIEW);
  $("feed-hint").textContent = HINTS[VIEW.indexOf("w:") === 0 ? "whisper" : VIEW];
  var day = new Date().toDateString(), had = focusedRow(box), rows = new Map(), nodes = [];
  FEED.lines(VIEW).forEach(function (l) {
    var r = ROWS.get(l.id);
    if (!r || r.line !== l || r.day !== day) { r = { line: l, day: day, node: row(l) }; }
    rows.set(l.id, r);
    nodes.push(r.node);
  });
  ROWS = rows;
  if (!nodes.length) { nodes.push(EMPTY); }
  // Out first, then the new rows in their places: a row kept is never moved, and the kept rows
  // are already in id order
  var keepSet = new Set(nodes);
  Array.prototype.slice.call(box.children).forEach(function (c) { if (!keepSet.has(c)) { box.removeChild(c); } });
  nodes.forEach(function (n, i) { if (box.children[i] !== n) { box.insertBefore(n, box.children[i] || null); } });
  if (had && !box.contains(document.activeElement)) {
    var f = box.querySelector('.msg[data-id="' + had + '"] [data-card]');
    if (f) { f.focus({ preventScroll: true }); }
  }
  if (at) { keep(box, at); } else { box.scrollTop = box.scrollHeight; }
  changed();
}

function since() {
  var g = S.status.game;
  return g.state === "in_game" && num(g.since) > 0 ? num(g.since) : Date.now() / 1000 - TONIGHT_S;
}
function renderKpis() {
  var box = $("comms-kpis");
  clear(box);
  kpis(FEED, S.voice, S.mood, since()).forEach(function (x) {
    var k = el("div", "kpi");
    k.appendChild(el("span", "k", x.k));
    k.appendChild(el("span", "v", x.v));
    k.appendChild(el("span", "d", x.d));
    box.appendChild(k);
  });
}

// The open view is read while he can see it; the marks are kept whenever they change
function changed() {
  if (document.visibilityState === "visible" && S.tab === "comms") { FEED.markRead(VIEW); }
  var state = Object.assign({}, recall(READ_KEY, {}), FEED.readState(), DISCORD.read), text = JSON.stringify(state);
  if (text !== saved) { saved = text; remember(READ_KEY, state); }
  badge();
  renderKpis();
  emit("feed");
}

// A snapshot (the first, or one after a reconnect) holds the feed as it is now; one whose read
// failed (chat: null) leaves the lines, the floor and the reader's place as they are
on("snapshot", function (d) { renderFeed(!FEED.adopt(d.chat)); });
on("chat", function (d) { FEED.upsert(d.lines); renderFeed(true); });
on("chat_gone", function (d) { FEED.drop(d.ids); renderFeed(true); });
on("status", renderKpis);
on("voice", function (m) {
  if (m.type === "state" || m.type === "down" || m.type === "moved") { renderKpis(); }
});
document.addEventListener("visibilitychange", function () {
  if (document.visibilityState === "visible") { changed(); }
});
function badge() {
  setBadge("comms", FEED.threads().reduce(function (n, t) { return n + t.unread; }, 0)
    + DISCORD.channels().reduce(function (n, c) { return n + num(c.unread); }, 0));
}
on("discord-read", badge);
registerTab("comms", { show: function () { renderFeed(false); emit("comms-show"); }, hide: function () { emit("comms-hide"); } });
