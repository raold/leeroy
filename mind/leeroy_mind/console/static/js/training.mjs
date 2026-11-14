import { $, api, arr, clear, el, num, obj, plural, refused, str } from "./core.mjs";
import { notSetUp, pollTab, renderKpis } from "./tabkit.mjs";
import { adaptText, changeText, coverageText, deathsText, evidenceNotes, gateText, playbookGroups, playbookTotals, scopeText, trialText } from "./trainmodel.mjs";
import { when } from "./words.mjs";

// -- the training tab: mind.db only, read-only -----------------------------------------------------

var TRAINING_MS = 60000;

function row(cls) { return el("div", "srow" + (cls ? " " + cls : "")); }
function empty(box, text) { if (!box.firstChild) { box.appendChild(el("p", "note", text)); } }
function kinds(o) {
  var parts = Object.keys(obj(o)).sort().map(function (k) { return k + " " + num(o[k]); });
  return parts.length ? parts.join(" · ") : "none";
}

function renderTraining(v) {
  var ids = ["training-week", "training-trials", "training-cards", "training-done", "training-evidence"];
  if (!v.setup) {
    renderKpis($("training-kpis"), []);
    ids.forEach(function (id) { notSetUp($(id), str(v.why)); });
    return;
  }
  var pulls = obj(v.pulls), adapt = adaptText(v), gate = gateText(v.gate);
  renderKpis($("training-kpis"), [
    { k: "Fights recorded", v: String(num(pulls.total)), d: kinds(pulls.by_kind) },
    { k: "Days until tactics may adapt", v: adapt.v, d: adapt.d },
    { k: "Gate", v: gate.v, d: gate.d },
    { k: "Trials open", v: String(num(v.trials_open)), d: "" }]);

  var week = $("training-week");
  clear(week);
  week.appendChild(el("p", "note", str(v.week) + " · pulls this week: " + kinds(obj(pulls).week_by_kind)));
  arr(v.contexts).forEach(function (c) {
    var r = row();
    r.appendChild(el("strong", "", str(c.boss) + (c.size ? " " + num(c.size) : "")));
    r.appendChild(el("span", "", plural(num(c.pulls), "pull") + " · " + plural(num(c.kills), "kill") + " · " + plural(num(c.wipes), "wipe")));
    r.appendChild(el("span", "", deathsText(c) + " deaths a pull"));
    r.appendChild(el("span", "chip", changeText(c)));
    r.appendChild(el("span", "muted", coverageText(c)));
    week.appendChild(r);
  });
  empty(week, "No scored fights this week.");

  var trials = $("training-trials");
  clear(trials);
  arr(v.trials).forEach(function (t) {
    var r = row();
    r.appendChild(el("strong", "", scopeText(t)));
    r.appendChild(el("span", "chip", str(t.status)));
    r.appendChild(el("span", "muted", "opened " + when(t.opened)));
    r.appendChild(el("span", "", trialText(t)));
    trials.appendChild(r);
  });
  empty(trials, "No trial has been opened.");

  var cards = $("training-cards"), tree = obj(v.cards);
  clear(cards);
  Object.keys(tree).forEach(function (ctx) {
    var g = obj(tree[ctx]);
    cards.appendChild(el("h4", "", scopeText(g)));
    arr(g.cards).forEach(function (c) {
      var r = row();
      r.appendChild(el("span", "", "v" + num(c.version)));
      r.appendChild(el("span", "chip", str(c.status)));
      r.appendChild(el("span", "muted", str(c.origin)));
      r.appendChild(el("strong", "", str(c.call)));
      r.appendChild(el("span", "", str(c.reason)));
      cards.appendChild(r);
    });
  });
  empty(cards, "No tactic card has been written.");

  var done = $("training-done");
  clear(done);
  arr(v.checklist).forEach(function (c) {
    var r = row();
    r.appendChild(el("span", "chip" + (c.done ? "" : " warn"), c.done ? "done" : "open"));
    r.appendChild(el("span", "", str(c.label)));
    if (c.waiting) { r.appendChild(el("span", "muted", "waiting for recording")); }
    done.appendChild(r);
  });

  var ev = $("training-evidence");
  clear(ev);
  evidenceNotes(v.evidence).forEach(function (t) { ev.appendChild(el("p", "note", t)); });
  empty(ev, "The report states no limits.");
}

// The raid playbooks are the repo's data: read once a page, kept while it stays open
var playbooks = null;

function renderPlaybooks(v) {
  var box = $("training-playbooks");
  clear(box);
  box.appendChild(el("p", "note", playbookTotals(v)));
  playbookGroups(v).forEach(function (raid) {
    box.appendChild(el("h4", "", raid.title));
    raid.bosses.forEach(function (b) {
      var d = el("details", ""), s = el("summary", "");
      s.appendChild(el("strong", "", b.title));
      s.appendChild(el("span", "muted", " " + b.detail));
      d.appendChild(s);
      if (b.call) { d.appendChild(el("p", "note", "Call: " + b.call)); }
      b.mechanics.forEach(function (m) {
        var r = row();
        r.appendChild(el("strong", "", m.title));
        r.appendChild(el("span", "chip", m.tag));
        r.appendChild(el("span", "muted", m.who));
        if (m.stock) { r.appendChild(el("span", "chip", m.stock)); }
        if (m.note) { r.appendChild(el("span", "muted", m.note)); }
        r.appendChild(el("span", "", m.text));
        d.appendChild(r);
      });
      box.appendChild(d);
    });
  });
}

function loadPlaybooks() {
  if (playbooks) { return; }
  playbooks = api("/api/training/playbooks", { poll: true }).then(function (r) {
    if (refused(r) || r.status !== 200) { playbooks = null; return null; }
    return r.json().then(renderPlaybooks);
  }).catch(function () { playbooks = null; });
}

pollTab("training", { path: "/api/training", ms: TRAINING_MS, render: function (v) { renderTraining(v); loadPlaybooks(); } });
