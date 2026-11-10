import { $, arr, clear, el, obj, on, own, plural, post, recall, remember, str } from "../core.mjs";
import { S, hereRoom, isMuted, name, roomName } from "../state.mjs";
import { face, faceButton, lightSpeakers } from "../faces.mjs";
import { FEED, currentView, openView } from "./feedview.mjs";
import { Heard } from "./heard.mjs";
import { roomRows, selfPress, selfView } from "./tree.mjs";
import { typingDots, typingWhispers } from "./typingview.mjs";
import { channelOf } from "./discord.mjs";

// The Comms left column, top to bottom: the WoW channels with their unread counts (a click opens
// the view), every voice room (folding groups, greyed empties, locks, n / 3, who sits where; a
// click joins), the whisper threads (dots beside a member writing to him, whose thread comes
// first when it is new), and his controls (the push-to-talk lamp and his room, Mute
// and Deafen, the voice service's status). Also the toasts (the console's own notes for NOTE_MS,
// and the invites) and the Comms banner. A member's row or face opens their card (rightview.mjs);
// a speaking event only lights the faces already drawn.

var NOTE_MS = 8000;             // how long one of the console's own notes stays up
var NOTES_SHOWN = 5;
var COLLAPSED_KEY = "guild-console-collapsed";
var CHANNELS = [["guild", "# guild"], ["officer", "# officer"], ["group", "# party / raid"]];

var NOTES = [];
var collapsed = obj(recall(COLLAPSED_KEY, {}));

// Redraw a box and hand the focus back to the control it was on, found again by its data key
function keepFocus(box, draw) {
  var a = document.activeElement, key = null;
  if (a && a !== box && box.contains(a)) {
    ["view", "room", "fold", "guid", "card"].forEach(function (k) {
      if (!key && a.dataset && a.dataset[k] !== undefined) { key = [k, a.dataset[k]]; }
    });
  }
  draw();
  if (key) {
    var again = box.querySelector("[data-" + key[0] + '="' + CSS.escape(key[1]) + '"]');
    if (again) { again.focus({ preventScroll: true }); }
  }
}

// -- the channels and the whispers ---------------------------------------------------------------

function viewButton(view, open) {
  var b = el("button", "ch" + (view === open ? " on" : ""));
  b.type = "button";
  b.dataset.view = view;
  if (view === open) { b.setAttribute("aria-current", "true"); }
  return b;
}
function badge(b, n) {
  if (!(n > 0)) { return; }
  var s = el("span", "badge", String(n));
  s.title = plural(n, "unread line");
  b.appendChild(s);
}
function renderChannels() {
  var box = $("channels"), open = currentView();
  keepFocus(box, function () {
    clear(box);
    CHANNELS.forEach(function (c) {
      var b = viewButton(c[0], open);
      b.appendChild(el("span", "nm", c[1]));
      badge(b, FEED.unread(c[0]));
      box.appendChild(b);
    });
  });
}
function renderWhispers() {
  var box = $("whispers"), open = currentView(), threads = FEED.threads(), writing = typingWhispers();
  var known = new Set(threads.map(function (t) { return t.guid; }));
  threads = writing.filter(function (w) { return !known.has(w.guid); })
    .map(function (w) { return { guid: w.guid, name: w.name, unread: 0, fresh: true }; }).concat(threads);
  var typing = new Set(writing.map(function (w) { return w.guid; }));
  keepFocus(box, function () {
    clear(box);
    if (!threads.length) { box.appendChild(el("div", "none", "No whispers yet.")); }
    threads.forEach(function (t) {
      var b = viewButton("w:" + t.guid, open);
      b.appendChild(faceButton(t.guid, t.name, "list", { inButton: true }));
      b.appendChild(el("span", "nm", t.name));
      if (typing.has(t.guid)) { b.appendChild(typingDots(t.name + " is typing")); }
      if (t.fresh) { b.dataset.who = t.name; }  // opened, it stays a thread (FEED.openThread)
      badge(b, t.unread);
      box.appendChild(b);
    });
  });
}

// -- the voice rooms -----------------------------------------------------------------------------

