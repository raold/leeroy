import { $, api, arr, emit, hashKey, keyGen, on, own, refused, remember, setKey } from "./core.mjs";
import { S, adoptVoice, applyVoice } from "./state.mjs";
import { forgetFaces } from "./faces.mjs";
import { renderStrip } from "./strip.mjs";
import { renderFeed } from "./comms/feedview.mjs";
import { renderLeft } from "./comms/leftview.mjs";
import { refreshFaces, renderRight } from "./comms/rightview.mjs";
import { renderComposer } from "./comms/composer.mjs";
import "./raid.mjs";
import "./guild.mjs";
import "./personas.mjs";
import "./mind.mjs";
import { showTab } from "./tabs.mjs";

// The page's start: the key, the first drawing, the open tab and the console's event stream.
// Every tab module above registers its tab and its bus listeners as it loads.

var es = null, retry = null;

// -- the connection -------------------------------------------------------------------------------

function lost(text) {
  var e = $("s-lost");
  e.textContent = text;
  e.hidden = !text;
}
function keyLost() {
  if (es) { es.abort(); es = null; }
  clearTimeout(retry);
  lost("This console has restarted, or this page was reloaded or opened without its key: open the URL the console printed when it started.");
}
on("key-lost", keyLost);
var KINDS = { snapshot: 1, status: 1, voice: 1, chat: 1, chat_gone: 1, control: 1, raid: 1, tuning: 1 };
// One server-sent event block: "event: <kind>" and "data: <json>" lines; ": ping" is a comment
function dispatch(block) {
  var kind = "", data = "";
  block.split("\n").forEach(function (line) {
    if (line.indexOf("event: ") === 0) { kind = line.slice(7); }
    else if (line.indexOf("data: ") === 0) { data += line.slice(6); }
  });
  if (!kind || !own(KINDS, kind)) { return; }
  var d;
  try { d = JSON.parse(data); } catch (e) { return; }
  if (!d || typeof d !== "object") { return; }
  if (kind === "snapshot") {
    S.status = d.status || S.status;
    S.rooms = arr(d.rooms);
    S.mood = d.mood && typeof d.mood === "object" ? d.mood : null;
    adoptVoice(d.voice);
  }
  if (kind === "status") { S.status = d; }
  if (kind === "voice") { applyVoice(d); }
  emit(kind, d);
}
// The event stream is read with fetch, since an EventSource cannot send the key's header
function connect() {
  retry = null;
  var ctl = new AbortController();
  es = ctl;
  // A probe still on its way when a new key is pasted answers for the old key: takeKey has
  // connected again with the new one, so the probe's 403 or failure is let go
  function again() {
    if (es !== ctl) { return; }
    es = null;
    lost("The console is not reachable: retrying.");
    var gen = keyGen();
    api("/api/state").then(function (r) {
      if (gen === keyGen() && !refused(r)) { retry = setTimeout(connect, 2000); }
    }, function () { if (gen === keyGen()) { retry = setTimeout(connect, 2000); } });
  }
  api("/api/events", { headers: { "Accept": "text/event-stream" }, signal: ctl.signal })
    .then(function (r) {
      if (r.status === 403) { if (es === ctl) { es = null; refused(r); } return null; }
      if (!r.ok || !r.body) { throw new Error("HTTP " + r.status); }
      lost("");
      var reader = r.body.getReader(), text = new TextDecoder(), buf = "";
      function pump() {
        return reader.read().then(function (step) {
          if (step.done || es !== ctl) { return null; }
          buf += text.decode(step.value, { stream: true });
          var at;
          while ((at = buf.indexOf("\n\n")) >= 0) {
            dispatch(buf.slice(0, at));
            buf = buf.slice(at + 2);
          }
          return pump();
        });
      }
      return pump().then(again);
    })
    .catch(again);
}

// The key from the URL's fragment (see setKey), then out of the address bar
var first = hashKey(location.hash);
setKey(first || "");
if (first !== null) { history.replaceState(null, "", location.pathname + "#comms"); }

// A new key pasted into this open tab (the console restarted on the same port) changes only the
// fragment, so the page does not reload: take it and connect again with it. A malformed one
// leaves the key as it was.
function takeKey(key) {
  setKey(key);
  forgetFaces();  // the photos the old key was refused are asked for again
  if (es) { es.abort(); es = null; }
  clearTimeout(retry);
  lost("");
  connect();
  refreshFaces();  // the room's faces and the card's photo, with the new key
  showTab(S.tab);  // the open tab asks again at once
}

renderStrip();
renderLeft();
renderFeed(false);
renderRight();
renderComposer();
showTab((location.hash || "#comms").slice(1));
window.addEventListener("hashchange", function () {
  var key = hashKey(location.hash);
  if (key !== null) {
    history.replaceState(null, "", location.pathname + "#" + S.tab);
    if (key) { takeKey(key); }
    return;
  }
  var want = location.hash.slice(1);
  if (want !== S.tab) { showTab(want); }
});
connect();

// The light/dark switch. theme.js applied the stored choice before paint; this flips it and
// remembers it under the one key (JSON, so a stored value reads "light" or "dark" with quotes)
var THEME_KEY = "guild-console-theme";
function currentTheme() { return document.documentElement.dataset.theme === "light" ? "light" : "dark"; }
$("theme").addEventListener("click", function () {
  var next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  remember(THEME_KEY, next);
});
