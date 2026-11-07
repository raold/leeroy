import { num, obj, plural, str } from "./core.mjs";

// How the Training tab words what /api/training holds. No DOM here: node imports this file.

export var FOUR_WEEKS = 28;

// The "days until tactics may adapt" figure: {v, d}
export function adaptText(v) {
  v = obj(v);
  if (v.days_left === null || v.days_left === undefined) { return { v: "-", d: "no fights recorded yet" }; }
  if (num(v.days_left) === 0) {
    return { v: FOUR_WEEKS + " days observed", d: v.four_weeks ? "the four-week milestone is met" : "the four-week milestone is not met yet" };
  }
  return { v: Math.max(0, Math.round(num(v.days_observed))) + " of " + FOUR_WEEKS + " days", d: plural(num(v.days_left), "day") + " to go" };
}

// The overhead gate: {v, d, ok}
export function gateText(g) {
  g = obj(g);
  var ms = typeof g.verdict_ms === "number" ? g.verdict_ms.toFixed(2) + " ms" : "no verdict";
  return { v: g.passed ? "Passed" : str(g.phase) || "-", d: ms + (g.passed ? "" : " (needs a pass at 1 ms or less)"), ok: g.passed === true };
}

// A boss's change against its baseline, as words
export function changeText(c) {
  c = obj(c);
  var d = typeof c.deaths_change === "number" ? " (" + (c.deaths_change > 0 ? "+" : "") + c.deaths_change.toFixed(2) + " deaths a pull)" : "";
  return (str(c.change) || "too few") + d;
}

export function deathsText(c) {
  c = obj(c);
  return typeof c.deaths_per_pull === "number" ? c.deaths_per_pull.toFixed(2) : "-";
}

// What the checks did not cover, or why nothing is said
export function coverageText(c) {
  c = obj(c);
  var gaps = Array.isArray(c.checks_coverage) ? c.checks_coverage : [];
  if (gaps.length) { return "not covered: " + gaps.join(", "); }
  return num(c.checks_complete_pulls) >= num(c.pulls) ? "fully covered" : "partly covered";
}

// The report's evidence as notes: every string it holds that says what the numbers do not prove
export function evidenceNotes(e) {
  e = obj(e);
  var out = [];
  ["scope", "tactic", "rotation", "spread", "mentees"].forEach(function (k) { if (typeof e[k] === "string" && e[k]) { out.push(e[k]); } });
  if (typeof e.schema === "string" && e.schema !== "available") { out.push("schema " + e.schema); }
  var cut = Array.isArray(e.truncated) ? e.truncated : [];
  if (cut.length) { out.push("shortened to the limit: " + cut.join(", ")); }
  var gone = Array.isArray(e.unavailable_sections) ? e.unavailable_sections : [];
  if (gone.length) { out.push("not available: " + gone.join(", ")); }
  var voice = obj(e.voice);
  if (voice.audible_delivery) {
    out.push("voice: audible delivery " + voice.audible_delivery + ", facts " + voice.fact_delivery + ", cast coverage " + voice.cast_coverage);
  }
  return out;
}

export function trialText(t) {
  t = obj(t);
  return plural(num(t.arms), "arm") + ", " + plural(num(t.observations), "observation");
}
