import { $, api, emit, on, own, remember, setKey } from "./core.mjs";
import { S } from "./state.mjs";
import { renderStrip } from "./strip.mjs";
import { renderComms, renderFeed } from "./comms.mjs";
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
var KINDS = { snapshot: 1, status: 1, voice: 1, chat: 1, control: 1, raid: 1, tuning: 1 };
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
  if (kind === "snapshot") { S.status = d.status || S.status; }
  if (kind === "status") { S.status = d; }
  emit(kind, d);
}
// The event stream is read with fetch, since an EventSource cannot send the key's header
function connect() {
  retry = null;
  var ctl = new AbortController();
  es = ctl;
  function again() {
    if (es !== ctl) { return; }
    es = null;
    lost("The console is not reachable: retrying.");
    api("/api/state").then(function (r) {
      if (r.status === 403) { emit("key-lost"); } else { retry = setTimeout(connect, 2000); }
    }, function () { retry = setTimeout(connect, 2000); });
  }
  api("/api/events", { headers: { "Accept": "text/event-stream" }, signal: ctl.signal })
    .then(function (r) {
      if (r.status === 403) { if (es === ctl) { es = null; emit("key-lost"); } return null; }
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
var m = /^#key=([A-Za-z0-9_-]{16,128})$/.exec(location.hash);
setKey(m ? m[1] : "");
if (/^#key=/.test(location.hash)) { history.replaceState(null, "", location.pathname + "#comms"); }

renderStrip();
renderComms();
renderFeed(false);
showTab((location.hash || "#comms").slice(1));
window.addEventListener("hashchange", function () {
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
