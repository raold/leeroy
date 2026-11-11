import { $, api, arr, cell, clear, el, emit, emptyRow, num, obj, on, own, plural, post, recall, refused,
         remember, setText, str } from "./core.mjs";
import { face } from "./faces.mjs";
import { appendInsightBadges, pollInsight, selectInsight } from "./insight.mjs";
import { photoButton } from "./gallery.mjs";
import { nudgeBadges, nudgeFocus, nudgePanel } from "./nudges.mjs";
import { forgetMemberShots, pollSelfies, setSelfieNames, showMemberPhoto } from "./selfies.mjs";
import { S } from "./state.mjs";
import { registerTab, showTab } from "./tabs.mjs";
import { CLASSES, moodText, moodTitle, photoKindText, signed, tileState, when, youText, youTitle } from "./words.mjs";

// -- the personas tab: mind.db only, and read-only: nothing here can change a persona -----------
// (its dials are the tuning's overlay, below; its profile photos are selfies.mjs's)

var PERSONAS_MS = 30000;  // as the Guild tab: the live mind writes this database meanwhile
var TRAITS = ["openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"];
var BY = { seed: "the seeding", owner: "you (an import or a personas command)", generator: "the generator",
           night: "the night's looks pass", photo: "the night's photo-kind pass", stance: "a stance move",
           undo: "an undo of a stance move" };
var LEDGER_KINDS = { note_added: "note added", note_dropped: "note dropped", note_rolled_back: "note rolled back (drift)",
                     drift_flag: "drift flag", stance_band: "stance move", undo: "undo" };
var personasTimer = null, personasBusy = false, pick = 0, pickSeq = 0, reveal = false;
var rosterOffset = 0, rosterPage = null, rosterDrawn = "";
var paging = el("div", "roster-pages"), previousPage = el("button", "", "Previous members"),
    nextPage = el("button", "", "Next members"), pageWords = el("span", "muted");
paging.setAttribute("aria-label", "Member pages");
paging.setAttribute("role", "group");
previousPage.type = nextPage.type = "button";
paging.appendChild(previousPage); paging.appendChild(pageWords); paging.appendChild(nextPage);
$("p-wall").before(paging);
previousPage.addEventListener("click", function () {
  if (rosterPage) { rosterOffset = Math.max(0, rosterOffset - rosterPage.limit); pollPersonas(); }
});
nextPage.addEventListener("click", function () {
  if (rosterPage && rosterPage.more) {
    rosterOffset = Math.min(Math.floor((rosterPage.total - 1) / rosterPage.limit) * rosterPage.limit, rosterOffset + rosterPage.limit);
    pollPersonas();
  }
});

function pname(view, guid) {
  if (!guid) { return "-"; }
  if (view && guid === view.human) { return "you"; }
  return str(obj(obj(view).names)[String(guid)]) || "#" + guid;
}
function tdT(tr, text, cls, title) {
  var td = el("td", cls || "", text);
  if (title) { td.title = title; }
  tr.appendChild(td);
}
function kv(dl, pairs) {
  clear(dl);
  pairs.forEach(function (p) {
    if (p[1] === null || p[1] === undefined || p[1] === "") { return; }
    dl.appendChild(el("dt", "", p[0]));
    dl.appendChild(el("dd", "", String(p[1])));
  });
}
function tiesText(view, t) {
  if (!t) { return "unavailable"; }
  if (!arr(t.top).length) { return "none yet"; }
  return t.top.map(function (r) { return pname(view, r.guid) + " " + signed(r.affinity, 0); }).join(", ")
    + (t.total > t.top.length ? " (+" + (t.total - t.top.length) + " more)" : "");
}
function memText(m) {
  if (!m) { return "unavailable"; }
  return m.total ? m.recent + " / " + m.total : "none yet";
}
function personaText(p) {
  if (!p) { return "no persona"; }
  return "v" + p.version + " · " + when(p.updated) + " · " + (BY[p.by] || str(p.by)) + (p.readable ? "" : " (unreadable)");
}
function biggestShift(traits) {
  var best = null, values = obj(obj(traits).values);
  TRAITS.forEach(function (t) {
    var v = values[t];
    if (v && typeof v.shift === "number" && (!best || Math.abs(v.shift) > Math.abs(best[1]))) { best = [t, v.shift]; }
  });
  return best;
}
function driftText(d) {
  if (!d) { return "unavailable"; }
  var bits = [], b = biggestShift(d.traits);
  if (typeof d.flags === "number") { bits.push(d.flags ? plural(d.flags, "open flag") : "no open flags"); }
  if (b) { bits.push(b[0] + " " + signed(b[1], 1)); }
  else if (!d.traits) { bits.push("traits not sampled"); }
  return bits.join(" · ");
}
function driftTitle(d) {
  var t = obj(d).traits;
  if (!t) { return "Open flags: lines the night's critic found off-persona in the last days, not undone or resolved."; }
  return "Traits the night sampled for " + str(t.night) + ": " + TRAITS.filter(function (n) { return obj(t.values)[n]; })
    .map(function (n) { var v = t.values[n]; return n + " " + num(v.sampled).toFixed(1) + (v.base !== null ? " (persona " + v.base + ")" : ""); })
    .join(", ");
}

