import { arr, num, obj, str } from "../core.mjs";

// The Comms voice rooms as data: every room the snapshot lists, in the mind's order, a child under
// its parent and gone while the parent is folded, with who sits where from the voice service's
// state; and his own mute and deafen. No DOM here: node imports this file.
//
// While voice is down every room is empty (greyed) and only the Officers room is locked: the mind
// always locks it (to officers and him); other locks come from the voice service's tree.

export var CROWDED = 8;              // more than this in a room and its faces fold into a strip
export var SHOWN_WHEN_CROWDED = 5;   // the faces that strip shows, before its "+N"

function parentOf(r, known) {
  var p = str(r.parent);
  return p && p !== r.id && known[p] ? p : "";
}
function hisRoom(voice) {
  var where = obj(voice.where), him = num(voice.human);
  return str(voice.room) || (him && typeof where[him] === "string" ? where[him] : "");
}

// room id -> [{guid, name, speaking, you}], him first, then by name
function seated(voice) {
  var by = {}, where = obj(voice.where), names = obj(voice.names), him = num(voice.human);
  var talking = arr(voice.speaking), here = hisRoom(voice);
  function put(room, p) { (by[room] = by[room] || []).push(p); }
  Object.keys(where).forEach(function (g) {
    var room = where[g], guid = Number(g);
    if (typeof room !== "string" || !room || !(guid > 0) || (him && guid === him)) { return; }
    put(room, { guid: guid, name: str(names[g]) || "#" + guid, speaking: talking.indexOf(guid) >= 0, you: false });
  });
  if (here) { put(here, { guid: him, name: str(names[him]) || "You", speaking: voice.ptt === true, you: true }); }
  Object.keys(by).forEach(function (room) {
    by[room].sort(function (a, b) {
      if (a.you !== b.you) { return a.you ? -1 : 1; }
      var x = a.name.toLowerCase(), y = b.name.toLowerCase();
      return x < y ? -1 : x > y ? 1 : a.guid - b.guid;
    });
  });
  return by;
}

export function roomRows(rooms, voice, collapsed) {
  collapsed = obj(collapsed);
  var defs = arr(rooms).filter(function (r) { return r && typeof r.id === "string" && r.id; });
  var known = {}, kids = {}, locks = {}, people = {}, here = "", out = [];
  defs.forEach(function (r) { known[r.id] = true; });
  defs.forEach(function (r) {
    var p = parentOf(r, known);
    if (p) { (kids[p] = kids[p] || []).push(r); }
  });
  if (voice) {
    arr(voice.tree).forEach(function (r) { if (r && r.locked === true) { locks[r.id] = true; } });
    people = seated(voice);
    here = hisRoom(voice);
  }
  function add(r, depth) {
    var inside = people[r.id] || [], hasKids = !!kids[r.id], open = !(hasKids && collapsed[r.id] === true);
    var crowded = inside.length > CROWDED;
    out.push({ id: r.id, name: str(r.name) || r.id, depth: depth, hasKids: hasKids, open: open,
               count: inside.length, capacity: num(r.capacity), locked: r.id === "officers" || !!locks[r.id],
               here: !!here && r.id === here, empty: inside.length === 0, crowded: crowded,
               people: crowded ? inside.slice(0, SHOWN_WHEN_CROWDED) : inside });
    if (hasKids && open) { kids[r.id].forEach(function (c) { add(c, depth + 1); }); }
  }
  defs.forEach(function (r) { if (!parentOf(r, known)) { add(r, 0); } });
  return out;
}

// His own mute and deafen as the controls show them. The voice service keeps the flags as sent and
// deafen implies mute, so Mute reads as on while he is deafened.
export function selfView(voice) {
  var s = voice ? obj(voice.self) : {}, deafen = s.deafen === true;
  return { mute: s.mute === true || deafen, deafen: deafen };
}
// What a press of "mute" or "deafen" sends. Undeafening leaves his own mute as it was; unmuting
// while deafened undeafens too, so the Mute he sees as on goes off.
export function selfPress(voice, which) {
  var s = voice ? obj(voice.self) : {}, mute = s.mute === true, deafen = s.deafen === true;
  if (which === "deafen") { return { type: "self", mute: mute, deafen: !deafen }; }
  if (deafen) { return { type: "self", mute: false, deafen: false }; }
  return { type: "self", mute: !mute, deafen: false };
}
