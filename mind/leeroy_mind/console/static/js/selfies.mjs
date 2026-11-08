import { $, api, arr, clear, el, num, obj, post, refused, setText, str } from "./core.mjs";
import { face } from "./faces.mjs";
import { photoButton } from "./gallery.mjs";
import { MORE_TITLE, countsText, currentText, forgetShots, historyRows, leadText, pageOf, pickLabel,
         recentRows, refocusSelector, whyOff } from "./selfiemodel.mjs";
import { S } from "./state.mjs";

// -- the Personas tab's profile photos: the "Choose selfies" card and a member's Profile photo ---
// Read from mind.db through /api/selfies (the queue, its counts and the running mind's recent
// changes) and /api/selfies/images (one ready set's thumbnails as data URLs). Every pick, undo
// and "4 more" goes to the running mind's selfie op through POST /api/selfie, which checks it
// again and says why when it refuses; one at a time. The photo in force is the member's face
// (faces.mjs), never shown another way. personas.mjs drives this: its poll, its member detail and
// the "selfie" event.

var JPEG = "data:image/jpeg;base64,";
var SEL = null, busy = false, again = false, changing = false, at = 0, drawn = "", names = {};
// "guid:set" -> its images' answer. A ready set's files never change, but which slot is the photo
// in force does: a change forgets that member's sets (forgetMemberShots)
var SHOTS = new Map();
var who = { guid: 0, name: "", seq: 0, view: undefined };

function live() { return !changing && !whyOff(SEL); }

// The roster's names, for the recent choices
export function setSelfieNames(n) { names = obj(n); }

// A photo picked, undone or asked for again, here or on another page (the "selfie" event)
export function forgetMemberShots(guid) { forgetShots(SHOTS, guid); }

export function pollSelfies() {
  if (S.tab !== "personas") { return; }
  if (busy) { again = true; return; }
  busy = true;
  api("/api/selfies").then(function (r) {
    if (refused(r)) { return null; }
    return r.json().then(function (j) { return { status: r.status, j: j }; });
  }).then(function (x) {
    if (!x) { return; }
    if (x.status === 200) { SEL = x.j; renderSelfies(); }
    else { setText("sf-lead", str(x.j.error) || "mind.db could not be read."); }
  }, function () { /* the strip says it */ })
    .then(function () {
      busy = false;
      if (again) { again = false; pollSelfies(); }
    });
}

function renderSelfies() {
  if (!SEL) { return; }
  var v = obj(SEL.view), why = whyOff(SEL), ok = live();
  setText("sf-counts", v.available ? countsText(v.counts) : "");
  setText("sf-lead", leadText(v));
  $("sf-why").textContent = why;
  $("sf-why").hidden = !why;
  var queue = arr(v.queue), keep = {};
  queue.forEach(function (q) { keep[q.guid + ":" + q.set] = true; });
  SHOTS.forEach(function (_, key) { if (!keep[key]) { SHOTS.delete(key); } });
  var pg = pageOf(queue, at), recent = recentRows(SEL, names);
  at = pg.at;
  $("sf-pager").hidden = pg.pages < 2;
  setText("sf-at", pg.text);
  $("sf-prev").disabled = at === 0;
  $("sf-next").disabled = at >= pg.pages - 1;
  var sig = JSON.stringify([pg.rows, ok, why, recent]);
  if (sig !== drawn) {  // a poll that changed nothing keeps the rows, and the focus in them
    drawn = sig;
    drawQueue(pg.rows, ok, why);
    drawRecent(recent, ok, why);
  }
  if (who.guid) { renderMemberPhoto(); }
}

