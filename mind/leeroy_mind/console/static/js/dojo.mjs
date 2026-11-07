import { $, arr, clear, el, num, obj, str } from "./core.mjs";
import { notSetUp, pollTab, renderKpis } from "./tabkit.mjs";
import { buildFacts, buildKpi, episodeText, episodesKpi, gateChip, gateKpi, movedChip, okText, problemsText,
         realmKpi, shortHead, stampText } from "./dojomodel.mjs";

// -- the dojo tab: the dojo's own files, read-only -------------------------------------------------

var DOJO_MS = 15000;
var BOXES = ["dojo-realm", "dojo-build", "dojo-baselines", "dojo-gates", "dojo-runs", "dojo-scenario", "dojo-g1md", "dojo-later"];

function row() { return el("div", "srow"); }
function chip(text, cls) { return el("span", "chip" + (cls ? " " + cls : ""), text); }
function mono(text) { return el("code", "mono", text); }
function empty(box, text) { if (!box.firstChild) { box.appendChild(el("p", "note", text)); } }
function replayCls(v) { return v === true ? "ok" : v === false ? "bad" : ""; }

function factList(pairs) {
  var dl = el("dl", "kv");
  pairs.forEach(function (p) {
    dl.appendChild(el("dt", "", p[0]));
    dl.appendChild(el("dd", "", p[1]));
  });
  return dl;
}

function renderDojo(v) {
  if (!v.setup) {
    renderKpis($("dojo-kpis"), []);
    BOXES.forEach(function (id) { clear($(id)); });
    notSetUp($("dojo-realm"), str(v.why));
    $("dojo-title").textContent = "No dojo set up";
    return;
  }
  $("dojo-title").textContent = "Dojo";
  var realm = obj(v.realm), rk = realmKpi(realm), bk = buildKpi(v.build), gk = gateKpi(v.gates), ek = episodesKpi(v.runs);
  renderKpis($("dojo-kpis"), [
    { k: "Training realm", v: rk.v, d: rk.d }, { k: "Build", v: bk.v, d: bk.d },
    { k: "G1", v: gk.v, d: gk.d }, { k: "Episodes", v: ek.v, d: ek.d }]);

  var rb = $("dojo-realm");
  clear(rb);
  var head = row();
  head.appendChild(el("i", "dot" + (rk.cls ? " " + rk.cls : "")));
  head.appendChild(el("strong", "", rk.v));
  head.appendChild(el("span", "muted", str(v.instance)));
  rb.appendChild(head);
  var ep = obj(realm.endpoint);
  var facts = [["worldserver pid", realm.pid ? String(num(realm.pid)) : "-"], ["world port", String(num(realm.port))]];
  if (realm.endpoint) {
    facts.push(["endpoint port", ep.port ? String(num(ep.port)) : "-"], ["endpoint", ep.up ? "listening" : "not listening"],
               ["scenario", str(ep.scenario) || "-"]);
  } else { facts.push(["endpoint", "no receipt"]); }
  rb.appendChild(factList(facts));

  var bb = $("dojo-build");
  clear(bb);
  if (!v.build) { bb.appendChild(el("p", "note", "No build receipt.")); }
  else if (str(v.build.why)) { bb.appendChild(el("p", "note", str(v.build.why))); }
  else {
    bb.appendChild(factList(buildFacts(v.build)));
    var pt = problemsText(v.build);
    if (pt) { bb.appendChild(el("p", "note", pt)); }
  }

  var bl = $("dojo-baselines");
  clear(bl);
  arr(obj(v.provenance).baselines).forEach(function (b) {
    var r = row(), c = movedChip(b.moved);
    r.appendChild(el("strong", "", str(b.name)));
    r.appendChild(mono(str(b.path)));
    r.appendChild(el("span", "muted", "recorded " + (shortHead(b.head) || "-") + " · now " + (shortHead(b.now) || "-")));
    r.appendChild(chip(c.text, c.cls));
    bl.appendChild(r);
  });
  empty(bl, v.provenance ? "The manifest records no baselines." : "No provenance manifest could be read.");

  var gb = $("dojo-gates");
  clear(gb);
  arr(v.gates).forEach(function (g) {
    var r = row();
    r.appendChild(el("strong", "", str(g.gate)));
    r.appendChild(chip(str(g.state), gateChip(g.state)));
    r.appendChild(el("span", "muted", g.run ? stampText(g.run) : ""));
    r.appendChild(el("span", "", str(g.detail)));
    gb.appendChild(r);
  });

  var rr = $("dojo-runs");
  clear(rr);
  arr(v.runs).forEach(function (run) {
    var eps = arr(run.episodes), line = row();
    line.appendChild(el("strong", "", stampText(run.stamp)));
    line.appendChild(el("span", "", str(run.kind)));
    line.appendChild(chip(okText(run.ok), replayCls(run.ok)));
    line.appendChild(el("span", "muted", str(run.why)));
    if (!eps.length) { rr.appendChild(line); return; }
    var d = el("details", "dojo-run"), s = el("summary", "");
    s.appendChild(line);
    d.appendChild(s);
    eps.forEach(function (e) {
      var er = row();
      er.appendChild(el("strong", "", str(e.name)));
      er.appendChild(el("span", "", episodeText(e)));
      er.appendChild(chip("replay " + okText(e.replay_ok), replayCls(e.replay_ok)));
      er.appendChild(chip(e.usable === true ? "usable" : e.usable === false ? "not usable" : "stock", ""));
      d.appendChild(er);
    });
    rr.appendChild(d);
  });
  empty(rr, "No runs recorded.");

  var sb = $("dojo-scenario");
  clear(sb);
  if (v.scenario) {
    var sd = factList([["id", str(v.scenario.id) || "-"], ["pinned hash", str(v.scenario.sha256) || "-"]]);
    sd.className = "kv mono";
    sb.appendChild(sd);
  } else { sb.appendChild(el("p", "note", "No G1 report names a scenario.")); }

  var mb = $("dojo-g1md");
  clear(mb);
  if (typeof v.g1_md === "string") {
    var d2 = el("details", "");
    d2.appendChild(el("summary", "", "G1.md"));
    d2.appendChild(el("pre", "dojo-pre", v.g1_md));
    mb.appendChild(d2);
  } else { mb.appendChild(el("p", "note", str(v.g1_md_why) || "No G1 run.")); }

  var lb = $("dojo-later"), later = obj(v.later);
  clear(lb);
  [["Learned policies", later.policies], ["Learning curves", later.curves], ["Live-trial adapter", later.live_adapter]].forEach(function (x) {
    var r = row();
    r.appendChild(el("strong", "", x[0]));
    r.appendChild(el("span", "muted", str(x[1]) || "not yet"));
    lb.appendChild(r);
  });
}

pollTab("dojo", { path: "/api/dojo", ms: DOJO_MS, render: renderDojo });
