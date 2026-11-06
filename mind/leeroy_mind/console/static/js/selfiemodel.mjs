import { arr, num, obj, plural, str } from "./core.mjs";
import { when } from "./words.mjs";

// The Personas tab's profile photos in words: the "Choose selfies" card from /api/selfies
// ({view, mind, control}) and a member's Profile photo from /api/selfies/member. No DOM here: node
// imports this file. The recent choices are the running mind's own list (mind.recent), never the
// view's, since only the mind can say whether an undo would be taken now.

export var PAGE = 5;    // members a page
export var RECENT = 8;  // recent choices shown
export var MIND_DOWN = "The mind isn't running, so it can't set photos. Start it from the Mind tab.";
export var MORE_TITLE = "Pass on these and render four new ones tonight";
export var NO_PHOTO = "No photo yet: a coloured initials circle shows instead.";

export function countsText(counts) {
  var c = obj(counts);
  var bits = [num(c.chosen) + " chosen", num(c.waiting) + " waiting", num(c.queued) + " rendering tonight",
              num(c.no_looks) + " no looks yet"];
  if (num(c.no_kind)) { bits.push(num(c.no_kind) + " no photo kind yet"); }
  bits.push(num(c.no_persona) + " no persona");
  return bits.join(" · ");
}

export function leadText(view) {
  var v = obj(view), n = num(obj(v.counts).waiting);
  if (!v.available) {
    return "This mind.db has no photo tables yet (" + arr(v.missing).join(", ") + "): `db migrate` with the mind stopped adds them.";
  }
  if (!n) { return "No photos are waiting. The night renders four for every member without one."; }
  return plural(n, "member has", "members have") + " four photos waiting. Click the one that fits; it becomes their photo everywhere.";
}

// Why a pick, undo or "4 more" cannot be sent now ("" when it can): the mind down (or without
// photos), else control off
export function whyOff(sel) {
  if (!sel) { return "Reading the photos."; }
  var mind = obj(sel.mind), control = obj(sel.control);
  if (mind.ok !== true) { return str(mind.error) || MIND_DOWN; }
  if (control.enabled === false) { return str(control.reason) || "control is off"; }
  return "";
}

// One page of the queue (at counts from 0, kept in range) and what it shows of the whole
export function pageOf(queue, at) {
  var q = arr(queue), pages = Math.max(1, Math.ceil(q.length / PAGE));
  at = Math.min(Math.max(0, Math.floor(num(at))), pages - 1);
  var first = at * PAGE, last = Math.min(q.length, first + PAGE);
  return { rows: q.slice(first, last), at: at, pages: pages,
           text: pages > 1 ? "members " + (first + 1) + "-" + last + " of " + q.length : "" };
}

export function pickLabel(index, name) { return "Use photo " + (index + 1) + " of 4 for " + name; }

function changeText(label) {
  label = str(label);
  if (label === "pick") { return "photo picked"; }
  return label.indexOf("undo #") === 0 ? "undo of " + label.slice(5) : label;
}
function row(r, who) {
  return { id: num(r.id), undoable: r.undoable === true,
           text: ["#" + num(r.id), when(r.ts)].concat(who ? [who] : [], [changeText(r.label)]).join(" · ") };
}

// The running mind's newest changes, named; null while the mind cannot be asked
export function recentRows(sel, names) {
  var mind = obj(obj(sel).mind), n = obj(names);
  if (mind.ok !== true) { return null; }
  return arr(mind.recent).slice(0, RECENT).map(function (r) {
    return row(r, str(n[String(r.guid)]) || "#" + num(r.guid));
  });
}

// A member's photo now, from /api/selfies/member
export function currentText(member) {
  if (!member) { return ""; }
  var c = member.current;
  if (!c) { return NO_PHOTO; }
  return "Photo " + (num(c.index) + 1) + " of 4 from set " + num(c.set)
    + (c.pick ? ", chosen by change #" + num(c.pick) : "") + ".";
}

export function historyRows(member) {
  return arr(obj(member).history).map(function (r) { return row(r, ""); });
}
