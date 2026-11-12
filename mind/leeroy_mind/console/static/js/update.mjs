// A tab left open across a console update keeps running the page it loaded: the server's files
// change, this page's code does not. The event stream's snapshot names the server's build; when it
// is not this page's own (its <meta name="console-build">), the page reloads into the new one,
// keeping its key, once no message is half-typed. Until then a note says why it waits.
import { currentKey } from "./core.mjs";

var MARK = "__CONSOLE_BUILD__";  // a page served without its build stamped never reloads itself
var WAIT_MS = 5000;
var pending = false;

export function pageBuild(doc) {
  var m = doc.querySelector('meta[name="console-build"]');
  return m ? String(m.getAttribute("content") || "") : "";
}

// True when the server runs another build than the one this page came from
export function stale(own, server) {
  return own !== "" && own !== MARK && typeof server === "string" && /^[0-9a-f]{16}$/.test(server)
    && server !== own;
}

// A message being written: the WoW reply box, or any text area (Discord, Board, nudges)
export function hasDraft(doc) {
  var fields = doc.querySelectorAll("#say-text, textarea");
  for (var i = 0; i < fields.length; i++) {
    if (String(fields[i].value || "").trim() !== "") { return true; }
  }
  return false;
}

// Where a reload goes: the key back in the fragment, which the page takes out again at once
export function reloadTarget(path, key) {
  return path + "#key=" + key;
}

function note(text) {
  var p = document.getElementById("update-note");
  if (!p) {
    p = document.createElement("p");
    p.id = "update-note";
    p.className = "update-note";
    p.setAttribute("role", "status");
    document.body.appendChild(p);
  }
  p.textContent = text;
}

// The snapshot's build: reload into it, or wait for the draft to go
export function serverBuild(build) {
  if (pending || !stale(pageBuild(document), build)) { return; }
  pending = true;
  (function attempt() {
    var key = currentKey();
    if (!key) {
      note("The console was updated: open it again from your bookmark to load the new version.");
      return;
    }
    if (hasDraft(document)) {
      note("The console was updated: this page reloads into the new version once your message is sent or cleared.");
      setTimeout(attempt, WAIT_MS);
      return;
    }
    history.replaceState(null, "", reloadTarget(location.pathname, key));
    location.reload();
  })();
}
