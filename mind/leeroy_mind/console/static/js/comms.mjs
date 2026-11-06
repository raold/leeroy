import { $, arr, clear, el, num, obj, on, own, post, recall, remember, str } from "./core.mjs";
import { S, hereRoom, name, roomName } from "./state.mjs";
import { renderStrip } from "./strip.mjs";

// The parts of Comms the rebuild has not replaced yet: the room tree, the volume menu, the
// invites and the voice status (the feed is comms/feedview.mjs)

var DB_LIMIT = 12;
var NOTE_MS = 8000;             // how long one of the console's own notes stays up
var NOTES_SHOWN = 5;
var COLLAPSED_KEY = "guild-console-collapsed";

var NOTES = [];
var collapsed = obj(recall(COLLAPSED_KEY, {}));
var menuGuid = 0;

function saveCollapsed() { remember(COLLAPSED_KEY, collapsed); }

// -- comms: the channel tree ---------------------------------------------------------------------

function blankVoice() {
  return { tree: [], where: {}, names: {}, room: "", human: 0, speaking: [], ptt: false,
           volumes: {}, mutes: [], invites: [], transcript: [], mind: false, status: {} };
}
function adoptVoice(m) {
  if (!m) { S.voice = null; return; }
  S.voice = blankVoice();
  S.voice.tree = arr(m.tree).filter(function (r) { return r && typeof r.id === "string"; });
  S.voice.where = obj(m.where); S.voice.names = obj(m.names); S.voice.room = str(m.room); S.voice.human = num(m.human);
  S.voice.speaking = arr(m.speaking); S.voice.ptt = m.ptt === true; S.voice.volumes = obj(m.volumes);
  S.voice.mutes = arr(m.mutes); S.voice.invites = arr(m.invites).filter(function (i) { return i && typeof i.channel === "string"; });
  S.voice.mind = m.mind === true; S.voice.status = obj(m.status);
}
function isSpeaking(g) { return S.voice && S.voice.speaking.indexOf(g) >= 0; }
function isMuted(g) { return S.voice && S.voice.mutes.indexOf(g) >= 0; }

function peopleByRoom() {
  var by = {};
  function put(room, p) { (by[room] = by[room] || []).push(p); }
  Object.keys(S.voice.where).forEach(function (g) {
    var room = S.voice.where[g];
    if (typeof room !== "string" || !room || Number(g) === S.voice.human) { return; }
    put(room, { guid: Number(g), you: false, label: name(g) });
  });
  var here = hereRoom();
  if (here) {
    var n = S.voice.human ? S.voice.names[S.voice.human] : "";
    put(here, { guid: S.voice.human, you: true, label: (typeof n === "string" && n ? n + " (you)" : "You") });
  }
  Object.keys(by).forEach(function (room) {
    by[room].sort(function (a, b) {
      if (a.you !== b.you) { return a.you ? -1 : 1; }
      return a.label.toLowerCase() < b.label.toLowerCase() ? -1 : 1;
    });
  });
  return by;
}
function renderTree() {
  var box = $("tree");
  clear(box);
  if (!S.voice) { box.appendChild(el("div", "row muted", "The voice service is not connected.")); return; }
  var here = hereRoom(), known = {}, children = {}, people = peopleByRoom(), drawn = {};
  S.voice.tree.forEach(function (r) { known[r.id] = true; });
  S.voice.tree.forEach(function (r) {
    var parent = r.parent && known[r.parent] && r.parent !== r.id ? r.parent : "";
    (children[parent] = children[parent] || []).push(r);
  });
  function person(p, into) {
    var talking = p.you ? S.voice.ptt : isSpeaking(p.guid), muted = !p.you && isMuted(p.guid);
    var row = el("div", "row member" + (p.you ? " you" : "") + (talking ? " speaking" : "") + (muted ? " muted" : ""));
    row.dataset.guid = String(p.guid);
    row.tabIndex = 0;
    row.appendChild(el("span", "light"));
    row.appendChild(el("span", "mname", p.label));
    var db = S.voice.volumes[p.guid];
    if (!p.you && typeof db === "number" && db !== 0) {
      row.appendChild(el("span", "tag", (db > 0 ? "+" : "") + db.toFixed(1) + " dB"));
    }
    if (muted) { row.appendChild(el("span", "tag", "muted")); }
    into.appendChild(row);
  }
  function room(r, into) {
    if (drawn[r.id]) { return; }
    drawn[r.id] = true;
    var inside = people[r.id] || [], subs = children[r.id] || [], open = !collapsed[r.id];
    var row = el("div", "row room" + (r.id === here ? " here" : ""));
    row.dataset.room = r.id;
    row.tabIndex = 0;
    row.title = r.id === here ? "You are here" : "Join " + (str(r.name) || r.id);
    row.appendChild(el("span", "twist" + (inside.length || subs.length ? "" : " none"), open ? "▾" : "▸"));
    if (r.locked === true) { row.appendChild(el("span", "padlock", "▣")); }
    row.appendChild(el("span", "rname", str(r.name) || r.id));
    var cap = num(r.capacity);
    if (inside.length || cap) { row.appendChild(el("span", "count", "(" + inside.length + (cap ? "/" + cap : "") + ")")); }
    into.appendChild(row);
    if (open && (inside.length || subs.length)) {
      var kids = el("div", "kids");
      inside.forEach(function (p) { person(p, kids); });
      subs.forEach(function (c) { room(c, kids); });
      into.appendChild(kids);
    }
  }
  (children[""] || []).forEach(function (r) { room(r, box); });
  S.voice.tree.forEach(function (r) { room(r, box); });
  Object.keys(people).filter(function (r) { return !known[r]; }).forEach(function (r) {
    room({ id: r, name: r, locked: false, capacity: 0 }, box);
  });
}

