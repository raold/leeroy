import { $, arr, clear, el, num, obj, plural, str } from "./core.mjs";
import { notSetUp, pollTab, renderKpis } from "./tabkit.mjs";
import { adaptText, avoidText, changeText, coverageText, deathsBy, deathsText, evidenceNotes, gateText, hitHardest, hurtHead,
  mechanicsText, scopeText, trialText } from "./trainmodel.mjs";
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
  var ids = ["training-week", "training-hurt", "training-trials", "training-cards", "training-done", "training-evidence"];
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

  renderHurt($("training-hurt"), obj(v.hurt));

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

// What hurt them: per boss, deaths by killing spell, the spells that hit hardest, failed mechanics
// and avoidable hits, from the stored pulls. Numbers and spell names only.
function renderHurt(box, h) {
  clear(box);
  arr(h.contexts).forEach(function (c) {
    box.appendChild(el("h4", "", scopeText(c)));
    box.appendChild(el("p", "note", hurtHead(c)));
    [["Deaths by", deathsBy(c)], ["Hit hardest by", hitHardest(c)], ["Failed mechanics", mechanicsText(c)]]
      .concat(avoidText(c).map(function (t) { return ["Avoidable", t]; }))
      .forEach(function (pair) {
        if (!pair[1]) { return; }
        var r = row();
        r.appendChild(el("span", "chip", pair[0]));
        r.appendChild(el("span", "", pair[1]));
        box.appendChild(r);
      });
  });
  empty(box, "No boss pull has been stored yet.");
  if (arr(h.contexts).length) {
    var ev = obj(h.evidence);
    ["damage_taken", "avoidable"].forEach(function (k) { if (str(ev[k])) { box.appendChild(el("p", "muted", str(ev[k]))); } });
  }
}

pollTab("training", { path: "/api/training", ms: TRAINING_MS, render: renderTraining });
