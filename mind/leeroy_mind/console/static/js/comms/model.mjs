import { arr, num, obj, own, plural, str } from "../core.mjs";
import { signed } from "../words.mjs";
import { BoundedMap } from "../resilience.mjs";

// The Comms feed as data: the lines the console sends (typed in WoW and spoken on voice) sorted
// into views, # guild, # officer, # party / raid, the voice transcript (every room he heard) and
// one whisper thread per member, with read marks and the KPI band's figures. No DOM here: node
// imports this file.
//
// Nothing he said or heard is lost to the page: the snapshot brings the last days, `index` lists
// every member he ever whispered with (the console's `threads`), and `older` adds the earlier
// lines a view reads back from /api/feed/history, which a later snapshot keeps.
//
// A read mark is the newest line id read in a view. A view with no stored mark is read up to the
// newest line of the feed's first load (its floor), so the history a first open (or a reload)
// brings is never unread, while a line or a thread that comes after it is. His own lines never
// count. Each view keeps its own newest KEEP lines, so a busy guild never pushes a whisper out.
// A whisper the voice service dropped because he was deafened or had muted the member comes as
// text with `unheard` (the reason): it shows, and counts, like a typed whisper, with its marker.

export var KEEP = 200;
export var VIEW_LIMIT = 64, READ_LIMIT = 128;
var VIEW = /^(?:guild|officer|group|voice|w:[1-9]\d{0,9})$/;
var FIXED = new Set(["guild", "officer", "group", "voice"]);
var GROUP = { party: 1, raid: 1, raid_warning: 1 };
var CHANNELS = { guild: "guild", officer: "officer", party: "party", raid: "raid",
                 raid_warning: "raid warning", whisper: "whisper" };
var UNHEARD = { deafened: "not heard (deafened)", muted: "not heard (muted)" };

function mine(line, human) { return line.you === true || (human > 0 && line.from_guid === human); }
function goodId(id) { return typeof id === "number" && id > 0 && Math.floor(id) === id; }