// -- comms: the console's own notes, invites, the banner, the voice status ------------------------

// A note (a refusal, an action's outcome) is a toast for NOTE_MS
function note(text) {
  var n = { text: str(text) };
  NOTES.push(n);
  if (NOTES.length > NOTES_SHOWN) { NOTES.shift(); }
  renderToasts();
  setTimeout(function () {
    NOTES = NOTES.filter(function (x) { return x !== n; });
    renderToasts();
  }, NOTE_MS);
}
function renderToasts() {
  var box = $("toasts");
  clear(box);
  NOTES.forEach(function (n) { box.appendChild(el("div", "toast", n.text)); });
  if (!S.voice) { return; }
  S.voice.invites.forEach(function (inv) {
    var t = el("div", "toast");
    t.appendChild(el("div", "", name(inv.from) + " asks you to join " + roomName(inv.channel) + "."));
    var acts = el("div", "acts");
    var ignore = el("button", "", "Ignore");
    ignore.type = "button";
    ignore.addEventListener("click", function () { dropInvite(inv); });
    var join = el("button", "", "Join");
    join.type = "button";
    join.addEventListener("click", function () { askJoin(inv.channel); dropInvite(inv); });
    acts.appendChild(ignore);
    acts.appendChild(join);
    t.appendChild(acts);
    box.appendChild(t);
  });
}
function dropInvite(inv) {
  if (!S.voice) { return; }
  S.voice.invites = S.voice.invites.filter(function (i) { return i !== inv; });
  renderToasts();
}
function renderBanner() {
  var b = $("comms-banner"), text = "", bad = false;
  if (!S.status.voice.enabled) { text = "Voice is off in mind.toml ([voice] enabled = false)."; }
  else if (!S.voice) { text = "The voice service is not running. Start it from the Mind tab."; bad = true; }
  else if (!S.voice.mind) {
    text = S.status.mind.state === "running"
      ? "The mind is running but not connected to the voice service yet: members stay quiet."
      : "The mind is " + S.status.mind.state + ": members stay quiet and push-to-talk is discarded until it is back.";
  }
  b.textContent = text;
  b.hidden = !text;
  b.classList.toggle("bad", bad);
}
function seg(id, text, bad) {
  var e = $(id);
  e.textContent = text;
  e.classList.toggle("bad", !!bad);
}
function renderVoiceStatus() {
  var s = S.voice ? S.voice.status : {}, errs = arr(s.errors);
  seg("v-out", "Output: " + (s.ok === true ? "ok" : s.ok === false ? "down" : "-"), s.ok === false);
  seg("v-mic", "Mic: " + (str(s.mic) || "-"), s.mic === "missing");
  seg("v-stt", "Recognition: " + (str(s.stt) || "-"), s.stt === "off");
  seg("v-queue", "Queue: " + (typeof s.queue === "number" ? s.queue : "-"), false);
  seg("v-err", errs.length ? errs.length + (errs.length === 1 ? " problem" : " problems") : "No problems", errs.length > 0);
  $("v-err").title = errs.join(", ");
}
export function renderComms() { renderTree(); renderToasts(); renderVoiceStatus(); renderBanner(); }