// A row lights (data-speaker) while their voice plays, his own while his key is held; a member's
// row opens their card (data-card)
function person(p, depth) {
  var row = el("div", "member d" + depth + (p.you ? " you" : "") + (p.speaking ? " speaking" : "")
                      + (!p.you && isMuted(p.guid) ? " muted" : ""));
  row.dataset.guid = String(p.guid);
  row.dataset.speaker = String(p.guid);
  if (!p.you) {
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-label", p.name);
    row.dataset.card = String(p.guid);
    row.dataset.name = p.name;
  }
  row.appendChild(face(p.guid, p.name, "list", { you: p.you }));
  row.appendChild(el("span", "mname", p.you ? "You" : p.name));
  return row;
}
function crowd(r) {
  var strip = el("div", "room-faces d" + r.depth), more = r.count - r.people.length;
  r.people.forEach(function (p) {
    if (!p.you) { strip.appendChild(faceButton(p.guid, p.name, "list", { speaking: p.speaking })); return; }
    var me = el("span", "facebtn" + (p.speaking ? " speaking" : ""));
    me.dataset.speaker = String(p.guid);
    me.appendChild(face(p.guid, p.name, "list", { you: true }));
    strip.appendChild(me);
  });
  strip.appendChild(el("span", "av sm more", "+" + more));
  var names = r.people.map(function (p) { return p.you ? "You" : p.name; }).join(", ")
              + " and " + plural(more, "other");
  strip.title = names;
  strip.setAttribute("role", "group");
  strip.setAttribute("aria-label", names);
  return strip;
}
export function renderRooms() {
  var box = $("tree");
  keepFocus(box, function () {
    clear(box);
    roomRows(S.rooms, S.voice, collapsed).forEach(function (r) {
      var line = el("div", "rm d" + r.depth + (r.empty ? " empty" : ""));
      if (r.hasKids) {
        var fold = el("button", "twist", r.open ? "▾" : "▸");
        fold.type = "button";
        fold.dataset.fold = r.id;
        fold.setAttribute("aria-expanded", String(r.open));
        fold.setAttribute("aria-label", (r.open ? "Fold " : "Unfold ") + r.name);
        line.appendChild(fold);
      } else {
        line.appendChild(el("span", "twist"));
      }
      var b = el("button", "ch room" + (r.here ? " on" : ""));
      b.type = "button";
      b.dataset.room = r.id;
      b.title = r.here ? "You are here" : "Join " + r.name;
      if (r.here) { b.setAttribute("aria-current", "true"); }
      b.appendChild(el("span", "nm", r.name));
      if (r.locked) { b.appendChild(el("span", "lock", "locked")); }
      var n = r.capacity ? r.count + " / " + r.capacity : r.count ? String(r.count) : "";
      if (n) { b.appendChild(el("span", "n", n)); }
      line.appendChild(b);
      box.appendChild(line);
      if (r.crowded) { box.appendChild(crowd(r)); }
      else { r.people.forEach(function (p) { box.appendChild(person(p, r.depth)); }); }
    });
  });
}

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

// -- his controls --------------------------------------------------------------------------------

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
  seg("v-err", errs.length ? plural(errs.length, "problem") : "No problems", errs.length > 0);
  $("v-err").title = errs.join(", ");
}
function renderControls() {
  var v = S.voice, me = selfView(v), here = hereRoom();
  $("c-ptt").classList.toggle("on", !!(v && v.ptt));
  $("c-room").textContent = !v ? "voice not connected" : here ? roomName(here) : "not in a room";
  [["c-mute", me.mute], ["c-deafen", me.deafen]].forEach(function (x) {
    var b = $(x[0]);
    b.disabled = !v;
    b.setAttribute("aria-pressed", String(x[1]));
  });
  $("c-mute").title = !v ? "The voice service is not connected." : me.deafen ? "Muted while deafened: this undeafens too." : "";
  $("c-deafen").title = !v ? "The voice service is not connected." : "";
  renderVoiceStatus();
}
function pressSelf(which) {
  if (!S.voice) { return; }
  post("/api/voice", selfPress(S.voice, which)).then(function (j) {
    if (!j.ok) { note(j.error || "That did not change."); }
  }, function () { note("The console is not reachable."); });
}

