import { $, api, arr, clear, el, emit, on, post, refused, setText, str } from "./core.mjs";
import { S } from "./state.mjs";
import { controlReason, decisionRow, pollModel, sectionHidden } from "./decisionsmodel.mjs";

// -- the guild's decisions: the Guild tab's ledger and the poll cards in Comms ------------------

export function castVote(poll, option) {
  return post("/api/decision", { action: "vote", poll: poll, option: option }).then(function (j) {
    if (!j.ok) { throw new Error(str(j.error) || "Your vote was not saved."); }
    emit("decision-saved", { action: "vote", id: poll });
    return j;
  });
}

export function overruleDecision(id) {
  return post("/api/decision", { action: "overrule", id: id }).then(function (j) {
    if (!j.ok) { throw new Error(str(j.error) || "That overrule was not saved."); }
    emit("decision-saved", { action: "overrule", id: id });
    return j;
  });
}

function button(text, cls) { var b = el("button", cls || "", text); b.type = "button"; return b; }

// The card a Discord message carries for its poll (comms/discordview.mjs draws it)
export function pollCard(poll, reason, busy) {
  var m = pollModel(poll, reason), box = el("section", "dc-poll");
  box.setAttribute("aria-label", "Poll: " + m.question);
  box.appendChild(el("b", "", m.question));
  m.options.forEach(function (o) {
    var b = button(o.label + " · " + o.count + (o.mine ? " (your vote)" : "") + (o.winner ? " (won)" : ""), "dc-poll-option" + (o.mine ? " on" : ""));
    b.dataset.action = "poll-vote"; b.dataset.poll = String(m.id); b.dataset.option = o.code;
    b.dataset.focus = "poll-" + m.id + ":" + o.code; b.disabled = !m.canVote || !!busy;
    b.setAttribute("aria-pressed", String(o.mine));
    b.style.setProperty("--share", o.share + "%");
    box.appendChild(b);
  });
  box.appendChild(el("small", "muted", m.votes + " of " + m.voters + " voted" + (m.closes && m.open ? " · closes " + m.closes : "") + (m.why ? " · " + m.why : "")));
  return box;
}

var busy = false, failure = "", last = null;

function renderDecisions(view) {
  last = view;
  var panel = $("g-decisions-panel");
  panel.hidden = sectionHidden(view);
  if (panel.hidden) { return; }
  var reason = controlReason(S.status), body = $("g-decisions");
  clear(body);
  arr(view.decisions).forEach(function (raw) {
    var d = decisionRow(raw), tr = el("tr");
    [d.label + (d.officers ? " (officers)" : ""), d.summary, d.state, d.settler, d.settled, d.effect, d.changed]
      .forEach(function (t) { tr.appendChild(el("td", "", t)); });
    var td = el("td");
    if (d.positions) { td.title = d.positions; }
    if (d.action) {
      var b = button(d.action.label);
      b.dataset.decision = String(d.id); b.title = d.action.words; b.disabled = busy || !!reason;
      b.setAttribute("aria-label", d.action.label + ": " + d.label + ", " + d.summary);
      td.appendChild(b);
      if (d.action.kind === "overrule" && d.action.words) { td.appendChild(el("small", "muted", " " + d.action.words)); }
    }
    tr.appendChild(td);
    body.appendChild(tr);
  });
  if (!arr(view.decisions).length) {
    var tr = el("tr"), td = el("td", "muted", "The guild has not decided anything yet.");
    td.colSpan = 8; tr.appendChild(td); body.appendChild(tr);
  }
  var polls = $("g-polls");
  clear(polls);
  arr(view.polls).forEach(function (p) { polls.appendChild(pollCard(p, reason, busy)); });
  if (!arr(view.polls).length) { polls.appendChild(el("p", "muted", "No polls yet.")); }
  setText("g-decisions-why", failure || reason || (arr(view.missing).length ? "The mind needs the decisions update." : ""));
}

export function pollDecisions() {
  if (S.tab !== "guild") { return Promise.resolve(); }
  return api("/api/decisions", { poll: true }).then(function (r) {
    if (refused(r)) { return null; }
    return r.json().then(function (j) { return r.status === 200 ? j : null; });
  }).then(function (j) { if (j) { renderDecisions(j); } }, function () { /* the strip says it */ });
}

function act(promise) {
  busy = true; failure = ""; if (last) { renderDecisions(last); }
  return promise.catch(function (error) { failure = str(error.message); emit("note", failure); })
    .finally(function () { busy = false; pollDecisions(); });
}

$("g-decisions").addEventListener("click", function (ev) {
  var b = ev.target.closest("[data-decision]");
  if (b && !b.disabled) { act(overruleDecision(Number(b.dataset.decision))); }
});
$("g-polls").addEventListener("click", function (ev) {
  var b = ev.target.closest('[data-action="poll-vote"]');
  if (b && !b.disabled) { act(castVote(Number(b.dataset.poll), b.dataset.option)); }
});
on("decision", function () { pollDecisions(); emit("discord"); });
