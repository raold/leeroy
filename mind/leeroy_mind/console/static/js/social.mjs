import { $, arr, clear, el, num, obj, plural, str } from "./core.mjs";
import { face } from "./faces.mjs";
import { notSetUp, pollTab, renderKpis } from "./tabkit.mjs";
import { feelings, feudText, romanceText, stageText } from "./socialmodel.mjs";
import { signed, when } from "./words.mjs";

// -- the social tab: mind.db only -----------------------------------------------------------------

var SOCIAL_MS = 30000;
var GONE = "the night's stages have not written this.";

function nameOf(v, g) { return str(obj(v.names)[String(g)]) || "#" + g; }
function person(v, g, size) {
  var box = el("span", "who");
  box.appendChild(face(g, nameOf(v, g), size || "list"));
  box.appendChild(el("span", "", nameOf(v, g)));
  return box;
}
function row(cls) { return el("div", "srow" + (cls ? " " + cls : "")); }
function section(id, tables, v, fill) {
  var box = $(id), gone = tables.filter(function (t) { return arr(v.missing).indexOf(t) >= 0; });
  clear(box);
  if (gone.length) { notSetUp(box, GONE); return; }
  fill(box);
}
function empty(box, text) { if (!box.firstChild) { box.appendChild(el("p", "note", text)); } }
function meter(level) {
  var m = el("div", "meter"), i = el("i");
  i.style.setProperty("width", Math.round(Math.max(0, Math.min(1, num(level))) * 100) + "%");
  m.appendChild(i);
  return m;
}

function renderSocial(v) {
  var mood = v.mood ? obj(v.mood) : null, reasons = mood ? arr(mood.reasons) : [];
  renderKpis($("social-kpis"), [
    { k: "Guild mood", v: mood ? signed(num(mood.value), 2) : "-", d: mood ? str(reasons[0]) || "no reason given" : "not recorded yet" },
    { k: "Open feuds", v: String(arr(v.feuds).length), d: "" },
    { k: "Romances", v: String(arr(v.romances).length), d: "between members" },
    { k: "Live rumours", v: String(arr(v.rumours).length), d: "" }]);
  var now = Date.now() / 1000;
  section("social-you", ["relationship", "attraction_state"], v, function (box) {
    arr(v.you).forEach(function (y) {
      var r = row();
      r.appendChild(person(v, y.guid));
      if (y.stance) { r.appendChild(el("span", "chip", y.stance)); }
      feelings(y, now).forEach(function (f) {
        r.appendChild(el("span", "chip" + (f.kind === "hurt" ? " warn" : ""), f.text + (f.at ? " " + when(f.at) : "")));
      });
      if (stageText(y.romance_stage)) { r.appendChild(el("span", "chip", y.romance_stage)); }
      r.appendChild(el("span", "muted", "affinity " + signed(num(y.affinity), 0) + " · attraction " + Math.round(num(y.attraction))));
      box.appendChild(r);
    });
    empty(box, "Nobody has a feeling for you yet.");
  });
  section("social-feuds", ["feud"], v, function (box) {
    arr(v.feuds).forEach(function (f) {
      var r = row();
      r.appendChild(person(v, f.a));
      r.appendChild(el("span", "muted", "vs"));
      r.appendChild(person(v, f.b));
      r.appendChild(el("span", "muted", "since " + when(f.started)));
      r.appendChild(el("span", "", str(f.cause)));
      r.title = feudText(f, v.names);
      box.appendChild(r);
    });
    empty(box, "No feud is open.");
  });
  section("social-jealousy", ["jealousy"], v, function (box) {
    arr(v.jealousy).forEach(function (j) {
      var r = row();
      r.appendChild(person(v, j.holder));
      r.appendChild(el("span", "muted", "→"));
      r.appendChild(person(v, j.target));
      r.appendChild(el("span", "chip", str(j.kind)));
      r.appendChild(meter(j.level));
      box.appendChild(r);
    });
    empty(box, "Nobody is jealous.");
  });
  section("social-romances", ["relationship"], v, function (box) {
    arr(v.romances).forEach(function (p) {
      var r = row();
      r.appendChild(person(v, p.a));
      r.appendChild(el("span", "muted", "&"));
      r.appendChild(person(v, p.b));
      r.appendChild(el("span", "muted", romanceText(p, nameOf(v, p.a), nameOf(v, p.b))));
      box.appendChild(r);
    });
    empty(box, "No romance between members.");
  });
  section("social-rumours", ["rumor", "rumor_state", "knows"], v, function (box) {
    arr(v.rumours).forEach(function (m) {
      var r = el("div", "srow rumour");
      var head = el("div", "who");
      head.appendChild(el("span", "muted", "about"));
      head.appendChild(person(v, m.about));
      r.appendChild(head);
      r.appendChild(el("p", "", str(m.text)));
      var foot = el("div", "who");
      arr(m.spreaders).forEach(function (g) { foot.appendChild(face(g, nameOf(v, g), "list")); });
      if (arr(m.stiflers).length) { foot.appendChild(el("span", "muted", "stifled by")); }
      arr(m.stiflers).forEach(function (g) { foot.appendChild(face(g, nameOf(v, g), "list")); });
      foot.appendChild(el("span", "muted", plural(num(m.knowers), "member") + " know · last told " + when(m.last_told)));
      r.appendChild(foot);
      box.appendChild(r);
    });
    empty(box, "No rumour is going round.");
  });
}

pollTab("social", { path: "/api/social", ms: SOCIAL_MS, render: renderSocial });
