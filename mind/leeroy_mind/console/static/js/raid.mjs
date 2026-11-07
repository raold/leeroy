import { $, api, arr, cell, clear, el, emit, emptyRow, num, obj, on, own, pad, plural, post, refused,
         setText, str } from "./core.mjs";
import { face, nameCell } from "./faces.mjs";
import { askConfirm } from "./mind.mjs";
import { S, roomOf } from "./state.mjs";
import { setPill } from "./strip.mjs";
import { registerTab } from "./tabs.mjs";
import { CLASSES, dkp, mode, person, when, who } from "./words.mjs";

// -- the raid tab ---------------------------------------------------------------------------------

var RAID_MS = 3000;
var RAID_STATES = { idle: ["no raid night open", ""], waiting_call: ["call pending", "warn"],
                    called: ["call out", "warn"], forming: ["forming", "warn"],
                    assembling: ["assembling", "ok"], running: ["under way", "ok"],
                    ended: ["over for tonight", ""] };
var LOOT_STATES = { pending: "open for claims", sending: "being given", acknowledged: "given",
                    charged: "awarded", unclaimed: "nobody needed it", failed: "could not be given",
                    uncertain: "uncertain", cancelled: "cancelled" };
var ROLE_ORDER = { tank: 0, healer: 1, damage: 2 };
var RAID_LABEL = { call: "call the raid", summon: "summon", start: "start the run",
                   stop: "stop the run", end: "end", form: "form the raid" };
var R = null, raidTimer = null, raidBusy = false;
var formReady = "";

function mmss(s) {
  if (typeof s !== "number") { return ""; }
  s = Math.max(0, Math.round(s));
  return Math.floor(s / 60) + ":" + pad(s % 60);
}
function raidName(view, key) {
  var c = arr(obj(view).catalog);
  for (var i = 0; i < c.length; i++) { if (c[i].key === key) { return str(c[i].name); } }
  return str(key).toUpperCase();
}

// The Raid tab: mind.db's view always; the live state and the actions while the mind's
// control endpoint answers
function raidControlsOff(ctl) {
  if (!obj(ctl).enabled) { return str(ctl.reason) || "control is off"; }
  if (ctl.busy) { return "a " + ctl.busy[1] + " of the " + ctl.busy[0] + " is running"; }
  return "";
}
function fillForm(view) {
  var key = JSON.stringify([arr(view.catalog).map(function (c) { return c.key; }),
                            arr(view.officers).map(function (o) { return o.guid; })]);
  if (key === formReady) { return; }
  formReady = key;
  var raid = $("r-raid"), lead = $("r-lead"), keepRaid = raid.value, keepLead = lead.value;
  clear(raid); clear(lead);
  arr(view.catalog).forEach(function (c) {
    var o = el("option", "", str(c.name));
    o.value = str(c.key);
    o.dataset.heroic = c.heroic ? "1" : "";
    raid.appendChild(o);
  });
  [["", "as guild life picks"], ["me", "me"], ["rotate", "someone new"]].forEach(function (p) {
    var o = el("option", "", p[1]);
    o.value = p[0];
    lead.appendChild(o);
  });
  arr(view.officers).forEach(function (off) {
    var o = el("option", "", str(off.name));
    o.value = String(off.guid);
    lead.appendChild(o);
  });
  if (keepRaid) { raid.value = keepRaid; }
  if (keepLead) { lead.value = keepLead; }
  heroicAllowed();
}
function heroicAllowed() {
  var opt = $("r-raid").selectedOptions[0], ok = !!(opt && opt.dataset.heroic);
  $("r-heroic").disabled = !ok;
  if (!ok) { $("r-heroic").checked = false; }
}
$("r-raid").addEventListener("change", heroicAllowed);