function renderPersonas(v) {
  $("personas-banner").hidden = true;
  setSelfieNames(v.names);
  var signature = JSON.stringify(v);
  if (signature === rosterDrawn) { return; }
  var focused = document.activeElement, restore = null;
  if (focused && ($("p-wall").contains(focused) || $("p-table").contains(focused))) {
    restore = focused.dataset.gallery ? '[data-gallery="' + Number(focused.dataset.gallery) + '"]' :
      focused.dataset.guid ? (focused.classList.contains("tile-detail") ? '.tile-detail' : 'tr') +
        '[data-guid="' + Number(focused.dataset.guid) + '"]' : null;
  }
  var members = arr(v.members), days = num(v.recent_days) || 7, gm = v.guild_mood, missing = arr(v.missing);
  rosterPage = v.page || { offset: 0, limit: members.length, total: members.length, more: false };
  rosterOffset = rosterPage.offset;
  paging.hidden = rosterPage.total <= rosterPage.limit;
  previousPage.disabled = rosterOffset === 0;
  nextPage.disabled = !rosterPage.more;
  pageWords.textContent = "Members " + (members.length ? rosterOffset + 1 : 0) + "–" + (rosterOffset + members.length) + " of " + rosterPage.total;
  setText("p-count", plural(rosterPage.total, "member") + ", "
          + (rosterPage.personas === undefined ? members.filter(function (m) { return m.persona; }).length : rosterPage.personas) + " with a persona");
  setText("p-mood", gm ? "Guild mood, night of " + str(gm.night) + ": " + signed(gm.value, 2)
          + (arr(gm.reasons).length ? " (" + gm.reasons.join(", ") + ")" : "")
          : missing.indexOf("guild_mood") >= 0 ? "Guild mood: unavailable in this mind.db."
          : "Guild mood: not recorded yet (the night's mood stage has not run).");
  $("p-missing").textContent = missing.length ? "This mind.db is older than some of what this tab reads; "
    + "unavailable here: " + missing.join(", ") + "." : "";
  $("p-missing").hidden = !missing.length;
  $("p-insight-badges-why").textContent = str(v.insight_badges_why);
  $("p-insight-badges-why").hidden = !v.insight_badges_why;
  setText("p-memhead", "memories (" + days + " d / held)");
  var body = $("p-roster");
  clear(body);
  members.forEach(function (m) {
    var tr = el("tr", (m.online === false ? "off" : "") + (m.guid === pick ? " sel" : ""));
    tr.tabIndex = 0;
    tr.dataset.guid = String(m.guid);
    var nameTd = el("td"), who = el("div", "who");
    who.appendChild(photoButton(m.guid, str(m.name), "list"));
    who.appendChild(el("span", "", str(m.name) + (m.in_cast ? "" : " (not in the cast)")));
    nameTd.appendChild(who);
    appendInsightBadges(nameTd, obj(v.insight_badges)[String(m.guid)]);
    tr.appendChild(nameTd);
    tdT(tr, m["class"] ? (CLASSES[m["class"]] || "-") + (m.role ? " · " + m.role : "") : "-", m.role ? "role-" + m.role : "");
    tdT(tr, typeof m.rank === "number" ? String(m.rank) : "-", "num");
    tdT(tr, missing.indexOf("pad_mood") >= 0 ? "unavailable" : moodText(m.mood), "", m.mood ? moodTitle(m.mood) : "");
    tdT(tr, m.stance || "-");
    tdT(tr, missing.indexOf("relationship") >= 0 ? "unavailable" : youText(m.you), "", youTitle(m.you));
    tdT(tr, tiesText(v, m.ties));
    tdT(tr, memText(m.memories), "num");
    tdT(tr, personaText(m.persona), "", m.persona ? "source " + m.persona.source : "The mind voices only members with a stored persona.");
    tdT(tr, driftText(m.drift), "", m.drift ? driftTitle(m.drift) : "");
    body.appendChild(tr);
  });
  if (!members.length) { emptyRow(body, 10, "The mind has no cast and no personas yet."); }
  renderWall(v);
  if (restore && !focused.isConnected) {
    var target = (view === "table" ? $("p-table") : $("p-wall")).querySelector(restore);
    if (target) { target.focus(); }
  }
  rosterDrawn = signature;
}
var VIEW_KEY = "guild-console-personas-view";
var view = recall(VIEW_KEY, "wall") === "table" ? "table" : "wall";
function applyView() {
  $("p-wall").hidden = view !== "wall";
  $("p-table").hidden = view !== "table";
  var bs = $("p-view").querySelectorAll("button[data-view]");
  for (var i = 0; i < bs.length; i++) { bs[i].setAttribute("aria-pressed", String(bs[i].dataset.view === view)); }
}
$("p-view").addEventListener("click", function (ev) {
  var b = ev.target.closest("button[data-view]");
  if (!b) { return; }
  view = b.dataset.view === "table" ? "table" : "wall";
  remember(VIEW_KEY, view);
  applyView();
});
applyView();

