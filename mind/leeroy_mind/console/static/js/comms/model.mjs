import { arr, num, obj, own, plural, str } from "../core.mjs";
import { signed } from "../words.mjs";

// The Comms feed as data: the lines the console sends (typed in WoW and spoken on voice) sorted
// into views, # guild, # officer, # party / raid and one whisper thread per member, with read
// marks and the KPI band's figures. No DOM here: node imports this file.
//
// A read mark is the newest line id read in a view. A view with no stored mark is read up to the
// newest line of the feed's first load (its floor), so the history a first open (or a reload)
// brings is never unread, while a line or a thread that comes after it is. His own lines never
// count. Each view keeps its own newest KEEP lines, so a busy guild never pushes a whisper out.
// A whisper the voice service dropped because he was deafened or had muted the member comes as
// text with `unheard` (the reason): it shows, and counts, like a typed whisper, with its marker.

export var KEEP = 200;
var VIEW = /^(?:guild|officer|group|w:[1-9]\d{0,9})$/;
var GROUP = { party: 1, raid: 1, raid_warning: 1 };
var CHANNELS = { guild: "guild", officer: "officer", party: "party", raid: "raid",
                 raid_warning: "raid warning", whisper: "whisper" };
var UNHEARD = { deafened: "not heard (deafened)", muted: "not heard (muted)" };

function mine(line, human) { return line.you === true || (human > 0 && line.from_guid === human); }
function goodId(id) { return typeof id === "number" && id > 0 && Math.floor(id) === id; }

// "guild", "officer", "group", "w:<guid>" (a whisper's other party), "" for a line no view shows
export function viewOf(line, human) {
  var ch = str(line.channel);
  if (ch === "guild" || ch === "officer") { return ch; }
  if (ch === "voice:officers") { return "officer"; }
  if (ch.indexOf("voice:") === 0) { return "guild"; }
  if (own(GROUP, ch)) { return "group"; }
  if (ch === "whisper") {
    var other = mine(line, num(human)) ? num(line.to_guid) : num(line.from_guid);
    return goodId(other) ? "w:" + other : "";
  }
  return "";
}

// A line's chips, as {cls, text}: where it was said (a voice room by room(id), "voice" for a spoken
// whisper, else the WoW channel), then "not heard (...)" for a whisper he did not hear
export function lineChips(line, room) {
  room = room || function (id) { return id; };
  var out = [];
  if (line.voice) {
    out.push({ cls: "chip voice", text: line.voice === "whisper" ? "voice" : room(str(line.voice)) });
  } else {
    var ch = str(line.channel);
    out.push({ cls: "chip wow", text: own(CHANNELS, ch) ? CHANNELS[ch] : ch });
  }
  if (typeof line.unheard === "string" && own(UNHEARD, line.unheard)) {
    out.push({ cls: "chip unheard", text: UNHEARD[line.unheard] });
  }
  return out;
}