// -- comms: what he does --------------------------------------------------------------------------

function refusal(room, reason) {
  var r = roomName(room);
  if (reason === "offline") {
    return S.status.bridge === false
      ? "The game bridge is down, so the mind cannot see you in game: " + r + " stays closed."
      : "You're not in game: rooms open when you log in.";
  }
  if (reason === "locked") { return r + " is locked."; }
  if (reason === "full") { return r + " is full."; }
  if (reason === "unknown") { return r + " does not exist."; }
  return r + " cannot be joined.";
}
function askJoin(room) {
  post("/api/voice", { type: "join", channel: room }).then(function (j) {
    if (!j.ok) { note(j.error || "That room cannot be joined now."); }
  }, function () { note("The console is not reachable."); });
}
function setVolume(guid, db) {
  db = Math.max(-DB_LIMIT, Math.min(DB_LIMIT, db));
  if (S.voice) { if (db === 0) { delete S.voice.volumes[guid]; } else { S.voice.volumes[guid] = db; } }
  post("/api/voice", { type: "volume", guid: guid, db: db }).then(function (j) {
    if (!j.ok) { note(j.error || "The volume did not change."); }
  });
  renderTree();
}
function setMute(guid, on) {
  if (S.voice) {
    S.voice.mutes = S.voice.mutes.filter(function (g) { return g !== guid; });
    if (on) { S.voice.mutes.push(guid); }
  }
  post("/api/voice", { type: "mute", guid: guid, on: on }).then(function (j) {
    if (!j.ok) { note(j.error || "Mute did not change."); }
  });
  renderTree();
}
function closeMenu() { $("menu").hidden = true; menuGuid = 0; }
function fmtDb(db) { return (db > 0 ? "+" : db < 0 ? "-" : "") + Math.abs(db).toFixed(1) + " dB"; }
function openMenu(guid, x, y) {
  var menu = $("menu"), db = (S.voice && S.voice.volumes[guid]) || 0;
  menuGuid = guid;
  $("menu-name").textContent = name(guid);
  $("menu-vol").value = String(db);
  $("menu-db").textContent = fmtDb(db);
  $("menu-mute").checked = isMuted(guid);
  menu.hidden = false;
  menu.style.left = Math.max(4, Math.min(x, window.innerWidth - menu.offsetWidth - 4)) + "px";
  menu.style.top = Math.max(4, Math.min(y, window.innerHeight - menu.offsetHeight - 4)) + "px";
  $("menu-vol").focus();
}
$("menu-vol").addEventListener("input", function () { $("menu-db").textContent = fmtDb(Number(this.value)); });
$("menu-vol").addEventListener("change", function () { if (menuGuid) { setVolume(menuGuid, Number(this.value)); } });
$("menu-mute").addEventListener("change", function () { if (menuGuid) { setMute(menuGuid, this.checked); } });
$("menu-reset").addEventListener("click", function () {
  $("menu-vol").value = "0";
  $("menu-db").textContent = fmtDb(0);
  if (menuGuid) { setVolume(menuGuid, 0); }
});
document.addEventListener("mousedown", function (ev) {
  if (!$("menu").hidden && !$("menu").contains(ev.target)) { closeMenu(); }
});
document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") { closeMenu(); } });
window.addEventListener("resize", closeMenu);
$("tree").addEventListener("scroll", closeMenu);
$("tree").addEventListener("contextmenu", function (ev) {
  var row = ev.target.closest(".member");
  if (!row || row.classList.contains("you")) { closeMenu(); return; }
  ev.preventDefault();
  openMenu(Number(row.dataset.guid), ev.clientX, ev.clientY);
});
$("tree").addEventListener("click", function (ev) {
  var row = ev.target.closest(".room");
  if (!row) { return; }
  var id = row.dataset.room;
  if (ev.target.closest(".twist")) {
    if (collapsed[id]) { delete collapsed[id]; } else { collapsed[id] = true; }
    saveCollapsed();
    renderTree();
  } else if (id !== hereRoom()) {
    askJoin(id);
  }
});
$("tree").addEventListener("keydown", function (ev) {
  var row = ev.target.closest ? ev.target.closest(".row") : null;
  if (!row || (ev.key !== "Enter" && ev.key !== " ")) { return; }
  ev.preventDefault();
  if (row.dataset.room !== undefined) {
    if (row.dataset.room !== hereRoom()) { askJoin(row.dataset.room); }
  } else if (!row.classList.contains("you")) {
    var box = row.getBoundingClientRect();
    openMenu(Number(row.dataset.guid), box.left + 24, box.bottom);
  }
});
// -- what the voice service sends (through the console) -------------------------------------------