function renderWall(v) {
  var members = arr(v.members), missing = arr(v.missing), box = $("p-wall");
  clear(box);
  members.forEach(function (m) {
    var st = tileState(m), t = el("div", "tile" + (st.off ? " off" : "") + (m.guid === pick ? " sel" : ""));
    t.dataset.guid = String(m.guid);
    t.appendChild(photoButton(m.guid, str(m.name), "wall"));
    var detail = el("button", "tile-detail tname", st.label);
    detail.type = "button";
    detail.dataset.guid = String(m.guid);
    detail.setAttribute("aria-label", "Profile of " + str(m.name));
    t.appendChild(detail);
    t.appendChild(el("span", "tsub", st.sub));
    t.appendChild(el("span", "tsub", missing.indexOf("pad_mood") >= 0 ? "unavailable" : moodText(m.mood)));
    if (st.chip) { t.appendChild(el("span", "chip", st.chip)); }
    appendInsightBadges(t, obj(v.insight_badges)[String(m.guid)]);
    box.appendChild(t);
  });
  if (!members.length) { box.appendChild(el("p", "why", "The mind has no cast and no personas yet.")); }
  nudgeBadges(tuneView());
}
function pollPersonas() {
  if (personasBusy || S.tab !== "personas") { return; }
  personasBusy = true;
  var requestedOffset = rosterOffset;
  api("/api/personas?offset=" + requestedOffset, { poll: true }).then(function (r) {
    if (refused(r)) { return null; }
    return r.json().then(function (j) { return { status: r.status, j: j }; });
  }).then(function (x) {
    if (!x || requestedOffset !== rosterOffset) { return; }
    if (x.status === 200) { renderPersonas(x.j); }
    else { $("personas-banner").textContent = str(x.j.error) || "mind.db could not be read."; $("personas-banner").hidden = false; }
  }, function () { /* the strip says it */ })
    .then(function () {
      personasBusy = false;
      if (requestedOffset !== rosterOffset) { pollPersonas(); }
      else if (pick) { pollMember(); }
    });
}

