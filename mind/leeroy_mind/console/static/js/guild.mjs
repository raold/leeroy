import { $, api, arr, cell, clear, el, emit, emptyRow, num, obj, plural, setText, str } from "./core.mjs";
import { S, roomOf } from "./state.mjs";
import { registerTab } from "./tabs.mjs";
import { CLASSES, dkp, mode, when, who } from "./words.mjs";

// -- the guild tab: mind.db only ------------------------------------------------------------------

var GUILD_MS = 30000;
var guildTimer = null, guildBusy = false;

function renderGuild(g) {
  var view = g, stats = obj(g.stats), roster = arr(g.roster);
  $("guild-banner").hidden = true;
  setText("g-count", plural(roster.length, "member") + ", " + roster.filter(function (p) { return p.online; }).length + " online");
  setText("g-stats", plural(num(stats.raids), "raid") + " done (" + num(stats.offline) + " offline)"
          + (stats.first ? " since " + when(stats.first) : "") + " · " + plural(num(stats.kills), "kill") + ", "
          + plural(num(stats.wipes), "wipe") + " · " + plural(num(stats.items), "item") + " awarded · DKP "
          + dkp(num(stats.awarded)) + " earned, " + dkp(num(stats.spent)) + " spent, " + dkp(num(stats.decayed)) + " decayed");
  var body = $("g-roster");
  clear(body);
  roster.forEach(function (p) {
    var tr = el("tr", p.online ? "" : "off");
    cell(tr, str(p.name) + (p.guid === g.human ? " (you)" : ""));
    cell(tr, CLASSES[p["class"]] || "-");
    cell(tr, str(p.spec) || "-");
    cell(tr, str(p.role) || "-", "role-" + str(p.role));
    cell(tr, String(num(p.level)), "num");
    cell(tr, String(num(p.rank)), "num");
    cell(tr, str(p.guild_role) + (p.officer_title ? " (" + p.officer_title + ")" : "") || "-");
    cell(tr, p.online ? "online" : "offline");
    cell(tr, roomOf(p.guid) || "-");
    body.appendChild(tr);
  });
  if (!roster.length) { emptyRow(body, 9, "The mind has no roster yet."); }
  body = $("g-ladder");
  clear(body);
  arr(g.ladder).forEach(function (e, i) {
    var tr = el("tr");
    cell(tr, String(i + 1), "num");
    cell(tr, who(view, e.guid));
    [e.balance, e.earned, e.spent, e.decay].forEach(function (v) { cell(tr, dkp(v), "num"); });
    [e.items, e.raids, e.benched].forEach(function (v) { cell(tr, String(num(v)), "num"); });
    body.appendChild(tr);
  });
  if (!arr(g.ladder).length) { emptyRow(body, 9, "No DKP has been awarded yet."); }
  body = $("g-loot");
  clear(body);
  arr(g.loot).forEach(function (l) {
    var tr = el("tr");
    cell(tr, when(l.ts));
    cell(tr, str(l.item) || ("item " + (l.item_id || "?")));
    cell(tr, who(view, l.guid));
    cell(tr, dkp(l.paid), "num");
    cell(tr, l.raid ? str(l.raid).toUpperCase() + mode(l.size, l.heroic) : "-");
    body.appendChild(tr);
  });
  if (!arr(g.loot).length) { emptyRow(body, 5, "No item has been awarded yet."); }
  var box = $("g-awards"), seasons = {}, order = [];
  clear(box);
  arr(g.awards).forEach(function (a) {
    if (!seasons[a.season]) { seasons[a.season] = []; order.push(a.season); }
    seasons[a.season].push(a);
  });
  order.forEach(function (s) {
    var sec = el("div", "season");
    sec.appendChild(el("h4", "", "Season " + s));
    seasons[s].forEach(function (a) {
      sec.appendChild(el("div", "", str(a.title) + ": " + who(view, a.guid) + " (" + num(a.value) + ")"));
    });
    box.appendChild(sec);
  });
  if (!order.length) { box.appendChild(el("div", "muted", "No season has ended yet.")); }
  box = $("g-chronicle");
  clear(box);
  arr(g.chronicle).forEach(function (c) {
    var e = el("div", "entry");
    e.appendChild(el("div", "meta", str(c.week) + " · " + str(c.era) + " · by " + who(view, c.author)));
    e.appendChild(el("p", "", str(c.text)));
    box.appendChild(e);
  });
  if (!arr(g.chronicle).length) { box.appendChild(el("div", "muted", "The chronicle is empty.")); }
}
function pollGuild() {
  if (guildBusy || S.tab !== "guild") { return; }
  guildBusy = true;
  api("/api/guild").then(function (r) {
    if (r.status === 403) { emit("key-lost"); return null; }
    return r.json().then(function (j) { return { status: r.status, j: j }; });
  }).then(function (x) {
    if (!x) { return; }
    if (x.status === 200) { renderGuild(x.j); }
    else { $("guild-banner").textContent = str(x.j.error) || "mind.db could not be read."; $("guild-banner").hidden = false; }
  }, function () { /* the strip says it */ })
    .then(function () { guildBusy = false; });
}

registerTab("guild", {
  show: function () { pollGuild(); guildTimer = setInterval(pollGuild, GUILD_MS); },
  hide: function () { clearInterval(guildTimer); guildTimer = null; }
});