export class Feed {
  constructor(o) {
    o = o || {};
    this.human = num(o.human);
    this.now = o.now || function () { return Date.now() / 1000; };
    this.views = new Map();    // view -> Map(id -> line)
    this.where = new Map();    // id -> view
    this.marks = Object.create(null);
    this.floor = null;
    this.opened = new Map();   // guid -> {name, at}: threads he opened, kept while the page lives
  }
  upsert(lines) {
    var top = 0;
    arr(lines).forEach((l) => {
      if (!l || !goodId(l.id)) { return; }
      var v = viewOf(l, this.human);
      if (!v) { return; }
      var was = this.where.get(l.id);
      if (was && was !== v) { this._remove(l.id); }
      var m = this.views.get(v);
      if (!m) { m = new Map(); this.views.set(v, m); }
      m.set(l.id, l);
      this.where.set(l.id, v);
      top = Math.max(top, l.id);
      if (m.size > KEEP) {
        Array.from(m.keys()).sort(function (a, b) { return a - b; }).slice(0, m.size - KEEP)
          .forEach((id) => { this._remove(id); });
      }
    });
    if (this.floor === null) { this.floor = top; }
  }
  // A snapshot's lines are the feed as it is now; null (the console could not read it) keeps the
  // lines and the floor here. True when it was adopted.
  adopt(chat) {
    if (!Array.isArray(chat)) { return false; }
    var now = new Set();
    chat.forEach(function (l) { if (l && goodId(l.id)) { now.add(l.id); } });
    this.drop(this.ids().filter(function (id) { return !now.has(id); }));
    this.upsert(chat);
    return true;
  }
  drop(ids) { arr(ids).forEach((id) => { this._remove(id); }); }
  _remove(id) {
    var v = this.where.get(id);
    if (!v) { return; }
    var m = this.views.get(v);
    m.delete(id);
    this.where.delete(id);
    if (!m.size) { this.views.delete(v); }
  }
  ids() { return Array.from(this.where.keys()).sort(function (a, b) { return a - b; }); }
  // The lines a view shows (none still on its way to him), oldest first
  lines(view) {
    var m = this.views.get(view);
    if (!m) { return []; }
    return Array.from(m.values()).filter(function (l) { return l.heard !== false; })
      .sort(function (a, b) { return a.id - b.id; });
  }
  mark(view) {
    return own(this.marks, view) ? this.marks[view] : this.floor === null ? Infinity : this.floor;
  }
  unread(view) {
    var at = this.mark(view), human = this.human;
    return this.lines(view).filter(function (l) { return l.id > at && !mine(l, human); }).length;
  }
  // Read up to the view's newest shown line; true when the mark moved
  markRead(view) {
    var shown = this.lines(view);
    if (!shown.length) { return false; }
    var newest = shown[shown.length - 1].id, at = this.mark(view);
    if (own(this.marks, view) && newest <= at) { return false; }
    this.marks[view] = Math.max(newest, at === Infinity ? 0 : at);
    return true;
  }
  loadRead(stored) {
    var s = obj(stored);
    Object.keys(s).forEach((view) => {
      if (VIEW.test(view) && goodId(s[view])) { this.marks[view] = s[view]; }
    });
  }
  // Every mark worth keeping: the stored ones and each present view's, its floor included
  readState() {
    var out = {};
    Object.keys(this.marks).forEach((v) => { out[v] = this.marks[v]; });
    if (this.floor !== null) {
      this.views.forEach((m, v) => { if (!own(out, v)) { out[v] = this.floor; } });
    }
    return out;
  }
  openThread(guid, name) {
    guid = num(guid);
    if (!goodId(guid)) { return ""; }
    if (!this.opened.has(guid)) { this.opened.set(guid, { name: str(name), at: this.now() }); }
    return "w:" + guid;
  }
  // One per member with a shown whisper line or a thread he opened, the newest first
  threads() {
    var out = [], guids = new Set(), human = this.human;
    this.views.forEach(function (m, v) { if (v.indexOf("w:") === 0) { guids.add(Number(v.slice(2))); } });
    this.opened.forEach(function (o, g) { guids.add(g); });
    guids.forEach((g) => {
      var view = "w:" + g, shown = this.lines(view), opened = this.opened.get(g);
      if (!shown.length && !opened) { return; }
      var last = shown.length ? shown[shown.length - 1] : null, name = "";
      for (var i = shown.length - 1; i >= 0 && !name; i--) {
        name = str(mine(shown[i], human) ? shown[i].to : shown[i].from);
      }
      out.push({ t: { guid: g, name: name || (opened && opened.name) || "#" + g, last: last,
                      unread: this.unread(view) },
                 at: last ? num(last.ts) : opened.at, id: last ? last.id : Infinity });
    });
    out.sort(function (a, b) { return b.at - a.at || b.id - a.id; });
    return out.map(function (x) { return x.t; });
  }
}

// The Comms KPI band from the shown lines since `since` (epoch seconds: his session's start while
// he is in game, else twelve hours back), the voice service's state and the guild's mood
export function kpis(feed, voice, mood, since) {
  since = num(since);
  var said = 0, spoken = 0, toHim = 0, human = feed.human;
  feed.views.forEach(function (m, v) {
    feed.lines(v).forEach(function (l) {
      if (num(l.ts) < since) { return; }
      said++;
      if (l.voice) { spoken++; }
      if (l.channel === "whisper" && !mine(l, human)) { toHim++; }
    });
  });
  var waiting = feed.threads().filter(function (t) {
    return t.last && !mine(t.last, human) && num(t.last.ts) >= since;
  }).length;
  var people = 0, rooms = {};
  if (voice) {
    var where = obj(voice.where), him = num(voice.human), here = str(voice.room);
    Object.keys(where).forEach(function (g) {
      if (typeof where[g] === "string" && where[g]) { people++; rooms[where[g]] = true; }
    });
    if (here && !(him && typeof where[him] === "string" && where[him])) { people++; rooms[here] = true; }
  }
  var reasons = mood ? arr(mood.reasons) : [];
  return [
    { k: "Said tonight", v: String(said), d: spoken + " on voice" },
    { k: "Whispers to you", v: String(toHim), d: waiting + " unanswered" },
    { k: "In voice", v: voice ? String(people) : "-",
      d: voice ? "across " + plural(Object.keys(rooms).length, "room") : "voice not connected" },
    { k: "Guild mood", v: mood ? signed(mood.value, 2) : "-",
      d: mood ? str(reasons[0]) : "not recorded yet" }];
}