var shownFace = null;
function renderMember(j) {
  var m = obj(j.member), c = j.card ? obj(j.card) : null, p = m.persona, days = num(j.recent_days) || 7;
  var missing = arr(j.missing), traits = obj(obj(m.drift).traits), values = obj(traits.values);
  $("p-detail").hidden = false;
  setText("p-name", str(m.name));
  if (shownFace !== m.guid) {
    shownFace = m.guid;
    clear($("p-face"));
    $("p-face").appendChild(face(m.guid, str(m.name), "card"));
  }
  showMemberPhoto(m.guid, str(m.name));
  setText("p-sub", [m["class"] ? (CLASSES[m["class"]] || "-") + (m.spec ? " " + m.spec : "") : "", str(m.role),
                    m.level ? "level " + m.level : "", typeof m.rank === "number" ? "rank " + m.rank : "",
                    m.in_cast ? (m.online ? "online" : "offline") : "not in the cast"].filter(Boolean).join(" · "));
  setText("p-lead", "Mood: " + (missing.indexOf("pad_mood") >= 0 ? "unavailable" : moodText(m.mood))
          + (m.mood ? " (" + moodTitle(m.mood) + ")" : "") + " · stance: " + (m.stance || "-")
          + " · toward you: " + (missing.indexOf("relationship") >= 0 ? "unavailable" : youText(m.you)));
  setText("p-why", p ? "Persona v" + p.version + ", last changed " + when(p.updated) + " by " + (BY[p.by] || str(p.by))
          + (p.readable ? "." : "; this console cannot read it (a newer persona schema?).")
          : "No persona is stored for this member yet, so the mind does not voice them.");
  var five = $("p-five");
  setText("p-nighthead", traits.night ? "night " + str(traits.night).slice(5) : "the night");
  clear(five);
  if (c) {
    TRAITS.forEach(function (t) {
      var b = obj(obj(c.big_five)[t]), v = values[t], tr = el("tr");
      cell(tr, t);
      cell(tr, String(num(b.score)), "num");
      cell(tr, v ? num(v.sampled).toFixed(1) : "-", "num");
      cell(tr, v && typeof v.shift === "number" ? signed(v.shift, 1) : "-", "num");
      cell(tr, str(b.behaviour));
      five.appendChild(tr);
    });
  } else { emptyRow(five, 5, p ? "This persona could not be read." : "No persona yet."); }
  var drift = obj(m.drift);
  if (!c) { kv($("p-who"), []); kv($("p-typing"), []); kv($("p-game"), []); }
  else {
    var id = obj(c.identity), ro = obj(c.romance), ty = obj(c.typing), ga = obj(c.game), pl = obj(c.play);
    kv($("p-who"), [["player", [id.age, id.player_gender, id.region].filter(Boolean).join(", ")],
                    ["attachment", c.attachment], ["humour", c.humour],
                    ["romance", ro.stance + (ro.style ? ", " + ro.style : "") + (ro.jealousy_mode ? ", " + ro.jealousy_mode.replace(/_/g, " ") : "")],
                    ["drift flags", typeof drift.flags === "number" ? (drift.flags ? plural(drift.flags, "open flag") + " in " + days + " days" : "none open") : "unavailable"],
                    ["the night's traits", traits.night ? "sampled for " + traits.night : missing.indexOf("trait_state") >= 0 ? "unavailable" : "not sampled yet"]]);
    kv($("p-typing"), [["case", ty["case"]], ["end punctuation", ty.end_punct],
                       ["emoticons", arr(ty.emoticons).length ? ty.emoticons.join(" ") + " (" + Math.round(num(ty.emoticon_rate) * 100) + "% of lines)" : "none"],
                       ["abbreviations", Object.keys(obj(ty.abbreviations)).map(function (k) { return k + " > " + ty.abbreviations[k]; }).join(", ") || "none"],
                       ["typos", Math.round(num(ty.typo_rate) * 100) + "% of words"],
                       ["words per line", arr(ty.words_per_line).join(" to ")],
                       ["split lines", Math.round(num(ty.split_rate) * 100) + "%"],
                       ["typing speed", num(ty.chars_per_sec).toFixed(1) + " chars/s"]]);
    kv($("p-game"), [["motivation", ga.motivation], ["thinks as", ga.thinks_as], ["wants", arr(ga.wants).join("; ")],
                     ["outside raids", arr(ga.outside_raids).join(", ").replace(/_/g, " ")], ["favourite spot", ga.favourite_spot],
                     ["years played", ga.years_played], ["spec", str(pl.main_spec) + (pl.off_spec ? " (off: " + pl.off_spec + ")" : "")],
                     ["raid role", pl.raid_role], ["skill", pl.skill],
                     ["guild role", str(pl.guild_role) + (pl.officer_title ? " (" + pl.officer_title + ")" : "")]]);
  }
  var a = c && c.appearance ? obj(c.appearance) : null;
  kv($("p-looks"), a ? [["height", a.height_cm + " cm"], ["build", a.build], ["weight", a.weight_kg + " kg"],
                        ["hair", [a.hair_colour, str(a.hair_length).replace(/_/g, " "), a.hair_style].filter(Boolean).join(", ")],
                        ["skin", a.skin_tone], ["eyes", a.eye_colour], ["face", arr(a.face).join(", ")], ["at the PC", a.at_the_pc],
                        ["photo kind", c.photo ? photoKindText(c.photo) : "none yet: the night's photo-kind pass gives one"]]
                     : [["looks", c ? "none yet: the night's looks pass gives them" : "-"]]);
  var kinds = $("p-kinds"), mem = j.memories;
  setText("p-kindhead", "in " + days + " d");
  clear(kinds);
  if (mem) {
    Object.keys(mem).sort().forEach(function (k) {
      var tr = el("tr");
      cell(tr, k);
      cell(tr, String(num(mem[k].recent)), "num");
      cell(tr, String(num(mem[k].total)), "num");
      kinds.appendChild(tr);
    });
  }
  if (!mem || !Object.keys(mem).length) { emptyRow(kinds, 3, mem ? "None held yet." : "Unavailable in this mind.db."); }
  setText("p-memn", m.memories && m.memories.total ? plural(m.memories.total, "memory", "memories") + " held" : "");
  var ties = $("p-ties"), t = m.ties;
  clear(ties);
  arr(t && t.top).forEach(function (r) {
    var tr = el("tr");
    cell(tr, pname(j, r.guid));
    cell(tr, str(r.label) || "-");
    cell(tr, signed(r.affinity, 0), "num");
    [r.trust, r.respect, r.attraction, r.familiarity].forEach(function (x) { cell(tr, String(Math.round(num(x))), "num"); });
    cell(tr, r.romance_stage && r.romance_stage !== "none" ? r.romance_stage : "-");
    cell(tr, String(num(r.interactions)), "num");
    cell(tr, r.last_seen ? when(r.last_seen) : "-");
    cell(tr, str(r.notes) || "-");
    ties.appendChild(tr);
  });
  if (!t || !arr(t.top).length) { emptyRow(ties, 11, t ? "No ties yet." : "Unavailable in this mind.db."); }
  setText("p-tien", t && t.total ? (t.total > arr(t.top).length ? "the strongest " + t.top.length + " of " + t.total : plural(t.total, "tie")) : "");
  var box = $("p-ledger");
  clear(box);
  arr(j.ledger).forEach(function (r) {
    box.appendChild(el("div", "r", when(r.ts) + "  " + (LEDGER_KINDS[r.kind] || str(r.kind)) + ": " + str(r.text) + (r.undone ? "  (undone)" : "")));
  });
  if (!arr(j.ledger).length) { box.appendChild(el("div", "muted", j.ledger ? "No ledger rows yet." : "Unavailable in this mind.db.")); }
  if (reveal) { reveal = false; $("p-detail").scrollIntoView({ block: "start" }); }
}
function pollMember() {
  if (!pick || S.tab !== "personas") { return; }
  var seq = ++pickSeq, guid = pick;
  pollInsight(guid);
  api("/api/persona?guid=" + guid, { poll: true }).then(function (r) {
    if (refused(r)) { return null; }
    return r.json().then(function (j) { return { status: r.status, j: j }; });
  }).then(function (x) {
    if (!x || seq !== pickSeq || guid !== pick) { return; }
    if (x.status === 200) { renderMember(x.j); }
    else if (x.status === 404) { choose(0); }
    else { setText("p-why", str(x.j.error) || "mind.db could not be read."); }
  }, function () { /* the strip says it */ });
}
function choose(guid) {
  pick = guid;
  selectInsight(guid);
  var rows = $("p-roster").querySelectorAll("tr[data-guid]");
  for (var i = 0; i < rows.length; i++) { rows[i].classList.toggle("sel", Number(rows[i].dataset.guid) === guid); }
  var tiles = $("p-wall").querySelectorAll(".tile[data-guid]");
  for (var j = 0; j < tiles.length; j++) { tiles[j].classList.toggle("sel", Number(tiles[j].dataset.guid) === guid); }
  if (!guid) { $("p-detail").hidden = true; reveal = false; return; }
  if (TUNE) { renderDials(guid); }
  pollMember();
}
$("p-roster").addEventListener("click", function (ev) {
  var tr = ev.target.closest("tr[data-guid]");
  if (tr) { choose(Number(tr.dataset.guid)); }
});
$("p-roster").addEventListener("keydown", function (ev) {
  var tr = ev.target.closest("tr[data-guid]");
  if (tr && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); choose(Number(tr.dataset.guid)); }
});
$("p-wall").addEventListener("click", function (ev) {
  if (ev.target.closest(".gallery-opener")) { return; }
  var t = ev.target.closest(".tile[data-guid]");
  if (t) { choose(Number(t.dataset.guid)); }
});
$("p-close").addEventListener("click", function () { choose(0); });

