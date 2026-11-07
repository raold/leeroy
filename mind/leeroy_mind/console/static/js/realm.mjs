import { $, arr, clear, el, num, obj, str } from "./core.mjs";
import { face } from "./faces.mjs";
import { notSetUp, pollTab, renderKpis } from "./tabkit.mjs";
import { firstKills, poachText, raidersText, roleWord, standing, tradeCount } from "./realmmodel.mjs";
import { when } from "./words.mjs";

// -- the realm tab: mind.db only ------------------------------------------------------------------

var REALM_MS = 60000;
var NONE = "there are no rival guilds (realm plan rivals, then approve it).";

function nameOf(v, g) { return str(obj(v.names)[String(g)]) || "#" + g; }
function dateText(sec) { return sec ? new Date(sec * 1000).toLocaleDateString() : "-"; }
function meter(pct) {
  var m = el("div", "meter"), i = el("i");
  i.style.setProperty("width", pct + "%");
  m.appendChild(i);
  return m;
}

function renderRivals(v, box) {
  clear(box);
  arr(v.rivals).forEach(function (g) {
    var p = el("article", "panel span"), head = el("header"), body = el("div");
    head.appendChild(el("h3", "", str(g.name)));
    head.appendChild(el("span", "chip", roleWord(g.role)));
    head.appendChild(el("span", "chip", str(g.faction)));
    p.appendChild(head);
    arr(g.officers).forEach(function (o) {
      var r = el("div", "srow");
      r.appendChild(face(o.guid, str(o.name), "list"));
      r.appendChild(el("span", "", str(o.name)));
      r.appendChild(el("span", "muted", str(o.place) || "somewhere"));
      body.appendChild(r);
    });
    body.appendChild(el("p", "note", num(g.gankers) + " gankers, " + num(g.applicants) + " applicants"));
    body.appendChild(el("p", "note", raidersText(g.raiders)));
    body.appendChild(el("p", "note", num(g.kills) + " boss kills"));
    p.appendChild(body);
    box.appendChild(p);
  });
}

function renderRace(v, box) {
  clear(box);
  var rivals = arr(v.rivals);
  arr(v.race).forEach(function (r) {
    var row = el("div", "srow");
    row.appendChild(el("span", "", str(r.boss_name)));
    row.appendChild(el("span", "muted", str(r.mode)));
    row.appendChild(el("span", "mono", r.guild_at ? "guild " + dateText(r.guild_at) : "guild: not yet"));
    rivals.forEach(function (g) {
      var s = standing(obj(r.rivals)[String(g.id)], dateText);
      row.appendChild(el("span", "chip", str(g.name) + ": " + s.text));
      if (s.pct !== null) { row.appendChild(meter(s.pct)); }
    });
    box.appendChild(row);
  });
  if (!box.firstChild) { box.appendChild(el("p", "note", "No boss has been raced yet.")); }
}

function renderRealm(v) {
  var now = Date.now() / 1000, rivals = arr(v.rivals), box = $("realm-rivals");
  renderKpis($("realm-kpis"), [
    { k: "Rival guilds", v: String(rivals.length), d: "" },
    { k: "Their first kills", v: String(firstKills(v.race)), d: "bosses" },
    { k: "Trade lines (7 d)", v: String(tradeCount(v.lines, now)), d: "" },
    { k: "Canon grants", v: String(arr(v.canon).length), d: "" }]);
  var lines = $("realm-lines"), canon = $("realm-canon"), poach = $("realm-poach"), race = $("realm-race");
  [lines, canon, poach, race].forEach(clear);
  if (!rivals.length) {
    notSetUp(box, NONE);
    return;
  }
  renderRivals(v, box);
  renderRace(v, race);
  poach.appendChild(el("p", "note", poachText(v.poach, function (g) { return nameOf(v, g); })));
  arr(v.lines).forEach(function (l) {
    var r = el("div", "srow");
    r.appendChild(el("span", "mono", when(l.ts)));
    r.appendChild(el("span", "chip", str(l.guild)));
    r.appendChild(el("span", "who", str(l.name)));
    r.appendChild(el("span", "", str(l.text)));
    lines.appendChild(r);
  });
  if (!lines.firstChild) { lines.appendChild(el("p", "note", "No rival has spoken in Trade yet.")); }
  arr(v.canon).forEach(function (c) {
    var r = el("div", "srow");
    r.appendChild(el("span", "", str(c.name)));
    r.appendChild(el("span", "who", nameOf(v, c.guid)));
    r.appendChild(el("span", "mono", when(c.ts)));
    canon.appendChild(r);
  });
  if (!canon.firstChild) { canon.appendChild(el("p", "note", "No canon achievement has been granted.")); }
}

pollTab("realm", { path: "/api/realm", ms: REALM_MS, render: renderRealm });
