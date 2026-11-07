import { arr, num, own, str } from "../core.mjs";
import { viewOf } from "./model.mjs";

// Who is typing, as data: the strip's `typing` (the mind's turns being composed or typed, from
// its status) sorted into the Comms views their lines will appear in, with the label the feed
// shows and the members the whisper list marks. No DOM here: node imports this file.
//
// An entry older than STALE_S is dropped (a mind that died mid-turn never leaves dots behind),
// and so is one whose writer's line has already landed in that view since the turn began: the
// dots go when the line comes, before the next strip says so.

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

// view -> [{guid, name, since}], oldest first, each member once. o: now (epoch seconds), human
// (his guid, 0 when the page does not know it), lines(view) (the view's shown lines) and
// name(guid) (the page's name for a member when the entry has none)
export function typingByView(list, o) {
  o = o || {};
  var now = num(o.now), human = num(o.human), lines = o.lines || function () { return []; };
  var nameOf = o.name || function (g) { return "#" + g; };
  var out = new Map();
  arr(list).forEach(function (e) {
    if (!e || typeof e !== "object" || !goodId(e.guid)) { return; }
    var ch = str(e.channel), since = e.since;
    if (!own(PLACES, ch) || typeof since !== "number" || !isFinite(since) || now - since > STALE_S) { return; }
    if (ch === "whisper" && !(goodId(e.to) && (!human || e.to === human))) { return; }
    var view = viewOf({ channel: ch, from_guid: e.guid, to_guid: ch === "whisper" ? e.to : 0 }, human);
    if (!view) { return; }
    var landed = arr(lines(view)).some(function (l) {
      return l && l.from_guid === e.guid && num(l.ts) >= since;
    });
    if (landed) { return; }
    var here = out.get(view) || [];
    var had = here.filter(function (t) { return t.guid === e.guid; })[0];
    if (had) { had.since = Math.min(had.since, since); return; }
    here.push({ guid: e.guid, name: str(e.name) || nameOf(e.guid), since: since });
    out.set(view, here);
  });
  out.forEach(function (l) { l.sort(function (a, b) { return a.since - b.since || a.guid - b.guid; }); });
  return out;
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