// Open this tab on one member's detail (the Profile of their card in Comms), scrolled into view
// once it is drawn. The tab is shown first, so the member is asked for at once.
export function openMember(guid) {
  showTab("personas");
  reveal = true;
  choose(guid);
}

// The dials: read from mind.db (/api/tuning); every change and preview goes to the running
// mind's tune op (/api/tune), which checks it again and says why when it refuses. Preview text
// is shown here only: never stored, logged or sent anywhere else.
var TUNE = null, tuneBusy = false, lastPreview = "", gWhere = {};
var WHO = { women: "women only", party: "drinkers and smokers only" };
function tuneView() { return obj(obj(TUNE).view); }
function tuneLive() { return !!TUNE && obj(TUNE.mind).ok === true && obj(TUNE.control).enabled !== false; }
function tuneWhy() {
  if (!TUNE) { return "reading"; }
  if (obj(TUNE.control).enabled === false) { return str(obj(TUNE.control).reason) || "control is off"; }
  return obj(TUNE.mind).ok === true ? "" : str(obj(TUNE.mind).error);
}
function previewKey(scope, target, dial, value) { return [scope, target, dial, value].join("|"); }
function showPreview(what, r) {
  var box = $("t-preview");
  clear(box);
  $("t-pvcard").hidden = false;
  setText("t-pvwhat", what);
  if (r.ok !== true) { box.appendChild(el("p", "why bad", str(r.error) || "refused")); return; }
  arr(r.world).forEach(function (x) { box.appendChild(el("p", "pv", numberText(x))); });
  if (r.note) { box.appendChild(el("p", "why", str(r.note))); }
  arr(r.subjects).forEach(function (s) {
    var d = el("div", "pv");
    d.appendChild(el("h4", "", str(s.name) || "#" + s.guid));
    if (s.numbers) { arr(s.numbers).forEach(function (x) { d.appendChild(el("p", "pv", numberText(x))); }); }
    ["before", "after"].forEach(function (k) {
      var line = obj(s[k]);
      if (!s.numbers) {
        var p = el("p", "pv", k + ": ");
        p.appendChild(el("span", line.text ? "said" : "muted", line.text ? str(line.text) : str(line.note)));
        d.appendChild(p);
      }
    });
    box.appendChild(d);
  });
  if (typeof r.reach === "number") { box.appendChild(el("p", "why", "It reaches " + plural(r.reach, "member") + " now.")); }
}
function numberText(x) {
  function v(y) { return y === true ? "yes" : y === false ? "no" : y === null ? "never" : String(y) + (x.unit ? " " + x.unit : ""); }
  return str(x.what) + ": " + v(x.before) + " → " + v(x.after);
}
function tuneAsk(args, what) {
  var preview = args.action === "preview" || (args.action === "nudge" && args.verb === "preview");
  if (preview) { setText("t-pvwhat", what + ": asking the mind"); $("t-pvcard").hidden = false; }
  return post("/api/tune", args).then(function (r) {
    if (r.status === 403 && !r.error) { emit("key-lost"); return r; }
    if (preview) {
      showPreview(what, r);
      if (r.ok === true) { lastPreview = previewKey(args.scope, args.target, args.dial, args.value); }
    } else if (r.ok !== true) { emit("note", what + " refused: " + str(r.error)); }
    else { emit("note", what + ": " + str(r.message)); pollTuning(); }
    return r;
  }, function () { emit("note", what + ": the console could not be reached"); });
}
function setValue(scope, target, dial, value, what) {
  return tuneAsk({ action: "set", scope: scope, target: target, dial: dial, value: value,
                   previewed: lastPreview === previewKey(scope, target, dial, value) }, what);
}
// One dial: its name and what it moves, a slider, and Preview / Set / Clear
function dialRow(box, o) {
  var row = el("div", "dial" + (o.off ? " off" : ""));
  var nm = el("div", "nm", o.label);
  if (o.sub) { nm.appendChild(el("small", "", o.sub)); }
  row.appendChild(nm);
  var ctl = el("div", "ctl");
  if (o.off) { ctl.appendChild(el("span", "muted", o.off)); }
  else {
    var range = el("input");
    range.type = "range"; range.min = o.lo; range.max = o.hi; range.step = 1; range.value = o.start;
    range.setAttribute("aria-label", o.label);
    var shown = el("span", "v", String(o.start));
    range.addEventListener("input", function () { shown.textContent = (o.signed && range.value > 0 ? "+" : "") + range.value; });
    ctl.appendChild(range); ctl.appendChild(shown);
    var live = tuneLive();
    [["Preview", function () { tuneAsk({ action: "preview", scope: o.scope, target: o.target, dial: o.dial,
                                          value: Number(range.value) }, o.what + " " + range.value); }],
     ["Set", function () { setValue(o.scope, o.target, o.dial, Number(range.value), o.what + " " + range.value); }],
     [o.clear || "Clear", function () { setValue(o.scope, o.target, o.dial, null, o.what + ": cleared"); }]]
      .forEach(function (b, i) {
        var btn = el("button", "", b[0]);
        btn.type = "button";
        btn.disabled = !live || (i === 2 && !o.clearable);
        if (!live) { btn.title = tuneWhy(); }
        btn.addEventListener("click", b[1]);
        ctl.appendChild(btn);
      });
    if (o.state) { ctl.appendChild(el("span", "muted", o.state)); }
  }
  row.appendChild(ctl);
  if (o.moves) { row.appendChild(el("div", "moves", o.moves)); }
  box.appendChild(row);
}
function dialSpec(name) { return arr(tuneView().dials).filter(function (d) { return d.name === name; })[0] || {}; }
function renderTuning() {
  var v = tuneView(), live = tuneLive();
  setText("t-sum", v.available ? "version " + num(v.version) : "");
  if (!v.available) {
    setText("t-lead", "This mind.db has no tuning tables yet (" + arr(v.missing).join(", ") + "): `db migrate` with the mind stopped adds them.");
  } else {
    setText("t-lead", plural(num(v.pinned_members), "member has", "members have") + " values of their own (they win over group and world changes)."
            + (live ? "" : " Read-only now: " + tuneWhy()));
  }
  var world = $("t-world");
  clear(world);
  arr(v.world_dials).forEach(function (d) {
    dialRow(world, { label: d.label, sub: "mind.toml " + (d.file === null || d.file === undefined ? "unread" : d.file)
                     + (d.override !== null && d.override !== undefined ? ", set here " + d.override : ""),
                     lo: d.lo, hi: d.hi, start: d.value === null || d.value === undefined ? d.lo : d.value,
                     scope: "world", target: "", dial: d.name, what: "world " + d.label,
                     clear: "Back to mind.toml", clearable: d.override !== null && d.override !== undefined,
                     moves: str(d.moves) + " (" + str(d.when) + ")", off: v.available ? "" : "unavailable" });
  });
  var groups = $("t-groups");
  var restoreGroupFocus = nudgeFocus(groups);
  clear(groups);
  arr(v.groups).forEach(function (g) {
    var head = el("h4", "", (g.builtin ? "everyone" : str(g.name)) + " · " + plural(num(g.members), "member")
                  + (g.builtin ? "" : " · " + str(g.describe)));
    groups.appendChild(head);
    if (!g.builtin) {
      var rt = el("button", "", "Retire");
      rt.type = "button"; rt.disabled = !live;
      rt.addEventListener("click", function () { tuneAsk({ action: "retire", id: Number(g.target) }, "retire " + g.name); });
      head.appendChild(document.createTextNode(" "));
      head.appendChild(rt);
    }
    var offs = obj(g.offsets);
    arr(v.dials).forEach(function (d) {
      var set = own(offs, d.name);
      if (!set && !g.showAll) { return; }
      dialRow(groups, { label: d.label, sub: set ? "offset " + (offs[d.name] > 0 ? "+" : "") + offs[d.name] : "",
                        lo: -10, hi: 10, start: set ? offs[d.name] : 0, signed: true,
                        scope: "group", target: g.target, dial: d.name, what: (g.builtin ? "everyone" : g.name) + " " + d.label,
                        clearable: set, moves: d.who !== "everyone" ? WHO[d.who] + ": the others are skipped" : "" });
    });
    var add = el("select");
    add.setAttribute("aria-label", "Offset another dial for " + (g.builtin ? "everyone" : g.name));
    add.appendChild(el("option", "", "offset another dial"));
    arr(v.dials).forEach(function (d) { if (!own(offs, d.name)) { var o = el("option", "", d.label); o.value = d.name; add.appendChild(o); } });
    add.addEventListener("change", function () {
      if (!add.value) { return; }
      var d = dialSpec(add.value), box = el("div");
      add.parentNode.insertBefore(box, add);
      dialRow(box, { label: d.label, lo: -10, hi: 10, start: 0, signed: true, scope: "group", target: g.target,
                     dial: d.name, what: (g.builtin ? "everyone" : g.name) + " " + d.label, clearable: false,
                     moves: d.who !== "everyone" ? WHO[d.who] + ": the others are skipped" : "" });
      add.remove();
    });
    groups.appendChild(add);
    nudgePanel(groups, v, "group", g.target, live, tuneWhy(), tuneAsk);
  });
  var fields = obj(v.fields), fsel = $("t-gfield");
  if (!fsel.options.length) {
    Object.keys(fields).forEach(function (k) { var o = el("option", "", k.replace(/_/g, " ")); o.value = k; fsel.appendChild(o); });
    fillValues();
  }
  var hist = $("t-history");
  clear(hist);
  arr(v.history).forEach(function (c) {
    var d = el("div", "chg");
    d.appendChild(el("div", "", "#" + c.id + " " + when(c.ts) + " · " + str(c.source) + " · " + str(c.label)
                     + (c.previewed ? " · previewed" : "") + (c.undone_by ? " · undone by #" + c.undone_by : "")));
    arr(c.rows).forEach(function (r) { d.appendChild(el("div", "muted", "  " + rowText(v, r))); });
    var acts = el("div", "acts");
    [["Undo", { action: "undo", change: c.id }, "undo #" + c.id], ["Revert to here", { action: "revert", to: c.id }, "revert to #" + c.id]]
      .forEach(function (b) {
        var btn = el("button", "", b[0]);
        btn.type = "button"; btn.disabled = !live;
        btn.addEventListener("click", function () { tuneAsk(b[1], b[2]); });
        acts.appendChild(btn);
      });
    d.appendChild(acts);
    hist.appendChild(d);
  });
  if (!arr(v.history).length) { hist.appendChild(el("div", "muted", v.available ? "No dial changes yet." : "Unavailable in this mind.db.")); }
  if (pick) { renderDials(pick); }
  nudgeBadges(v);
  restoreGroupFocus();
}
function rowText(v, r) {
  var who = r.scope === "world" ? "world" : r.scope === "group" ? "group " + groupName(v, r.target) : pname(v, Number(r.target));
  if (r.dial === "definition") {
    return who + (r.after ? " saved: " + str(obj(r.after).name) : " retired");
  }
  function val(x) { return x === null || x === undefined ? "unset" : String(x); }
  return who + " " + r.dial + " " + val(r.before) + " → " + val(r.after);
}
function groupName(v, target) {
  var g = arr(v.groups).filter(function (x) { return x.target === target; })[0];
  return g ? str(g.name) : "#" + target;
}
function renderDials(guid) {
  var v = tuneView(), box = $("p-dials");
  var restoreFocus = nudgeFocus(box);
  clear(box);
  var m = arr(v.members).filter(function (x) { return x.guid === guid; })[0];
  if (!m) { setText("p-dialsum", ""); box.appendChild(el("p", "muted", v.available ? "No dials: this member has no readable persona, or is you." : "Unavailable in this mind.db.")); return; }
  var mine = 0;
  arr(v.dials).forEach(function (d) {
    var x = obj(obj(m.dials)[d.name]), owned = x.own !== null && x.own !== undefined;
    if (owned) { mine += 1; }
    var state = owned ? "your value " + x.own + (x.stale ? " (their persona changed since you set it)" : "")
              : x.value !== x.base ? "groups make it " + x.value : "";
    dialRow(box, { label: d.label, sub: (d.kind === "score" ? "their own score " : "as written ") + x.base
                   + (x.value !== x.base ? " · now " + x.value : ""),
                   lo: d.lo, hi: d.hi, start: owned ? x.own : x.value, scope: "member", target: String(guid),
                   dial: d.name, what: m.name + " " + d.label, clearable: owned, state: state,
                   moves: str(d.moves) + " (from " + str(d.when) + ")", off: x.applies ? "" : str(x.note) });
  });
  setText("p-dialsum", mine ? plural(mine, "value") + " of your own" : "nothing of your own set");
  nudgePanel(box, v, "member", String(guid), tuneLive(), tuneWhy(), tuneAsk);
  restoreFocus();
}
function fillValues() {
  var vals = arr(obj(tuneView().fields)[$("t-gfield").value]), sel = $("t-gvalues");
  clear(sel);
  vals.forEach(function (x, i) { var o = el("option", "", String(x)); o.value = String(i); sel.appendChild(o); });
}
function whereText() {
  var parts = Object.keys(gWhere).map(function (k) { return k.replace(/_/g, " ") + ": " + gWhere[k].join(k === "age" ? "-" : " or "); });
  setText("t-gwhere", parts.length ? parts.join("; ") : "no conditions yet");
}
$("t-gfield").addEventListener("change", fillValues);
$("t-gadd").addEventListener("click", function () {
  var from = Number($("t-gfrom").value), to = Number($("t-gto").value);
  if (from && to) { gWhere.age = [from, to]; $("t-gfrom").value = $("t-gto").value = ""; }
  var field = $("t-gfield").value, all = arr(obj(tuneView().fields)[field]), picked = [];
  var opts = $("t-gvalues").options;
  for (var i = 0; i < opts.length; i++) { if (opts[i].selected) { picked.push(all[Number(opts[i].value)]); } }
  if (picked.length) { gWhere[field] = picked; }
  whereText();
});
$("t-gclear").addEventListener("click", function () { gWhere = {}; $("t-gname").value = ""; whereText(); });
$("t-gsave").addEventListener("click", function () {
  tuneAsk({ action: "group", id: null, name: $("t-gname").value, where: gWhere }, "group " + $("t-gname").value)
    .then(function (r) { if (r && r.ok === true) { gWhere = {}; $("t-gname").value = ""; whereText(); } });
});
$("t-pvclose").addEventListener("click", function () { $("t-pvcard").hidden = true; });
function pollTuning() {
  if (tuneBusy || S.tab !== "personas") { return; }
  tuneBusy = true;
  api("/api/tuning", { poll: true }).then(function (r) {
    if (refused(r)) { return null; }
    return r.json().then(function (j) { return { status: r.status, j: j }; });
  }).then(function (x) {
    if (!x) { return; }
    if (x.status === 200) { TUNE = x.j; renderTuning(); }
    else { setText("t-lead", str(x.j.error) || "mind.db could not be read."); }
  }, function () { /* the strip says it */ })
    .then(function () { tuneBusy = false; });
}
whereText();

on("tuning", function (d) {
  emit("note", "dials: change #" + num(d.change) + " saved");
  if (S.tab === "personas") { pollTuning(); }
});
// A photo picked, undone or asked for again, here or on another page: faces.mjs already let the
// old photo go, so the roster and the open member draw it afresh
on("selfie", function (d) {
  if (num(obj(d).guid)) { forgetMemberShots(obj(d).guid); }  // its sets' marks are read again
  if (obj(d).guid === shownFace) { shownFace = null; }
  if (S.tab === "personas") { pollPersonas(); pollSelfies(); }
});
registerTab("personas", {
  show: function () {
    pollPersonas(); pollTuning(); pollSelfies();
    personasTimer = setInterval(function () { pollPersonas(); pollTuning(); pollSelfies(); }, PERSONAS_MS);
  },
  hide: function () { clearInterval(personasTimer); personasTimer = null; }
});
