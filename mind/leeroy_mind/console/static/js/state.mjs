import { arr, num, obj, own, str } from "./core.mjs";

// What the page knows, shared by its modules: the console's status (the strip: the mind, the
// bridge, the game, voice and the control rules), the voice service's page state (null while it
// is down), the voice rooms and the guild's mood from the last snapshot, and the open tab.
// S.status, S.rooms, S.mood and S.voice are brought up to date before a "status", "snapshot" or
// "voice" event goes out, so every listener sees the same state. No DOM here.
export var S = {
  status: { mind: { state: "unknown", detail: "connecting" }, bridge: null,
            game: { state: "unknown", reason: "connecting" },
            voice: { enabled: false, service: false, link: null }, control: { enabled: false } },
  voice: null,
  rooms: [],
  mood: null,
  tab: "comms"
};

// What the voice service's state says about people and rooms
export function name(guid) {
  var n = S.voice && S.voice.names[guid];
  return typeof n === "string" && n ? n : "#" + guid;
}
export function roomName(id) {
  var lists = [S.rooms, S.voice ? S.voice.tree : []];
  for (var j = 0; j < lists.length; j++) {
    for (var i = 0; i < lists[j].length; i++) {
      if (lists[j][i] && lists[j][i].id === id) { return str(lists[j][i].name) || id; }
    }
  }
  return id;
}
export function hereRoom() {
  if (!S.voice) { return ""; }
  return S.voice.room || (S.voice.human && typeof S.voice.where[S.voice.human] === "string" ? S.voice.where[S.voice.human] : "");
}
export function isMuted(guid) { return !!S.voice && S.voice.mutes.indexOf(guid) >= 0; }
export function roomOf(guid) {
  if (!S.voice) { return ""; }
  var r = S.voice.where[String(guid)];
  return typeof r === "string" && r ? roomName(r) : "";
}

// -- the voice service's page state (protocol in voice/leeroy_voice/ui/server.py) -----------------

function selfOf(m) {
  var s = obj(m);
  return { mute: s.mute === true, deafen: s.deafen === true };
}
export function adoptVoice(m) {
  if (!m || typeof m !== "object") { S.voice = null; return; }
  S.voice = {
    tree: arr(m.tree).filter(function (r) { return r && typeof r.id === "string"; }),
    where: obj(m.where), names: obj(m.names), room: str(m.room), human: num(m.human),
    speaking: arr(m.speaking), ptt: m.ptt === true, volumes: obj(m.volumes), mutes: arr(m.mutes),
    invites: arr(m.invites).filter(function (i) { return i && typeof i.channel === "string"; }),
    mind: m.mind === true, status: obj(m.status), self: selfOf(m.self) };
}

var VOICE = {
  state: adoptVoice,
  down: function () { S.voice = null; },
  speaking: function (m) {
    S.voice.speaking = S.voice.speaking.filter(function (g) { return g !== m.guid; });
    if (m.on === true) { S.voice.speaking.push(m.guid); }
  },
  ptt: function (m) { S.voice.ptt = m.down === true; },
  moved: function (m) {
    var g = String(m.guid), room = str(m.channel), before = S.voice.where[g];
    if (room) { S.voice.where[g] = room; } else { delete S.voice.where[g]; }
    if (S.voice.human && Number(m.guid) === S.voice.human) {
      S.voice.room = room;
      S.voice.invites = S.voice.invites.filter(function (i) { return i.channel !== room; });
    }
    S.voice.invites = S.voice.invites.filter(function (i) {
      return !(String(i.from) === g && before === i.channel && room !== i.channel);
    });
  },
  invite: function (m) {
    if (!str(m.channel)) { return; }
    if (!S.voice.invites.some(function (i) { return i.from === m.from && i.channel === m.channel; })) {
      S.voice.invites.push({ from: m.from, channel: m.channel });
    }
  },
  refused: function (m) {
    S.voice.invites = S.voice.invites.filter(function (i) { return i.channel !== m.channel; });
  },
  status: function (m) {
    var s = {};
    Object.keys(m).forEach(function (k) { if (k !== "type") { s[k] = m[k]; } });
    S.voice.status = s;
  },
  mind: function (m) { S.voice.mind = m.up === true; },
  self: function (m) { S.voice.self = selfOf(m); }
};
// One event from the voice service into S.voice; while it is down only a fresh state counts
export function applyVoice(m) {
  var kind = m && typeof m.type === "string" ? m.type : "";
  if (!own(VOICE, kind) || (!S.voice && kind !== "state" && kind !== "down")) { return; }
  VOICE[kind](m);
}
