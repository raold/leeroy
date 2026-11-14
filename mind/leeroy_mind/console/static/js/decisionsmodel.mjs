import { arr, num, obj, str } from "./core.mjs";
import { when } from "./words.mjs";

// How the page words the guild's decisions and polls (decisions.mjs draws them). No DOM here:
// node imports this file.

var STATUS_WORDS = { open: "open", closed: "closed", cancelled: "cancelled" };

export function controlReason(status) {
  status = obj(status);
  if (status.control && status.control.enabled === false) { return "Decisions are read-only in this console."; }
  if (!status.mind || status.mind.state !== "running") { return "The mind must be running to save a vote or an overrule."; }
  return "";
}

function words(code) { return str(code).replace(/_/g, " "); }

// One ledger row as the page shows it; an action only where the mind's view offered one
export function decisionRow(d) {
  d = obj(d);
  var action = obj(d.action), kind = action.kind === "undo" || action.kind === "overrule" ? action.kind : "";
  return {
    id: num(d.id), label: str(d.label) || words(d.kind), summary: str(d.summary), state: str(d.state) || words(d.status),
    settler: str(d.settler) || "-", settled: d.settled ? when(d.settled) : "-", effect: d.effect ? when(d.effect) : "-",
    changed: arr(d.changed).map(str).filter(Boolean).join("; ") || "-",
    positions: arr(d.positions).map(function (p) {
      p = obj(p);
      return (str(p.name) || "#" + num(p.guid)) + ": " + words(p.side) + " (" + words(p.why) + ")";
    }).join(", "),
    officers: d.officers_only === true,
    action: kind ? { kind: kind, label: str(action.label) || (kind === "undo" ? "Undo" : "Overrule"),
                     words: str(action.words) } : null
  };
}

// A poll as a card: each option with its count and share, his vote, and whether he can vote now
export function pollModel(poll, reason) {
  poll = obj(poll);
  var options = arr(poll.options).map(obj), total = options.reduce(function (n, o) { return n + num(o.count); }, 0);
  var mine = typeof poll.his_vote === "string" ? poll.his_vote : null, open = poll.status === "open" && poll.open !== false;
  var why = !open ? "This poll has " + (STATUS_WORDS[poll.status] || "ended") + "." :
    mine ? "You voted; a vote never changes." : poll.he_votes === false ? "You are not a voter in this poll." : str(reason);
  return {
    id: num(poll.id), question: str(poll.question), channel: str(poll.channel),
    options: options.map(function (o) {
      var count = num(o.count);
      return { code: str(o.code), label: words(o.code), count: count, mine: o.code === mine,
               share: total ? Math.round(100 * count / total) : 0, winner: !open && poll.result === o.code };
    }),
    votes: total, voters: num(poll.voters), open: open, mine: mine, canVote: open && !mine && poll.he_votes !== false && !reason,
    why: why, closes: poll.closes ? when(poll.closes) : ""
  };
}

export function sectionHidden(view) {
  view = obj(view);
  return view.enabled !== true && !arr(view.decisions).length && !arr(view.polls).length;
}
