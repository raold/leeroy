import { $, api, clear, el, emit, num, obj, on, own, post, refused, str } from "../core.mjs";
import { S, hereRoom, isMuted, roomName } from "../state.mjs";
import { faceButton, lightSpeakers } from "../faces.mjs";
import { photoButton } from "../gallery.mjs";
import { openMember } from "../personas.mjs";
import { cardText } from "../words.mjs";
import { FEED, openView } from "./feedview.mjs";
import { renderRooms } from "./leftview.mjs";
import { roomMates } from "./tree.mjs";

// The Comms right column: the faces of the room he is in, lit while their voice plays, and below
// them the card of the member whose face he clicked anywhere in Comms (the feed, the room tree, a
// whisper thread or here): their photo, name, age and where from, role, mood and feeling toward
// him (mind.db, through /api/persona), their volume and mute on the voice service, Whisper (the
// thread, and the focus in the reply box) and Profile (the Personas tab). His own face opens
// nothing. A face anywhere in Comms opens the card through its data-card (faces.mjs).

var DB_LIMIT = 12;
var shownFaces = null;   // what the faces show, so a move in another room draws nothing again
var CARD = { guid: 0, name: "", seq: 0, face: "" };

function fmtDb(db) { return (db > 0 ? "+" : db < 0 ? "-" : "") + Math.abs(db).toFixed(1) + " dB"; }
function volumeOf(guid) { return S.voice ? num(obj(S.voice.volumes)[guid]) : 0; }

// -- the room's faces ----------------------------------------------------------------------------

function renderFaces() {
  var here = hereRoom(), mates = roomMates(S.voice), box = $("here-faces");
  $("here-title").textContent = here ? "In " + roomName(here) : "Not in a voice room";
  var key = here + "|" + mates.map(function (p) { return p.guid + ":" + p.name; }).join(",");
  if (key !== shownFaces) {
    shownFaces = key;
    var a = document.activeElement, again = a && box.contains(a) && a.dataset ? a.dataset.card : "";
    clear(box);
    mates.forEach(function (p) { box.appendChild(faceButton(p.guid, p.name, "room")); });
    if (here && !mates.length) { box.appendChild(el("p", "none", "Nobody else is here.")); }
    var f = again ? box.querySelector('[data-card="' + CSS.escape(again) + '"]') : null;
    if (f) { f.focus({ preventScroll: true }); }
  }
  mates.forEach(function (p) { lightSpeakers(box, p.guid, p.speaking); });
}

// -- the card ------------------------------------------------------------------------------------

function renderVoiceControls() {
  if (!CARD.guid) { return; }
  var up = !!S.voice, db = volumeOf(CARD.guid), why = up ? "" : "The voice service is not connected.";
  $("mcard-vol").value = String(db);
  $("mcard-db").textContent = fmtDb(db);
  ["mcard-vol", "mcard-reset", "mcard-mute"].forEach(function (id) {
    $(id).disabled = !up;
    $(id).title = why;
  });
  $("mcard-mute").setAttribute("aria-pressed", String(isMuted(CARD.guid)));
}
// The card's photo, drawn again only for another member or name (CARD.face says which is shown)
function drawFace(name) {
  var faceKey = CARD.guid + "|" + name;
  if (CARD.face === faceKey) { return; }
  CARD.face = faceKey;
  clear($("mcard-face"));
  // A click opens the member's photo gallery: full size, every photo newest first, Make cover.
  $("mcard-face").appendChild(photoButton(CARD.guid, name, "card"));
}
// t: cardText's words; known: mind.db knows them (Profile has something to open)
function drawCard(t, known) {
  drawFace(t.name);
  $("mcard-name").textContent = t.name;
  $("mcard-note").textContent = t.note;
  var facts = $("mcard-facts");
  clear(facts);
  t.facts.forEach(function (f) {
    var d = el("div", "fact");
    d.appendChild(el("span", "k", f.k));
    d.appendChild(el("span", "v", f.v));
    facts.appendChild(d);
  });
  facts.hidden = !t.facts.length;
  $("mcard-profile").disabled = !known;
  $("mcard-profile").title = known ? "" : "Not in mind.db.";
  renderVoiceControls();
}