function drawQueue(rows, ok, why) {
  var box = $("sf-queue");
  clear(box);
  rows.forEach(function (q) {
    var name = str(q.name) || "#" + num(q.guid), row = el("div", "sfrow"), nm = el("div", "who");
    row.dataset.guid = String(q.guid);
    row.dataset.set = String(q.set);
    nm.appendChild(face(q.guid, name, "list"));
    nm.appendChild(el("span", "", name));
    row.appendChild(nm);
    var shots = el("div", "shots");
    for (var i = 0; i < 4; i++) {
      var b = el("button", "shot");
      b.type = "button";
      b.dataset.index = String(i);
      b.setAttribute("aria-label", pickLabel(i, name));
      b.disabled = true;  // until its photo is in
      shots.appendChild(b);
    }
    row.appendChild(shots);
    var more = el("button", "", "4 more");
    more.type = "button";
    more.title = MORE_TITLE;
    more.dataset.more = "1";
    more.disabled = !ok;
    row.appendChild(more);
    box.appendChild(row);
    loadShots(q.guid, q.set).then(function (j) { fillShots(shots, j, ok, why); });
  });
}

function loadShots(guid, set) {
  var key = guid + ":" + set;
  if (!SHOTS.has(key)) {
    SHOTS.set(key, api("/api/selfies/images?guid=" + num(guid) + "&set=" + num(set)).then(function (r) {
      if (refused(r) || r.status !== 200) { return null; }
      return r.json();
    }).then(function (j) {
      if (!j) { SHOTS.delete(key); }  // asked again at the next poll
      return j;
    }, function () { SHOTS.delete(key); return null; }));
  }
  return SHOTS.get(key);
}

function fillShots(shots, j, ok, why) {
  if (!j || !shots.parentNode) { return; }
  arr(j.images).forEach(function (im) {
    var b = shots.children[num(im.index)];
    if (!b || typeof im.src !== "string" || im.src.indexOf(JPEG) !== 0 || b.firstChild) { return; }
    var img = el("img");
    img.alt = "";
    img.src = im.src;
    b.appendChild(img);
    b.disabled = !ok;
    if (!ok) { b.title = why; }
    if (j.current === im.index) { b.classList.add("now"); b.title = "Their photo now"; }
  });
  for (var i = 0; i < shots.children.length; i++) {
    if (!shots.children[i].firstChild) { shots.children[i].title = "This photo could not be read."; }
  }
}

function drawRecent(rows, ok, why) {
  var box = $("sf-recent");
  clear(box);
  if (rows === null) { box.appendChild(el("p", "muted", "Recent choices show while the mind is running.")); return; }
  if (!rows.length) { box.appendChild(el("p", "muted", "No choices yet.")); return; }
  rows.forEach(function (r) { box.appendChild(changeRow(r, ok, why, "sf-status")); });
}

function changeRow(r, ok, why, status) {
  var d = el("div", "chg");
  d.appendChild(el("span", "", r.text));
  if (r.undoable) {
    var b = el("button", "", "Undo");
    b.type = "button";
    b.disabled = !ok;
    if (!ok) { b.title = why; }
    b.addEventListener("click", function () { change({ action: "undo", pick: r.id }, status); });
    d.appendChild(document.createTextNode(" "));
    d.appendChild(b);
  }
  return d;
}

// The mind's answer in a status line, with an Undo of a pick that changed the photo
function say(id, text, bad, undo) {
  var p = $(id);
  clear(p);
  p.className = "why" + (bad ? " bad" : "");
  p.appendChild(el("span", "", text));
  if (undo) {
    var b = el("button", "", "Undo");
    b.type = "button";
    b.addEventListener("click", function () { change({ action: "undo", pick: undo }, id); });
    p.appendChild(document.createTextNode(" "));
    p.appendChild(b);
  }
}