function raidFacts(view, live, run) {
  var facts = [];
  if (live) {
    if (live.state === "waiting_call" && live.call_in !== null) { facts.push("the raid call goes out in " + mmss(live.call_in)); }
    if (live.state === "called" && live.pick_left !== null) { facts.push("pick window: " + mmss(live.pick_left) + " left"); }
    if (live.forming) { facts.push("forming " + raidName(view, live.forming.raid) + mode(live.forming.size, live.forming.heroic)); }
    if (live.closing) { facts.push("the last raid is still closing (its standings and wrap-up)"); }
    if (live.offline_raid) { facts.push("an offline raid is running or closing"); }
    if (live.blocked) { facts.push(str(live.blocked)); }
    else if (!live.present || !live.online) { facts.push("you're not in game: raid actions wait until you are"); }
    if (run) {
      facts.push(run.in_zone ? "you are in " + raidName(view, run.raid) : "you are not in " + raidName(view, run.raid) + " yet");
      facts.push(plural(arr(run.inside).length, "member") + " summoned inside of " + arr(run.members).length);
      if (arr(run.late).length) {
        facts.push(plural(run.late.length, "late member") + " still on the way (next in " + mmss(Math.min.apply(null, run.late.map(function (l) { return num(l[1]); }))) + ")");
      }
      if (run.pull) { facts.push("a pull is under way"); }
      if (run.ready_in !== null) { facts.push("ready check in " + mmss(run.ready_in)); }
      if (run.dc_in !== null) { facts.push((run.countdown ? "dungeon-clear starts in " : "pull countdown in ") + mmss(run.countdown ? run.dc_in : Math.max(0, run.dc_in - 5))); }
      facts.push(run.lead_is_human ? "you lead: dungeon-clear does not run"
                 : run.dc === "on" ? "dungeon-clear is running" : run.dc === "held" ? "dungeon-clear is stopped (Start resumes it)"
                 : "dungeon-clear has not started");
    }
  }
  if (view && view.quiet_night) { facts.push("tonight is a quiet night (a hangout was called)"); }
  return facts;
}
function renderRaid(j) {
  R = j;
  var view = obj(j.view), m = obj(j.mind), live = m.ok ? obj(m.raid) : null, ctl = obj(j.control);
  if (live && !live.state) { live = null; }
  var run = live ? live.run : null, open = view.open || null;
  if (j.view) { fillForm(view); }
  var off = raidControlsOff(ctl), banner = [];
  if (!j.view) { banner.push("mind.db could not be read just now."); }
  if (!m.ok) { banner.push("The raid night's live state and its actions are unavailable: " + str(m.error) + "."); }
  else if (!live) { banner.push("The mind answers but runs no guild life, so it has no raid night."); }
  if (off && m.ok) { banner.push("Raid actions are off here: " + off + "."); }
  $("raid-banner").textContent = banner.join(" ");
  $("raid-banner").hidden = !banner.length;
  var st = live ? RAID_STATES[live.state] || [live.state, ""] : open ? ["open in mind.db", "warn"] : ["-", ""];
  setPill("r-state", st[1], st[0], "");
  var summary = "No raid is open.";
  var shown = open || (run ? { raid: run.raid, size: run.size, heroic: run.heroic, lead: run.lead,
                               tank: run.tank, loot: run.loot, kills: run.kills, wipes: run.wipes } : null);
  if (shown) {
    var o = obj(shown);
    summary = raidName(view, o.raid) + mode(o.size, o.heroic) + (o.offline ? " (offline)" : "")
      + " · lead " + who(view, o.lead) + " · tank " + who(view, o.tank) + " · loot " + who(view, o.loot)
      + (o.started ? " · since " + when(o.started) : "")
      + " · " + plural(num(o.kills), "kill") + (o.bosses ? " of " + o.bosses : "") + ", " + plural(num(o.wipes), "wipe");
  }
  setText("r-summary", summary);
  var facts = $("r-facts");
  clear(facts);
  raidFacts(view, live, run).forEach(function (f) { facts.appendChild(el("li", "", f)); });
  var actions = live ? obj(live.actions) : {};
  var buttons = document.querySelectorAll("button[data-raid]");
  for (var i = 0; i < buttons.length; i++) {
    var a = buttons[i].dataset.raid, why = !live ? str(m.error) || "unavailable" : off || actions[a];
    buttons[i].disabled = !!why;
    buttons[i].title = why ? str(why) : "";
  }
  var formWhy = !live ? str(m.error) || "unavailable" : off || actions.form;
  $("r-go").disabled = !!formWhy;
  $("r-go").title = formWhy ? str(formWhy) : "";
  renderRaidMembers(view, open, run);
  renderRaidLoot(view, open);
  renderRaidDkp(view, open);
  renderRecent(view);
}
function renderRaidMembers(view, open, run) {
  var body = $("r-members"), members = arr(open ? open.members : run ? run.members : []);
  var inside = {}, late = {};
  if (run) {
    arr(run.inside).forEach(function (g) { inside[g] = true; });
    arr(run.late).forEach(function (l) { late[l[0]] = l[1]; });
  }
  clear(body);
  var rows = members.map(function (g) { return { g: g, p: person(view, g) }; });
  rows.sort(function (a, b) {
    var ra = ROLE_ORDER[a.p.role], rb = ROLE_ORDER[b.p.role];
    if (ra !== rb) { return (ra === undefined ? 9 : ra) - (rb === undefined ? 9 : rb); }
    return str(a.p.name).toLowerCase() < str(b.p.name).toLowerCase() ? -1 : 1;
  });
  rows.forEach(function (r) {
    var tr = el("tr", r.p.online === false ? "off" : "");
    var tags = [];
    if (open && r.g === open.lead) { tags.push("lead"); }
    if (open && r.g === open.tank) { tags.push("tank"); }
    if (open && r.g === open.loot) { tags.push("loot"); }
    nameCell(tr, r.g, who(view, r.g), { you: r.g === view.human,
                                       text: who(view, r.g) + (tags.length ? " (" + tags.join(", ") + ")" : "") });
    cell(tr, str(r.p.role) || "-", "role-" + str(r.p.role));
    cell(tr, (CLASSES[r.p["class"]] || "-") + (r.p.spec ? " " + r.p.spec : ""));
    cell(tr, r.p.level ? String(r.p.level) : "-", "num");
    cell(tr, r.p.online ? "online" : "offline");
    cell(tr, roomOf(r.g) || "-");
    cell(tr, !run ? "-" : inside[r.g] ? "inside" : own(late, r.g) ? "late, " + mmss(late[r.g]) : "on the way");
    cell(tr, dkp(obj(view.balances)[String(r.g)]), "num");
    body.appendChild(tr);
  });
  if (!rows.length) { emptyRow(body, 8, open || run ? "Nobody is listed for this raid." : "No raid is open."); }
  setText("r-count", members.length ? plural(members.length, "member") + (open && open.human_in_raid ? " and you" : "") : "");
  var bench = arr(open ? open.bench : run ? run.bench : []);
  setText("r-bench", bench.length ? "Bench: " + bench.map(function (g) { return who(view, g); }).join(", ") : "");
}
function renderRaidLoot(view, open) {
  var body = $("r-loot"), items = arr(open && open.loot_items);
  clear(body);
  items.forEach(function (it) {
    var tr = el("tr");
    cell(tr, str(it.name) || ("item " + (it.item_id || "?")));
    cell(tr, typeof it.price === "number" ? String(it.price) : "-", "num");
    cell(tr, (LOOT_STATES[it.state] || str(it.state)) + (it.claimed ? " (you claimed it)" : ""));
    nameCell(tr, it.winner, it.winner ? who(view, it.winner) : "-", { you: it.winner === view.human });
    cell(tr, typeof it.paid === "number" ? dkp(it.paid) + (it.close ? " (close)" : "") : "-", "num");
    body.appendChild(tr);
  });
  if (!items.length) { emptyRow(body, 5, open ? "No epic has dropped yet." : "No raid is open."); }
  setText("r-lootn", items.length ? plural(items.length, "drop") : "");
}
function renderRaidDkp(view, open) {
  var box = $("r-dkp"), events = arr(open && open.dkp && open.dkp.events);
  clear(box);
  events.slice().reverse().forEach(function (e) {
    var text, amt = dkp(Math.abs(num(e.each)));
    if (e.reason === "kill") { text = "+" + amt + " to " + plural(e.n, "member") + " for a kill"; }
    else if (e.reason === "attendance") { text = "+" + amt + " attendance to " + plural(e.n, "member"); }
    else if (e.reason === "standby") { text = "+" + amt + " standby to " + plural(e.n, "member"); }
    else if (e.reason === "item") { text = who(view, e.guid) + " paid " + dkp(-num(e.total)) + (e.item ? " for " + e.item : ""); }
    else { text = str(e.reason) + " " + dkp(num(e.total)) + (e.guid ? " for " + who(view, e.guid) : ""); }
    var line = el("div", "r");
    if (e.guid) {
      var wrap = el("span", "dkpface");
      wrap.appendChild(face(e.guid, who(view, e.guid), "list", { you: e.guid === view.human }));
      line.appendChild(wrap);
    }
    line.appendChild(document.createTextNode(when(e.ts) + "  " + text));
    box.appendChild(line);
  });
  if (!events.length) { box.appendChild(el("div", "muted", open ? "No DKP has moved in this raid yet." : "No raid is open.")); }
  box.scrollTop = box.scrollHeight;
}
function renderRecent(view) {
  var body = $("r-recent"), raids = arr(view.recent);
  clear(body);
  raids.forEach(function (r) {
    var tr = el("tr");
    cell(tr, when(r.started));
    cell(tr, raidName(view, r.raid) + mode(r.size, r.heroic) + (r.offline ? " (offline)" : ""));
    nameCell(tr, r.lead, who(view, r.lead), { you: r.lead === view.human });
    cell(tr, String(num(r.kills)) + (r.bosses ? "/" + r.bosses : ""), "num");
    cell(tr, String(num(r.wipes)), "num");
    cell(tr, (r.not_raided ? "not raided: " : "") + (str(r.outcome).replace(/_/g, " ") || "-"));
    cell(tr, String(num(r.items)), "num");
    cell(tr, r.mvp ? who(view, r.mvp.guid) + (r.mvp.votes ? " (" + plural(r.mvp.votes, "vote") + ")" : "") : "-");
    body.appendChild(tr);
  });
  if (!raids.length) { emptyRow(body, 8, "No raid has ended yet."); }
}
function pollRaid() {
  if (raidBusy || S.tab !== "raid") { return; }
  raidBusy = true;
  api("/api/raid").then(function (r) {
    if (refused(r)) { return null; }
    return r.json();
  }).then(function (j) { if (j) { renderRaid(j); } }, function () { /* the strip says it */ })
    .then(function () { raidBusy = false; });
}
function raidAct(action, body) {
  var why = $("r-why");
  why.textContent = RAID_LABEL[action] + ": sent to the mind…";
  why.classList.remove("bad");
  post("/api/raid", body).then(function (j) {
    why.textContent = j.ok ? str(j.message) : str(j.error) || "it did not work";
    why.classList.toggle("bad", !j.ok);
    pollRaid();
  }, function () { why.textContent = "The console is not reachable."; why.classList.add("bad"); });
}
document.querySelector(".raidacts").addEventListener("click", function (ev) {
  var b = ev.target.closest("button[data-raid]");
  if (!b || b.disabled) { return; }
  var action = b.dataset.raid;
  if (action === "end") {
    askConfirm(R && R.mind && R.mind.raid && R.mind.raid.state === "called"
               ? "Say no raid tonight? The guild is told." : "End tonight's raid? The standings and the wrap-up follow.",
               "End", function () { raidAct("end", { action: "end" }); });
  } else if (action === "stop") {
    askConfirm("Stop dungeon-clear? The raid goes on, and Start resumes it.", "Stop",
               function () { raidAct("stop", { action: "stop" }); });
  } else {
    raidAct(action, { action: action });
  }
});
$("r-form").addEventListener("submit", function (ev) {
  ev.preventDefault();
  if ($("r-go").disabled) { return; }
  var lead = $("r-lead").value, size = $("r-size").value;
  raidAct("form", { action: "form", raid: $("r-raid").value, size: size ? Number(size) : null,
                    heroic: $("r-heroic").checked,
                    lead: lead === "" ? null : (lead === "me" || lead === "rotate") ? lead : Number(lead) });
});

on("raid", function (d) {
  emit("note", "raid, " + (RAID_LABEL[d.action] || str(d.action)) + ": " + str(d.message));
  if (S.tab === "raid") { pollRaid(); }
});
registerTab("raid", {
  show: function () { pollRaid(); raidTimer = setInterval(pollRaid, RAID_MS); },
  hide: function () { clearInterval(raidTimer); raidTimer = null; }
});