// -- the toasts and the banner -------------------------------------------------------------------

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
function dropInvite(inv) {
  if (!S.voice) { return; }
  S.voice.invites = S.voice.invites.filter(function (i) { return i !== inv; });
  renderToasts();
}
function renderToasts() {
  var box = $("toasts");
  clear(box);
  NOTES.forEach(function (n) { box.appendChild(el("div", "toast", n.text)); });
  if (!S.voice) { return; }
  S.voice.invites.forEach(function (inv) {
    var t = el("div", "toast invite"), line = el("div", "line"), who = name(inv.from);
    line.appendChild(face(inv.from, who, "list"));
    line.appendChild(el("span", "", who + " asks you to join " + roomName(inv.channel) + "."));
    t.appendChild(line);
    var acts = el("div", "acts");
    var ignore = el("button", "", "Ignore");
    ignore.type = "button";
    ignore.addEventListener("click", function () { dropInvite(inv); });
    var join = el("button", "primary", "Join");
    join.type = "button";
    join.addEventListener("click", function () { askJoin(inv.channel); dropInvite(inv); });
    acts.appendChild(ignore);
    acts.appendChild(join);
    t.appendChild(acts);
    box.appendChild(t);
  });
}
function renderBanner() {
  var b = $("comms-banner"), text = "", bad = false;
  if (channelOf(currentView())) { b.hidden = true; return; }
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

export function renderLeft() {
  renderChannels(); renderRooms(); renderWhispers(); renderControls(); renderToasts(); renderBanner();
}

// -- what he does --------------------------------------------------------------------------------

$("left").addEventListener("click", function (ev) {
  if (ev.target.closest("[data-card]")) { return; }  // a member's face or row: their card (rightview.mjs)
  var t = ev.target.closest("[data-fold], [data-room], [data-view]");
  if (!t) { return; }
  if (t.dataset.fold) {
    if (collapsed[t.dataset.fold]) { delete collapsed[t.dataset.fold]; } else { collapsed[t.dataset.fold] = true; }
    remember(COLLAPSED_KEY, collapsed);
    renderRooms();
  } else if (t.dataset.room) {
    if (t.dataset.room !== hereRoom()) { askJoin(t.dataset.room); }
  } else {
    if (t.dataset.who) { FEED.openThread(Number(t.dataset.view.slice(2)), t.dataset.who); }
    openView(t.dataset.view);
  }
});
$("c-mute").addEventListener("click", function () { pressSelf("mute"); });
$("c-deafen").addEventListener("click", function () { pressSelf("deafen"); });

// -- the bus (S.voice is already up to date when a "voice" event comes) --------------------------

var ON_VOICE = {
  state: renderLeft, down: renderLeft,
  speaking: function (m) {
    lightSpeakers($("tree"), m.guid, !!S.voice && S.voice.speaking.indexOf(m.guid) >= 0);
  },
  ptt: function () {
    if (S.voice) { lightSpeakers($("tree"), S.voice.human, S.voice.ptt); }
    renderControls();
  },
  moved: function () { renderRooms(); renderControls(); renderToasts(); },
  invite: renderToasts,
  refused: function (m) { note(refusal(str(m.channel), m.reason)); },
  status: renderVoiceStatus,
  mind: renderBanner,
  self: renderControls
};
on("snapshot", renderLeft);
on("status", renderBanner);
// Push-to-talk the voice service recognised but the mind never matched to a line of his
var HEARD = new Heard(), HEARD_SHOWN = 160, heardTimer = null;
function heardNotes() {
  if (!S.voice) { return; }
  HEARD.due(Date.now()).forEach(function (e) {
    var t = e.text.length > HEARD_SHOWN ? e.text.slice(0, HEARD_SHOWN) + "…" : e.text;
    note("Heard you: \"" + t + "\" (not sent: the mind didn't match it to a line)");
  });
}
function heardWatch() {
  if (S.voice && !heardTimer) { heardTimer = setInterval(heardNotes, 1000); }
  if (!S.voice && heardTimer) { clearInterval(heardTimer); heardTimer = null; }
}
on("chat", function (d) {
  arr(d.lines).forEach(function (l) { HEARD.sawOwnVoiceLine(l, Date.now()); });
});
on("snapshot", heardWatch);
on("voice", function (m) {
  if (m && m.type === "transcript" && m.kind === "you") { HEARD.heardYou({ text: m.text }, Date.now()); }
  if (m && m.type === "down") { HEARD.clear(); }
  heardWatch();
  if (typeof m.type === "string" && own(ON_VOICE, m.type)) { ON_VOICE[m.type](m); }
});
on("feed", function () { renderChannels(); renderWhispers(); renderBanner(); });
on("typing", renderWhispers);
on("note", note);
