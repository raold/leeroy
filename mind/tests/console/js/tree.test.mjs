import test from "node:test";
import assert from "node:assert/strict";
import { CROWDED, SHOWN_WHEN_CROWDED, roomMates, roomRows, selfPress, selfView }
  from "../../../leeroy_mind/console/static/js/comms/tree.mjs";

// The snapshot's 32 rooms in the mind's order, and invented members only. He is 1000; members are
// 1101 and up.
var HIM = 1000;
var CLASS = [["warriors", "Warriors"], ["paladins", "Paladins"], ["hunters", "Hunters"], ["rogues", "Rogues"],
             ["priests", "Priests"], ["death_knights", "Death Knights"], ["shamans", "Shamans"],
             ["mages", "Mages"], ["warlocks", "Warlocks"], ["druids", "Druids"]];
function def(id, name, parent, capacity) { return { id: id, name: name, parent: parent || "", capacity: capacity || 0 }; }
var ROOMS = [def("guild_hall", "Guild Hall"), def("raid", "Raid"), def("raid2", "Raid 2", "raid"),
  def("officers", "Officers"), def("tanks", "Tanks"), def("healers", "Healers"), def("casters", "Casters"),
  def("melee", "Melee"), def("class_halls", "Class Halls")]
  .concat(CLASS.map(function (c) { return def(c[0], c[1], "class_halls"); }))
  .concat([def("loot_dkp", "Loot & DKP"), def("lore", "Lore & Strategy"), def("pvp", "PvP & Arena"),
    def("achievements", "Achievements & Mounts"), def("kitchen", "The Kitchen"), def("irl", "IRL"),
    def("smoke_room", "The Smoke Room"), def("afk", "AFK"), def("private1", "Private 1", "", 3),
    def("private2", "Private 2", "", 3), def("private3", "Private 3", "", 3),
    def("interview", "Interview Room"), def("penalty_box", "Penalty Box")]);

function voice(o) {
  return Object.assign({ tree: ROOMS.map(function (r) { return Object.assign({ locked: false }, r); }),
                         where: {}, names: {}, room: "", human: HIM, speaking: [], ptt: false,
                         self: { mute: false, deafen: false } }, o);
}
function seat(n, room, from) {
  var where = {}, names = {};
  for (var i = 0; i < n; i++) { where[String(from + i)] = room; names[String(from + i)] = "Member" + (from + i); }
  return { where: where, names: names };
}
function row(rows, id) { return rows.filter(function (r) { return r.id === id; })[0]; }
function ids(rows) { return rows.map(function (r) { return r.id; }); }

test("with voice down every room is listed, greyed, and only the Officers room is locked", () => {
  var rows = roomRows(ROOMS, null, {});
  assert.equal(rows.length, 32);
  assert.deepEqual(ids(rows), ROOMS.map(function (r) { return r.id; }));
  assert.ok(rows.every(function (r) { return r.empty && !r.here && r.count === 0 && r.people.length === 0; }));
  assert.deepEqual(rows.filter(function (r) { return r.locked; }).map(function (r) { return r.id; }), ["officers"]);
  var g = row(rows, "guild_hall");
  assert.deepEqual(Object.keys(g).sort(), ["capacity", "count", "crowded", "depth", "empty", "hasKids", "here",
                                           "id", "locked", "name", "open", "people"]);
  assert.equal(g.name, "Guild Hall");
});

test("Raid 2 and the ten class rooms sit under their parents and fold away with them", () => {
  var rows = roomRows(ROOMS, voice(), {});
  assert.equal(rows.length, 32);
  var raid = rows.findIndex(function (r) { return r.id === "raid"; });
  assert.equal(rows[raid + 1].id, "raid2");
  assert.equal(rows[raid + 1].depth, 1);
  assert.equal(rows[raid].depth, 0);
  assert.ok(rows[raid].hasKids && rows[raid].open);
  var halls = rows.findIndex(function (r) { return r.id === "class_halls"; });
  assert.deepEqual(ids(rows.slice(halls + 1, halls + 11)), CLASS.map(function (c) { return c[0]; }));
  assert.ok(rows.slice(halls + 1, halls + 11).every(function (r) { return r.depth === 1 && !r.hasKids; }));
  assert.equal(row(rows, "guild_hall").hasKids, false);

  var folded = roomRows(ROOMS, voice(), { raid: true });
  assert.equal(folded.length, 31);
  assert.equal(row(folded, "raid2"), undefined);
  assert.equal(row(folded, "raid").open, false);
  folded = roomRows(ROOMS, voice(), { raid: true, class_halls: true });
  assert.equal(folded.length, 21);
  assert.ok(CLASS.every(function (c) { return row(folded, c[0]) === undefined; }));
  assert.equal(row(folded, "class_halls").open, false);
  assert.equal(roomRows(ROOMS, null, { class_halls: true }).length, 22);
});

test("a private room reads its count against its capacity", () => {
  var s = seat(2, "private1", 1101);
  var rows = roomRows(ROOMS, voice(s), {});
  var p = row(rows, "private1");
  assert.equal(p.count, 2);
  assert.equal(p.capacity, 3);
  assert.equal(p.empty, false);
  assert.deepEqual(p.people.map(function (x) { return x.guid; }), [1101, 1102]);
  assert.equal(p.people[0].name, "Member1101");
  assert.equal(row(rows, "private2").count, 0);
  assert.equal(row(rows, "private2").empty, true);
  assert.equal(row(rows, "guild_hall").capacity, 0);
});

