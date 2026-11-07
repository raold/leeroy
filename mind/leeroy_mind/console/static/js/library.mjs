import { $, api, arr, clear, el, obj, refused, str } from "./core.mjs";
import { banner } from "./tabkit.mjs";
import { registerTab } from "./tabs.mjs";
import { KINDS, answerNote, cleanQuery, isPre, queryProblem, searchPath, statsLine, wantsDeeper } from "./librarymodel.mjs";

// -- the library tab: a search of library.db, on submit only (no poll) -----------------------------

var busy = false, asked = false;   // asked: an answer is on show, so "Search deeper" has a question

function say(text, bad) {
  var n = $("library-status");
  n.textContent = text;
  n.className = bad ? "why bad" : "note";
}

function drawHits(hits) {
  var box = $("library-hits");
  clear(box);
  arr(hits).forEach(function (h) {
    h = obj(h);
    var p = el("article", "panel lib-hit"), head = el("header");
    head.appendChild(el("span", "chip", str(h.kind)));
    head.appendChild(el("h3", "", str(h.title) + (h.section ? " - " + str(h.section) : "")));
    p.appendChild(head);
    p.appendChild(el(isPre(h.kind) ? "pre" : "p", "lib-text", str(h.text)));
    p.appendChild(el("p", "hint", str(h.cite)));
    box.appendChild(p);
  });
}

function show(view, deeper) {
  if (view.stats) { $("library-stats").textContent = statsLine(view.stats); }
  drawHits(view.hits);
  var note = answerNote(view, deeper);
  say(note.text, note.bad);
  $("library-deeper").hidden = view.setup !== true || !asked;
}

function ask(q, kind, deeper) {
  return api(searchPath(q, kind, deeper)).then(function (r) {
    if (refused(r)) { return null; }
    return r.json().then(function (j) { return { status: r.status, j: j }; },
                         function () { return { status: r.status, j: {} }; });
  }).then(function (x) {
    if (!x) { return null; }
    if (x.status === 200) { banner("library", null); return x.j; }
    if (x.status === 503) { banner("library", str(x.j.error) || "library.db could not be read just now."); }
    else { say(str(x.j.error) || "HTTP " + x.status, true); }
    return null;
  });
}

function run(deeper) {
  if (busy) { return; }
  var text = $("library-q").value, kind = $("library-kind").value;
  var problem = queryProblem(text);
  if (problem) { say(problem, true); return; }
  var q = cleanQuery(text);
  busy = true;
  $("library-go").disabled = $("library-deeper").disabled = true;
  say(deeper ? "Asking the mind..." : "Searching...", false);
  ask(q, kind, deeper).then(function (view) {
    if (!view) { return null; }
    asked = true;
    show(view, deeper);
    if (wantsDeeper(view, deeper)) {
      say("Nothing in the text. Asking the mind...", false);
      return ask(q, kind, true).then(function (v2) {
        if (!v2) { return; }
        if (arr(v2.hits).length || v2.deeper_why) { show(v2, true); }
        else { show(view, false); }
      });
    }
    return null;
  }).catch(function () { say("The console did not answer.", true); })
    .then(function () { busy = false; $("library-go").disabled = $("library-deeper").disabled = false; });
}

function loadStats() {
  api("/api/library").then(function (r) {
    if (refused(r) || r.status !== 200) { return null; }
    return r.json();
  }).then(function (v) {
    if (!v) { return; }
    if (v.setup === false) { say("Not set up yet: " + str(v.why), false); return; }
    $("library-stats").textContent = statsLine(v.stats);
  }).catch(function () { /* the strip says it */ });
}

(function init() {
  var sel = $("library-kind");
  sel.appendChild(el("option", "", "Everything")).value = "";
  KINDS.forEach(function (k) { sel.appendChild(el("option", "", k)).value = k; });
  $("library-form").addEventListener("submit", function (ev) { ev.preventDefault(); run(false); });
  $("library-deeper").addEventListener("click", function () { run(true); });
  registerTab("library", { show: function () { if (!$("library-stats").textContent) { loadStats(); } } });
})();