// Open the card of one member: what the page knows at once, then mind.db's words
export function openCard(guid, known) {
  guid = num(guid);
  if (!(guid > 0) || (S.voice && guid === S.voice.human)) { return; }
  var seq = ++CARD.seq;
  CARD.guid = guid;
  CARD.face = "";  // opened again: the photo is asked for again (a new one, or one where none was)
  CARD.name = str(known) || (S.voice && str(S.voice.names[guid])) || "#" + guid;
  $("mcard").hidden = false;
  drawCard({ name: CARD.name, note: "", facts: [] }, true);
  $("mcard").scrollIntoView({ block: "nearest" });
  function said(note) {
    if (seq === CARD.seq) { drawCard({ name: CARD.name, note: note, facts: [] }, true); }
  }
  api("/api/persona?guid=" + guid).then(function (r) {
    if (refused(r)) { return null; }
    return r.json().then(function (j) { return { status: r.status, j: obj(j) }; },
                         function () { return { status: r.status, j: {} }; });
  }).then(function (x) {
    if (!x || seq !== CARD.seq) { return; }
    if (x.status === 200) {
      var t = cardText(x.j, CARD.name);
      CARD.name = t.name;
      drawCard(t, true);
    } else if (x.status === 404) { drawCard(cardText(null, CARD.name), false); }
    else { said(str(x.j.error) || "mind.db could not be read."); }
  }, function () { said("The console is not reachable."); });
}

function setVolume(guid, db) {
  db = Math.max(-DB_LIMIT, Math.min(DB_LIMIT, db));
  if (S.voice) { if (db === 0) { delete S.voice.volumes[guid]; } else { S.voice.volumes[guid] = db; } }
  post("/api/voice", { type: "volume", guid: guid, db: db }).then(function (j) {
    if (!j.ok) { emit("note", j.error || "The volume did not change."); }
  }, function () { emit("note", "The console is not reachable."); });
}
function setMute(guid, muted) {
  if (S.voice) {
    S.voice.mutes = S.voice.mutes.filter(function (g) { return g !== guid; });
    if (muted) { S.voice.mutes.push(guid); }
  }
  post("/api/voice", { type: "mute", guid: guid, on: muted }).then(function (j) {
    if (!j.ok) { emit("note", j.error || "Mute did not change."); }
  }, function () { emit("note", "The console is not reachable."); });
  renderVoiceControls();
  renderRooms();  // the tree strikes a muted member through
}
// The reply box (#composer, composer.mjs): its line while it takes one, else the box itself
function focusComposer() {
  var box = $("composer"), f = box.querySelector("input:not(:disabled)");
  (f || box).focus();
}

$("mcard-vol").addEventListener("input", function () { $("mcard-db").textContent = fmtDb(Number(this.value)); });
$("mcard-vol").addEventListener("change", function () { if (CARD.guid) { setVolume(CARD.guid, Number(this.value)); } });
$("mcard-reset").addEventListener("click", function () {
  if (!CARD.guid) { return; }
  $("mcard-vol").value = "0";
  $("mcard-db").textContent = fmtDb(0);
  setVolume(CARD.guid, 0);
});
$("mcard-mute").addEventListener("click", function () { if (CARD.guid) { setMute(CARD.guid, !isMuted(CARD.guid)); } });
$("mcard-whisper").addEventListener("click", function () {
  if (!CARD.guid) { return; }
  openView(FEED.openThread(CARD.guid, CARD.name));
  focusComposer();
});
$("mcard-profile").addEventListener("click", function () { if (CARD.guid) { openMember(CARD.guid); } });

// A member's face anywhere in Comms opens their card: a click, or Enter or Space on a focusable
// row (a button clicks itself)
$("tab-comms").addEventListener("click", function (ev) {
  var t = ev.target.closest("[data-card]");
  if (t) { openCard(Number(t.dataset.card), t.dataset.name); }
});
$("tab-comms").addEventListener("keydown", function (ev) {
  if (ev.key !== "Enter" && ev.key !== " ") { return; }
  var t = ev.target.closest ? ev.target.closest("[data-card]") : null;
  if (!t || t !== ev.target || t.tagName === "BUTTON") { return; }
  ev.preventDefault();
  openCard(Number(t.dataset.card), t.dataset.name);
});

export function renderRight() { renderFaces(); renderVoiceControls(); }
// After a new key (main.mjs takeKey forgot the photos): the room's faces and the card's photo
// are drawn again, so they are asked for with it
export function refreshFaces() {
  shownFaces = null;
  renderFaces();
  if (CARD.guid) {
    CARD.face = "";
    drawFace(CARD.name);
  }
}

// -- the bus (S.voice is already up to date when a "voice" event comes) --------------------------

var ON_VOICE = {
  state: renderRight, down: renderRight, moved: renderFaces,
  speaking: function (m) {
    lightSpeakers($("here-faces"), m.guid, !!S.voice && S.voice.speaking.indexOf(m.guid) >= 0);
  }
};
on("snapshot", renderRight);
on("voice", function (m) {
  if (typeof m.type === "string" && own(ON_VOICE, m.type)) { ON_VOICE[m.type](m); }
});