test("the Officers room is always locked and the voice tree locks the rest", () => {
  var tree = voice().tree.map(function (r) { return r.id === "raid" ? Object.assign({}, r, { locked: true }) : r; });
  var rows = roomRows(ROOMS, voice({ tree: tree }), {});
  assert.deepEqual(rows.filter(function (r) { return r.locked; }).map(function (r) { return r.id; }),
                   ["raid", "officers"]);
  assert.deepEqual(roomRows(ROOMS, voice(), {}).filter(function (r) { return r.locked; })
                   .map(function (r) { return r.id; }), ["officers"]);
});

test("a room of more than eight is crowded and shows five faces", () => {
  assert.equal(CROWDED, 8);
  assert.equal(SHOWN_WHEN_CROWDED, 5);
  var nine = row(roomRows(ROOMS, voice(seat(9, "guild_hall", 1101)), {}), "guild_hall");
  assert.equal(nine.count, 9);
  assert.equal(nine.crowded, true);
  assert.equal(nine.people.length, 5);
  var eight = row(roomRows(ROOMS, voice(seat(8, "guild_hall", 1101)), {}), "guild_hall");
  assert.equal(eight.count, 8);
  assert.equal(eight.crowded, false);
  assert.equal(eight.people.length, 8);
});

test("his own room is here and he is you, counted once and listed first", () => {
  var s = seat(2, "kitchen", 1101);
  s.where[String(HIM)] = "kitchen";
  s.names[String(HIM)] = "Owner";
  var rows = roomRows(ROOMS, voice(Object.assign(s, { room: "kitchen" })), {});
  var k = row(rows, "kitchen");
  assert.equal(k.here, true);
  assert.equal(k.count, 3);
  assert.deepEqual(k.people.map(function (p) { return [p.guid, p.you]; }), [[HIM, true], [1101, false], [1102, false]]);
  assert.equal(rows.filter(function (r) { return r.here; }).length, 1);
  var listed = row(roomRows(ROOMS, voice({ where: { 1000: "afk" }, room: "" }), {}), "afk");  // known from where alone
  assert.ok(listed.here && listed.people[0].you);
  assert.ok(row(roomRows(ROOMS, voice({ room: "afk", ptt: true }), {}), "afk").people[0].speaking);
});

test("a speaking guid is speaking", () => {
  var s = seat(3, "lore", 1101);
  var rows = roomRows(ROOMS, voice(Object.assign(s, { speaking: [1102] })), {});
  assert.deepEqual(row(rows, "lore").people.map(function (p) { return p.speaking; }), [false, true, false]);
});

test("deafen reads as mute too, and each press says what to send", () => {
  assert.deepEqual(selfView(null), { mute: false, deafen: false });
  assert.deepEqual(selfView(voice()), { mute: false, deafen: false });
  assert.deepEqual(selfView(voice({ self: { mute: true, deafen: false } })), { mute: true, deafen: false });
  assert.deepEqual(selfView(voice({ self: { mute: false, deafen: true } })), { mute: true, deafen: true });

  function press(mute, deafen, which) { return selfPress(voice({ self: { mute: mute, deafen: deafen } }), which); }
  assert.deepEqual(press(false, false, "mute"), { type: "self", mute: true, deafen: false });
  assert.deepEqual(press(true, false, "mute"), { type: "self", mute: false, deafen: false });
  assert.deepEqual(press(false, false, "deafen"), { type: "self", mute: false, deafen: true });
  assert.deepEqual(press(true, true, "deafen"), { type: "self", mute: true, deafen: false });   // his mute stays
  assert.deepEqual(press(false, true, "deafen"), { type: "self", mute: false, deafen: false });
  assert.deepEqual(press(false, true, "mute"), { type: "self", mute: false, deafen: false });   // unmute undeafens
  assert.deepEqual(press(true, true, "mute"), { type: "self", mute: false, deafen: false });
});

test("his room's faces are everyone there but him, by name, even in a crowded room", () => {
  assert.deepEqual(roomMates(null), []);
  assert.deepEqual(roomMates(voice(seat(3, "lore", 1101))), []);  // he is in no room
  var s = seat(12, "guild_hall", 1101);
  s.where[String(HIM)] = "guild_hall";
  s.where["1201"] = "afk";
  s.names["1105"] = "Aaron";
  var mates = roomMates(voice(Object.assign(s, { room: "guild_hall", speaking: [1107], ptt: true })));
  assert.equal(mates.length, 12);
  assert.deepEqual(mates[0], { guid: 1105, name: "Aaron", speaking: false });
  assert.ok(mates.every(function (p) { return p.guid !== HIM && p.guid !== 1201; }));
  assert.deepEqual(mates.filter(function (p) { return p.speaking; }).map(function (p) { return p.guid; }), [1107]);
  assert.deepEqual(roomMates(voice({ where: { 1000: "afk", 1101: "afk" }, names: { 1101: "Bexley" } })),
                   [{ guid: 1101, name: "Bexley", speaking: false }]);  // his room known from where alone
});
