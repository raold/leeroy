import { $, api, arr, cell, clear, el, emptyRow, num, obj, plural, refused, setText, str } from "./core.mjs";
import { nameCell } from "./faces.mjs";
import { photoButton } from "./gallery.mjs";
import { S, roomOf } from "./state.mjs";
import { registerTab } from "./tabs.mjs";
import { pollDecisions } from "./decisions.mjs";
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
    nameCell(tr, p.guid, str(p.name), { you: p.guid === g.human, text: str(p.name) + (p.guid === g.human ? " (you)" : ""),
      build: p.guid === g.human ? null : function (guid, name) { return photoButton(guid, name, "list"); } });
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
    nameCell(tr, e.guid, who(view, e.guid), { you: e.guid === g.human });
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
    nameCell(tr, l.guid, who(view, l.guid), { you: l.guid === g.human });
    cell(tr, dkp(l.paid), "num");
    cell(tr, l.raid ? str(l.raid).toUpperCase() + mode(l.size, l.heroic) : "-");
    body.appendChild(tr);
  });
  if (!arr(g.loot).length) { emptyRow(body, 5, "No item has been awarded yet."); }
  renderDkpStats(g, view);
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
function renderDkpStats(g, view) {
  var s = obj(g.dkp_stats), members = arr(s.members), raids = arr(s.raids), due = arr(s.due);
  var line = $("g-due");
  clear(line);
  if (!due.length) { line.appendChild(el("span", "muted", "Nobody is due.")); }
  else {
    line.appendChild(el("span", "muted", "Due an item: "));
    due.forEach(function (guid) { line.appendChild(el("span", "chip", who(view, guid))); line.appendChild(document.createTextNode(" ")); });
  }
  var body = $("g-dkp");
  clear(body);
  members.slice().sort(function (a, b) { return (b.attendance === null ? -1 : b.attendance) - (a.attendance === null ? -1 : a.attendance) || a.guid - b.guid; })
    .forEach(function (m) {
      var tr = el("tr");
      nameCell(tr, m.guid, who(view, m.guid), { you: m.guid === g.human });
      cell(tr, m.attendance === null ? "-" : m.attendance + "%", "num");
      [m.earned, m.spent].forEach(function (v) { cell(tr, dkp(v), "num"); });
      cell(tr, String(num(m.items)), "num");
      cell(tr, String(num(m.since_item)), "num");
      var td = el("td");
      if (m.due) { td.appendChild(el("span", "chip warn", "due")); }
      tr.appendChild(td);
      body.appendChild(tr);
    });
  if (!members.length) { emptyRow(body, 7, "No raid has ended yet."); }
  body = $("g-raids");
  clear(body);
  raids.forEach(function (r) {
    var tr = el("tr");
    cell(tr, when(r.started));
    cell(tr, str(r.raid).toUpperCase() + mode(r.size, r.heroic) + (r.offline ? " (offline)" : ""));
    cell(tr, String(num(r.kills)), "num");
    cell(tr, dkp(r.awarded), "num");
    cell(tr, dkp(r.spent), "num");
    cell(tr, String(num(r.items)), "num");
    cell(tr, String(num(r.paid)), "num");
    body.appendChild(tr);
  });
  if (!raids.length) { emptyRow(body, 7, "No raid has ended yet."); }
}
function pollGuild() {
  if (guildBusy || S.tab !== "guild") { return; }
  guildBusy = true;
  api("/api/guild", { poll: true }).then(function (r) {
    if (refused(r)) { return null; }
    return r.json().then(function (j) { return { status: r.status, j: j }; });
  }).then(function (x) {
    if (!x) { return; }
    if (x.status === 200) { renderGuild(x.j); pollDecisions(); }
    else { $("guild-banner").textContent = str(x.j.error) || "mind.db could not be read."; $("guild-banner").hidden = false; }
  }, function () { /* the strip says it */ })
    .then(function () { guildBusy = false; });
}

registerTab("guild", {
  show: function () { pollGuild(); guildTimer = setInterval(pollGuild, GUILD_MS); },
  hide: function () { clearInterval(guildTimer); guildTimer = null; }
});