var DOING = { pick: "Setting the photo.", undo: "Undoing.", more: "Asking for four more." };
function change(args, status) {
  if (changing) { return; }
  changing = true;
  drawn = "";
  renderSelfies();
  say(status, DOING[args.action]);
  var refused = true, guid = num(args.guid);
  post("/api/selfie", args).then(function (r) {
    if (r.ok !== true) { say(status, "Not done: " + (str(r.error) || "refused"), true); return; }
    refused = false;
    guid = num(r.guid) || guid;
    say(status, str(r.message) || "Done.", false, args.action === "pick" && typeof r.pick === "number" ? r.pick : 0);
  }, function () { say(status, "Not done: the console could not be reached.", true); })
    .then(function () {
      changing = false;
      if (!refused && guid) { forgetShots(SHOTS, guid); }
      drawn = "";
      renderSelfies();
      refocus(args, refused, status);
      pollSelfies();
      if (who.guid && !$("p-detail").hidden) { loadMember(); }
    });
}

// The queue and the status line were drawn again, so the focused button is gone: back to the
// button that was clicked after a refusal (a shot once its picture is in), else the status
// line's Undo, else the status line
function refocus(args, refused, status) {
  var line = $(status), sel = refocusSelector(args, refused, status);
  var b = sel ? document.querySelector(sel) : null;
  function toLine() { (line.querySelector("button") || line).focus(); }
  if (!b) { toLine(); return; }
  if (!b.disabled) { b.focus(); return; }
  if (args.action !== "pick") { toLine(); return; }
  loadShots(args.guid, args.set).then(function () {  // after drawQueue's fillShots on it
    if (b.isConnected && !b.disabled) { b.focus(); } else { toLine(); }
  });
}

$("sf-queue").addEventListener("click", function (ev) {
  var b = ev.target.closest("button"), row = b && b.closest(".sfrow");
  if (!row || b.disabled) { return; }
  var guid = Number(row.dataset.guid);
  if (b.dataset.more) { change({ action: "more", guid: guid }, "sf-status"); }
  else { change({ action: "pick", guid: guid, set: Number(row.dataset.set), index: Number(b.dataset.index) }, "sf-status"); }
});
$("sf-prev").addEventListener("click", function () { at -= 1; renderSelfies(); });
$("sf-next").addEventListener("click", function () { at += 1; renderSelfies(); });

// -- one member's Profile photo, in their detail ------------------------------------------------

export function showMemberPhoto(guid, name) {
  if (guid !== who.guid) {
    who.view = undefined;
    clear($("p-photostatus"));
  }
  who.guid = guid;
  who.name = name;
  loadMember();
}

function loadMember() {
  var seq = ++who.seq, guid = who.guid;
  api("/api/selfies/member?guid=" + num(guid)).then(function (r) {
    if (refused(r)) { return null; }
    return r.json().then(function (j) { return { status: r.status, j: j }; });
  }).then(function (x) {
    if (!x || seq !== who.seq || guid !== who.guid) { return; }
    who.view = x.status === 200 ? x.j : x.status === 404 ? null : { error: str(x.j.error) || "mind.db could not be read." };
    renderMemberPhoto();
  }, function () { /* the strip says it */ });
}

function renderMemberPhoto() {
  var j = who.view, why = whyOff(SEL), ok = live();
  var now = $("p-photonow");
  clear(now);
  now.appendChild(photoButton(who.guid, who.name, "room"));
  now.appendChild(el("span", "note", j === undefined ? "Reading." : j === null ? "No photos for this member in mind.db."
                                     : j.error ? j.error : currentText(j)));
  var has = !!j && !j.error;
  $("p-more").disabled = !ok || !has;
  var hist = $("p-photohist"), rows = has ? historyRows(j) : [];
  clear(hist);
  rows.forEach(function (r) { hist.appendChild(changeRow(r, ok, why, "p-photostatus")); });
  if (!rows.length) { hist.appendChild(el("p", "muted", has ? "No choices yet." : "-")); }
  $("p-photowhy").textContent = has ? why : "";
  $("p-photowhy").hidden = !has || !why;
}

$("p-more").addEventListener("click", function () {
  if (who.guid) { change({ action: "more", guid: who.guid }, "p-photostatus"); }
});
