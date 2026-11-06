import { $, arr, clear, clock, el, emit, num, on, pad, recall, remember, str } from "../core.mjs";
import { S, name, roomName } from "../state.mjs";
import { face } from "../faces.mjs";
import { registerTab, setBadge } from "../tabs.mjs";
import { Feed, kpis } from "./model.mjs";

// The Comms middle column: the open view of the feed (# guild, # officer, # party / raid or one
// whisper thread), each line with its face, name, time, a chip for where it was said and its text;
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
var CHANNELS = { guild: "guild", officer: "officer", party: "party", raid: "raid",
                 raid_warning: "raid warning", whisper: "whisper" };

export var FEED = new Feed();
FEED.loadRead(recall(READ_KEY, {}));
var VIEW = "guild";
var saved = "";

export function currentView() { return VIEW; }
export function openView(view) {
  if (!VIEW_RE.test(str(view))) { return; }
  VIEW = view;
  renderFeed(false);
}

function title(view) {
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
function chip(l) {
  if (l.voice) { return el("span", "chip voice", l.voice === "whisper" ? "voice" : roomName(str(l.voice))); }
  return el("span", "chip wow", CHANNELS[l.channel] || str(l.channel));
}
function row(l) {
  var r = el("div", "msg" + (l.you ? " mine" : "")), at = num(l.ts) * 1000;
  r.dataset.id = String(l.id);
  r.appendChild(face(l.from_guid, str(l.from), "feed", { you: l.you === true }));
  var m = el("div"), h = el("div", "h");
  h.appendChild(el("span", "who", l.you ? "You" : str(l.from) || "#" + l.from_guid));
  var t = el("time", "t", stamp(at));
  t.dateTime = new Date(at).toISOString();
  t.title = new Date(at).toLocaleString();
  h.appendChild(t);
  h.appendChild(chip(l));
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

// Redraw the open view. keepPlace: new lines leave the reader where he is unless he is at the
// bottom, where they keep him
export function renderFeed(keepPlace) {
  var box = $("feed");
  var bottom = box.scrollHeight - box.scrollTop - box.clientHeight < STICK_PX;
  var at = keepPlace && !bottom ? place(box) : null;
  $("feed-title").textContent = title(VIEW);
  $("feed-hint").textContent = HINTS[VIEW.indexOf("w:") === 0 ? "whisper" : VIEW];
  clear(box);
  var shown = FEED.lines(VIEW);
  if (!shown.length) { box.appendChild(el("div", "empty muted", "Nothing said here yet.")); }
  shown.forEach(function (l) { box.appendChild(row(l)); });
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
  var state = FEED.readState(), text = JSON.stringify(state);
  if (text !== saved) { saved = text; remember(READ_KEY, state); }
  setBadge("comms", FEED.threads().reduce(function (n, t) { return n + t.unread; }, 0));
  renderKpis();
  emit("feed");
}

// A snapshot (the first, or one after a reconnect) holds the feed as it is now
on("snapshot", function (d) {
  var now = {};
  arr(d.chat).forEach(function (l) { if (l) { now[l.id] = true; } });
  FEED.drop(FEED.ids().filter(function (id) { return !now[id]; }));
  FEED.upsert(d.chat);
  renderFeed(false);
});
on("chat", function (d) { FEED.upsert(d.lines); renderFeed(true); });
on("chat_gone", function (d) { FEED.drop(d.ids); renderFeed(true); });
on("status", renderKpis);
on("voice", function (m) {
  if (m.type === "state" || m.type === "down" || m.type === "moved") { renderKpis(); }
});
document.addEventListener("visibilitychange", function () {
  if (document.visibilityState === "visible") { changed(); }
});
registerTab("comms", { show: function () { renderFeed(false); } });
