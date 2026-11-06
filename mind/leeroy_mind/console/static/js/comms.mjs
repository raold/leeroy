import { $, emit, post } from "./core.mjs";
import { S, name } from "./state.mjs";
import { renderRooms } from "./comms/leftview.mjs";

// The part of Comms the rebuild has not replaced yet: the volume and mute menu of a member in the
// voice rooms (a right-click or Enter on them). The member card replaces it.

var DB_LIMIT = 12;
var menuGuid = 0;

function isMuted(g) { return !!S.voice && S.voice.mutes.indexOf(g) >= 0; }
function setVolume(guid, db) {
  db = Math.max(-DB_LIMIT, Math.min(DB_LIMIT, db));
  if (S.voice) { if (db === 0) { delete S.voice.volumes[guid]; } else { S.voice.volumes[guid] = db; } }
  post("/api/voice", { type: "volume", guid: guid, db: db }).then(function (j) {
    if (!j.ok) { emit("note", j.error || "The volume did not change."); }
  });
}
function setMute(guid, muted) {
  if (S.voice) {
    S.voice.mutes = S.voice.mutes.filter(function (g) { return g !== guid; });
    if (muted) { S.voice.mutes.push(guid); }
  }
  post("/api/voice", { type: "mute", guid: guid, on: muted }).then(function (j) {
    if (!j.ok) { emit("note", j.error || "Mute did not change."); }
  });
  renderRooms();
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
$("left").addEventListener("scroll", closeMenu);
$("tree").addEventListener("contextmenu", function (ev) {
  var row = ev.target.closest(".member");
  if (!row || row.classList.contains("you")) { closeMenu(); return; }
  ev.preventDefault();
  openMenu(Number(row.dataset.guid), ev.clientX, ev.clientY);
});
$("tree").addEventListener("keydown", function (ev) {
  var row = ev.target.closest ? ev.target.closest(".member") : null;
  if (!row || row.classList.contains("you") || (ev.key !== "Enter" && ev.key !== " ")) { return; }
  ev.preventDefault();
  var box = row.getBoundingClientRect();
  openMenu(Number(row.dataset.guid), box.left + 24, box.bottom);
});
