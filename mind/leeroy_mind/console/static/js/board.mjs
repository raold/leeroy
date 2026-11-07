import { $, arr, clear, el, obj, str } from "./core.mjs";
import { face } from "./faces.mjs";
import { notSetUp, pollTab, renderKpis } from "./tabkit.mjs";
import { NOTHING, REALM_OFF, boardEmpty, itemText, minutes, postCount, qualityClass } from "./boardmodel.mjs";
import { when } from "./words.mjs";

// -- the board tab: mind.db only, read-only ---------------------------------------------------------

var BOARD_MS = 30000;

function nameOf(v, g) { return str(obj(v.names)[String(g)]) || "#" + g; }

function whoLine(guid, name) {
  var w = el("span", "who");
  w.appendChild(face(guid, name, "list"));
  w.appendChild(el("span", "", name));
  return w;
}

function renderBoard(v) {
  var forum = arr(v.forum), letters = arr(v.letters), writeups = arr(v.writeups);
  renderKpis($("board-kpis"), [
    { k: "Forum posts", v: String(postCount(forum)), d: forum.length + " threads" },
    { k: "Letters", v: String(letters.length), d: "delivered to you" },
    { k: "Write-ups", v: String(writeups.length), d: "of the Realm's events" }]);
  var note = $("board-note"), fbox = $("board-forum"), lbox = $("board-letters"), wbox = $("board-writeups");
  var empty = $("board-empty"), panels = $("board-panels");
  note.hidden = v.realm_on !== false;
  note.textContent = v.realm_on === false ? REALM_OFF : "";
  clear(fbox);
  clear(lbox);
  clear(wbox);
  if (boardEmpty(v)) {
    panels.hidden = true;
    empty.hidden = false;
    notSetUp(empty, NOTHING);
    return;
  }
  panels.hidden = false;
  empty.hidden = true;
  forum.forEach(function (t) {
    var box = el("div", "thread");
    box.appendChild(el("h4", "", str(t.thread)));
    arr(t.posts).forEach(function (p) {
      var r = el("div", "srow");
      r.appendChild(whoLine(p.author_kind === "member" ? p.author : 0, str(p.name)));
      if (p.guild) { r.appendChild(el("span", "chip", str(p.guild))); }
      r.appendChild(el("span", "muted", when(p.ts)));
      r.appendChild(el("p", "text", str(p.text)));
      box.appendChild(r);
    });
    fbox.appendChild(box);
  });
  if (!forum.length) { fbox.appendChild(el("p", "note", "No forum posts yet.")); }
  letters.forEach(function (m) {
    var r = el("div", "srow letter");
    r.appendChild(whoLine(m.sender, nameOf(v, m.sender)));
    r.appendChild(el("strong", "", str(m.subject)));
    r.appendChild(el("span", "muted", when(m.ts)));
    if (m.state === "taken") { r.appendChild(el("span", "chip ok", "taken")); }
    r.appendChild(el("p", "text", str(m.body)));
    arr(m.items).forEach(function (it) {
      r.appendChild(el("span", "chip item " + qualityClass(obj(it).quality), itemText(it)));
    });
    lbox.appendChild(r);
  });
  if (!letters.length) { lbox.appendChild(el("p", "note", "No letter has reached you yet.")); }
  writeups.forEach(function (w) {
    var r = el("div", "srow");
    r.appendChild(el("span", "chip", str(w.kind)));
    r.appendChild(el("span", "", str(w.place)));
    r.appendChild(whoLine(w.organiser, nameOf(v, w.organiser)));
    r.appendChild(el("span", "muted", when(w.start) + ", " + minutes(w) + " min"));
    r.appendChild(el("p", "text", str(w.text)));
    wbox.appendChild(r);
  });
  if (!writeups.length) { wbox.appendChild(el("p", "note", "No write-up yet.")); }
}

pollTab("board", { path: "/api/board", ms: BOARD_MS, render: renderBoard });
