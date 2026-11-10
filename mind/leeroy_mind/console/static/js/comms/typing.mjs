import { arr, num, own, str } from "../core.mjs";
import { viewOf } from "./model.mjs";

// Who is typing, as data: the strip's `typing` (the mind's turns being composed or typed, from
// its status) sorted into the Comms views their lines will appear in, with the label the feed
// shows and the members the whisper list marks. No DOM here: node imports this file.
//
// An entry older than STALE_S is dropped (a mind that died mid-turn never leaves dots behind):
// its age is the mind's `age` (read as it answered) plus the time since this page received the
// strip, on the page's own monotonic clock, so a device whose clock is off shows the same dots.
// The mind's `since` is never held against this device's clock; it only says whether the
// writer's line has landed in that view since the turn began (a line stamped after it), and then
// the dots go when the line comes, before the next strip says so.

export var STALE_S = 30;
var PLACES = { guild: 1, officer: 1, party: 1, raid: 1, raid_warning: 1, whisper: 1 };

function goodId(x) { return typeof x === "number" && x > 0 && Math.floor(x) === x; }

// "", "Arkon is typing", "Arkon and Belra are typing", "Several people are typing"
export function typingLabel(names) {
  names = arr(names);
  if (!names.length) { return ""; }
  if (names.length === 1) { return names[0] + " is typing"; }
  if (names.length === 2) { return names[0] + " and " + names[1] + " are typing"; }
  return "Several people are typing";
}

function seconds(x) { return typeof x === "number" && isFinite(x); }

// view -> [{guid, name, since, age, left}], oldest first, each member once (left: seconds until
// it goes stale). o: receivedAt and now (ms on one clock of this page's, any offset: when the
// strip came and now), human (his guid, 0 when the page does not know it), lines(view) (the
// view's shown lines) and name(guid) (the page's name for a member when the entry has none)
export function typingByView(list, o) {
  o = o || {};
  var elapsed = Math.max(0, num(o.now) - num(o.receivedAt)) / 1000;
  var human = num(o.human), lines = o.lines || function () { return []; };
  var nameOf = o.name || function (g) { return "#" + g; };
  var out = new Map();
  arr(list).forEach(function (e) {
    if (!e || typeof e !== "object" || !goodId(e.guid)) { return; }
    var ch = str(e.channel), since = e.since, age = e.age;
    var discord = /^discord:[a-z][a-z0-9-]{0,39}$/.test(ch), dm = /^dm:[1-9]\d{0,9}$/.test(ch);
    if ((!own(PLACES, ch) && !discord && !dm) || !seconds(since) || !seconds(age) || age < 0) { return; }
    var left = STALE_S - (age + elapsed);
    if (left < 0) { return; }
    if ((ch === "whisper" || dm) && !(goodId(e.to) && (!human || e.to === human))) { return; }
    if (dm && Number(ch.slice(3)) !== e.guid) { return; }
    var view = discord ? "d:" + ch.slice(8) : dm ? ch : viewOf({ channel: ch, from_guid: e.guid, to_guid: ch === "whisper" ? e.to : 0 }, human);
    if (!view) { return; }
    var landed = arr(lines(view)).some(function (l) {
      return l && l.from_guid === e.guid && num(l.ts) > since;
    });
    if (landed) { return; }
    var here = out.get(view) || [];
    var had = here.filter(function (t) { return t.guid === e.guid; })[0];
    if (had) {
      had.since = Math.min(had.since, since);
      had.age = Math.max(had.age, age);
      had.left = Math.min(had.left, left);
      return;
    }
    here.push({ guid: e.guid, name: str(e.name) || nameOf(e.guid), since: since, age: age, left: left });
    out.set(view, here);
  });
  out.forEach(function (l) { l.sort(function (a, b) { return a.since - b.since || a.guid - b.guid; }); });
  return out;
}

// Seconds until the next entry shown goes stale; Infinity when none is
export function staleIn(byView) {
  var next = Infinity;
  byView.forEach(function (l) { l.forEach(function (t) { next = Math.min(next, t.left); }); });
  return next;
}

// The members writing a whisper to him, oldest first: [{guid, name}]
export function whisperTypers(byView) {
  var out = [];
  byView.forEach(function (l, view) {
    if (view.indexOf("w:") === 0) { l.forEach(function (t) { out.push({ guid: t.guid, name: t.name, since: t.since }); }); }
  });
  out.sort(function (a, b) { return a.since - b.since || a.guid - b.guid; });
  return out.map(function (t) { return { guid: t.guid, name: t.name }; });
}