// "guild", "officer", "group", "voice" (any room), "w:<guid>" (a whisper's other party), "" for a
// line no view shows
export function viewOf(line, human) {
  var ch = str(line.channel);
  if (ch === "guild" || ch === "officer") { return ch; }
  if (ch.indexOf("voice:") === 0) { return "voice"; }
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
    this.active = "guild";
    this.where = new Map();    // id -> view
    this.marks = Object.create(null);
    this.floor = null;
    this.opened = new BoundedMap(VIEW_LIMIT); // recent explicitly opened threads
    this.index = new Map();    // guid -> {name, last}: every member he ever whispered with
    this.more = new Map();     // view -> whether older lines remain; absent: not known yet
    this.held = new Set();     // views whose history he read back: open, they keep every line
    this.gaps = new Map();     // view -> {edge, floor, more}: lines between floor and edge may be missing
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
      else { this.views.delete(v); this.views.set(v, m); }
      m.set(l.id, l);
      this.where.set(l.id, v);
      top = Math.max(top, l.id);
      if (v.indexOf("w:") === 0 && l.heard !== false) { this._indexLine(Number(v.slice(2)), l); }
      // The open view keeps every line once he has read its history back; any other view (that
      // one too, once he has left it) keeps its newest KEEP
      if (m.size > KEEP && !(v === this.active && this.held.has(v))) {
        Array.from(m.keys()).sort(function (a, b) { return a - b; }).slice(0, m.size - KEEP)
          .forEach((id) => { this._remove(id); });
        this.more.set(v, true);
        this.held.delete(v);
        this.gaps.delete(v);
      }
      while (this.views.size > VIEW_LIMIT) {
        var old = Array.from(this.views.keys()).find(key => !FIXED.has(key) && key !== this.active);
        Array.from(this.views.get(old).keys()).forEach(id => this._remove(id));
        this.more.delete(old);  // opened again, it reads its lines back
        this.held.delete(old);
        this.gaps.delete(old);
      }
    });
    if (this.floor === null) { this.floor = top; }
  }
  // A snapshot's lines are the feed as it is now, from `since` (epoch seconds) on, every shown
  // line of each view but those it names `partial` (cut to their newest lines): a line here it
  // lacks within what it covered of the line's view (newer than its oldest line there, or from
  // `since` on in a view it holds whole) has gone; an older one was read back through the history
  // and stays. Without `since` (an older console) the snapshot is the whole feed. A view that
  // loses lines may read them back again.
  // null (the console could not read it) keeps the lines and the floor here. True when adopted.
  adopt(chat, since, partial) {
    if (!Array.isArray(chat)) { return false; }
    var now = new Set(), low = new Map(), human = this.human, cutViews = new Set(arr(partial));
    chat.forEach(function (l) {
      if (!l || !goodId(l.id)) { return; }
      now.add(l.id);
      var v = viewOf(l, human);
      if (v && !(low.get(v) < l.id)) { low.set(v, l.id); }
    });
    var cut = typeof since === "number" && isFinite(since) ? since : null;
    var gone = this.ids().filter((id) => {
      if (now.has(id)) { return false; }
      if (cut === null) { return true; }
      var v = this.where.get(id);
      if (low.has(v) && id > low.get(v)) { return true; }
      return !cutViews.has(v) && num(this.views.get(v).get(id).ts) >= cut;
    });
    gone.forEach((id) => { this.more.set(this.where.get(id), true); });
    this.drop(gone);
    this.upsert(chat);
    // A thread the snapshot cut whose newest line here is older than its index's (set first, from
    // the same snapshot): it is behind, and history reads only backward, so its window is let go
    // and read again from its newest line when it opens
    cutViews.forEach((v) => {
      var m = this.views.get(v), k = v.indexOf("w:") === 0 ? this.index.get(Number(v.slice(2))) : null;
      if (!m) { return; }
      if (!low.has(v)) {
        if (!k || Math.max.apply(null, Array.from(m.keys())) >= k.last.id) { return; }
        Array.from(m.keys()).forEach((id) => { this._remove(id); });
        this.more.delete(v);
        this.held.delete(v);
        this.gaps.delete(v);
        return;
      }
      // Its newest lines sit above lines read back before: what fell between may be missing, and
      // the gap is read back from the snapshot's oldest line down (feedview fills it)
      var edge = low.get(v), below = Array.from(m.keys()).filter(function (id) { return id < edge; });
      if (!below.length) { return; }
      var was = this.gaps.get(v);
      this.gaps.set(v, { edge: edge, floor: Math.max.apply(null, below),
                         more: was ? was.more : this.more.get(v) });
    });
    return true;
  }
  // Every member he ever whispered with ({guid, name, last}, the console's `threads`); null (it
  // could not read them) keeps the list. True when adopted.
  setThreads(list) {
    if (!Array.isArray(list)) { return false; }
    var index = new Map();
    list.forEach(function (t) {
      if (t && goodId(t.guid) && t.last && goodId(t.last.id)) {
        index.set(t.guid, { name: str(t.name), last: t.last });
      }
    });
    this.index = index;
    return true;
  }
  // A page of a view's history ({lines, more}, from /api/feed/history), read back into it. `from`
  // is the view's oldest line when it was asked (0: it held none); a page whose window changed
  // meanwhile (the view trimmed, or evicted and begun again) no longer joins on and is dropped:
  // false.
  older(view, page, from) {
    if (num(from) > 0 && this.oldest(view) !== from) { return false; }
    page = obj(page);
    var lines = arr(page.lines).filter(function (l) { return l && goodId(l.id); });
    var gap = this.gaps.get(view);
    this.held.add(view);
    if (gap) {
      // Filling a gap: closed once the page reaches the lines below it (or there is nothing older)
      this.upsert(lines);
      var reached = page.more !== true || !lines.length || lines.some(function (l) { return l.id <= gap.floor; });
      if (!this.gaps.has(view)) { return true; }  // trimmed meanwhile: its "more" stands
      if (reached) {
        this.gaps.delete(view);
        if (gap.more === undefined) { this.more.delete(view); } else { this.more.set(view, gap.more); }
      } else {
        gap.edge = Math.min.apply(null, lines.map(function (l) { return l.id; }));
      }
      return true;
    }
    this.more.delete(view);
    this.upsert(lines);
    // A view he left in the meantime was trimmed again (more: true), whatever the page said
    if (this.more.get(view) !== true) { this.more.set(view, page.more === true); }
    return true;
  }
  // Whether a view has a gap still to read back (between history and a newer snapshot)
  gapped(view) { return this.gaps.has(view); }
  // Where a view's history reads back from: its gap's edge while it has one, else the oldest line
  // id the page holds (0: none)
  oldest(view) {
    if (this.gaps.has(view)) { return this.gaps.get(view).edge; }
    var m = this.views.get(view), low = 0;
    if (m) { m.forEach(function (l, id) { if (!low || id < low) { low = id; } }); }
    return low;
  }
  // Whether a view may have older lines than it holds: true until its history says otherwise
  mayHaveMore(view) { return this.gaps.has(view) || (this.more.has(view) ? this.more.get(view) : true); }
  // Whether the page has asked a view's history yet
  known(view) { return this.more.has(view); }
  // Lines that went from the feed. A thread whose indexed newest line went falls back to its
  // newest line still here, or leaves the list until the next snapshot says otherwise.
  drop(ids) {
    var hit = new Set();
    arr(ids).forEach((id) => {
      var v = this.where.get(id), g = v && v.indexOf("w:") === 0 ? Number(v.slice(2)) : 0;
      var k = g ? this.index.get(g) : null;
      if (k && k.last.id === id) { hit.add(g); }
      this._remove(id);
    });
    hit.forEach((g) => {
      var shown = this.lines("w:" + g);
      if (shown.length) { this.index.set(g, { name: this.index.get(g).name, last: shown[shown.length - 1] }); }
      else { this.index.delete(g); }
    });
  }
  // A shown whisper line newer than (or the same as) its thread's indexed one becomes its newest
  _indexLine(g, l) {
    var k = this.index.get(g);
    if (k && k.last.id > l.id) { return; }
    var name = str(mine(l, this.human) ? l.to : l.from) || (k ? k.name : "");
    this.index.set(g, { name: name, last: l });
  }
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
    delete this.marks[view];
    this.marks[view] = Math.max(newest, at === Infinity ? 0 : at);
    this._trimMarks(this.marks);
    return true;
  }
  loadRead(stored) {
    var s = obj(stored);
    Object.keys(s).forEach((view) => {
      if (VIEW.test(view) && goodId(s[view])) { this.marks[view] = s[view]; }
    });
    this._trimMarks(this.marks);
  }
  _trimMarks(marks) {
    var keys = Object.keys(marks);
    while (keys.length > READ_LIMIT) {
      var old = keys.find(v => !FIXED.has(v) && v !== this.active && !this.views.has(v));
      delete marks[old]; keys = Object.keys(marks);
    }
  }
  // Every mark worth keeping: the stored ones and each present view's, its floor included
  readState() {
    var out = {};
    Object.keys(this.marks).forEach((v) => { out[v] = this.marks[v]; });
    if (this.floor !== null) {
      this.views.forEach((m, v) => { if (!own(out, v)) { out[v] = this.floor; } });
    }
    this._trimMarks(out);
    return out;
  }
  openThread(guid, name) {
    guid = num(guid);
    if (!goodId(guid)) { return ""; }
    this.opened.set(guid, this.opened.get(guid) || { name: str(name), at: this.now() });
    return "w:" + guid;
  }
  // One per member he ever whispered with (the index), with a shown whisper line or a thread he
  // opened, the newest first
  threads() {
    var out = [], guids = new Set(), human = this.human;
    this.views.forEach(function (m, v) { if (v.indexOf("w:") === 0) { guids.add(Number(v.slice(2))); } });
    this.opened.forEach(function (o, g) { guids.add(g); });
    this.index.forEach(function (t, g) { guids.add(g); });
    guids.forEach((g) => {
      var view = "w:" + g, shown = this.lines(view), opened = this.opened.get(g), known = this.index.get(g);
      if (!shown.length && !opened && !known) { return; }
      var last = shown.length ? shown[shown.length - 1] : null, name = "";
      if (known && (!last || known.last.id > last.id)) { last = known.last; }
      for (var i = shown.length - 1; i >= 0 && !name; i--) {
        name = str(mine(shown[i], human) ? shown[i].to : shown[i].from);
      }
      out.push({ t: { guid: g, name: name || (known && known.name) || (opened && opened.name) || "#" + g, last: last,
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