var VOICE = {
  state: function (m) { adoptVoice(m); renderComms(); renderStrip(); },
  down: function () { S.voice = null; renderComms(); renderStrip(); },
  speaking: function (m) {
    if (!S.voice) { return; }
    S.voice.speaking = S.voice.speaking.filter(function (g) { return g !== m.guid; });
    if (m.on === true) { S.voice.speaking.push(m.guid); }
    renderTree(); renderStrip();
  },
  ptt: function (m) { if (S.voice) { S.voice.ptt = m.down === true; renderTree(); renderStrip(); } },
  moved: function (m) {
    if (!S.voice) { return; }
    var g = String(m.guid), room = str(m.channel), before = S.voice.where[g];
    if (room) { S.voice.where[g] = room; } else { delete S.voice.where[g]; }
    if (S.voice.human && Number(m.guid) === S.voice.human) {
      S.voice.room = room;
      S.voice.invites = S.voice.invites.filter(function (i) { return i.channel !== room; });
    }
    S.voice.invites = S.voice.invites.filter(function (i) {
      return !(String(i.from) === g && before === i.channel && room !== i.channel);
    });
    renderTree(); renderToasts(); renderStrip();
  },
  invite: function (m) {
    if (!S.voice || !str(m.channel)) { return; }
    if (!S.voice.invites.some(function (i) { return i.from === m.from && i.channel === m.channel; })) {
      S.voice.invites.push({ from: m.from, channel: m.channel });
    }
    renderToasts();
  },
  refused: function (m) {
    note(refusal(str(m.channel), m.reason));
    if (S.voice) { S.voice.invites = S.voice.invites.filter(function (i) { return i.channel !== m.channel; }); }
    renderToasts();
  },
  status: function (m) {
    if (!S.voice) { return; }
    var s = {};
    Object.keys(m).forEach(function (k) { if (k !== "type") { s[k] = m[k]; } });
    S.voice.status = s;
    renderVoiceStatus();
  },
  mind: function (m) { if (S.voice) { S.voice.mind = m.up === true; renderBanner(); } }
};

on("snapshot", function (d) {
  adoptVoice(d.voice);
  renderComms(); renderStrip();
});
on("status", renderBanner);
on("voice", function (m) { if (typeof m.type === "string" && own(VOICE, m.type)) { VOICE[m.type](m); } });
on("note", note);
