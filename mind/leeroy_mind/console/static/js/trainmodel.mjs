import { arr, num, obj, plural, str } from "./core.mjs";

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

var GATE_PHASES = { ab: "Measuring", passed: "Passed", failed: "Failed" };

// The overhead gate: {v, d, ok}. Its phase is the gate's own verdict against its configured limit
export function gateText(g) {
  g = obj(g);
  var ms = typeof g.verdict_ms === "number" ? g.verdict_ms.toFixed(2) + " ms" : "";
  var d = g.phase === "ab" ? "no verdict yet (" + plural(num(g.raids), "raid") + " measured)"
        : g.phase === "failed" ? (ms ? ms + ", " : "") + "over the gate's limit" : ms || "no verdict";
  return { v: GATE_PHASES[g.phase] || str(g.phase) || "-", d: d, ok: g.passed === true };
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

// A trial's or a card group's scope as its boss and mode (trainview's _scope); the raw scope without them
export function scopeText(x) {
  x = obj(x);
  return (str(x.boss) || str(x.scope)) + (x.mode ? " · " + str(x.mode) : "");
}

export function trialText(t) {
  t = obj(t);
  return plural(num(t.arms), "arm") + ", " + plural(num(t.observations), "observation");
}

// -- what hurt them (the `hurt` report): each boss's numbers as short lines ------------------------

// A spell by its stored name, else its id
export function spellText(s) {
  s = obj(s);
  return str(s.name) || "spell " + num(s.spell);
}

function who(m) {
  m = obj(m);
  return str(m.name) || "#" + num(m.guid);
}

// The boss's heading line: pulls, kills, wipes, kinds and deaths a pull
export function hurtHead(c) {
  c = obj(c);
  var kinds = Object.keys(obj(c.by_kind)).sort().map(function (k) { return k + " " + num(c.by_kind[k]); });
  var parts = [plural(num(c.pulls), "pull") + " (" + plural(num(c.kills), "kill") + ", " + plural(num(c.wipes), "wipe") + ")"];
  if (kinds.length) { parts.push(kinds.join(", ")); }
  var deaths = obj(c.deaths);
  parts.push((typeof deaths.per_pull === "number" ? deaths.per_pull.toFixed(2) : "-") + " deaths a pull");
  if (num(c.incomplete_pulls)) { parts.push(plural(num(c.incomplete_pulls), "incomplete pull") + " not read"); }
  return parts.join(" · ");
}

// Deaths by killing spell: "Vile Gas ×9 (6 members)"; "" when nobody died
export function deathsBy(c, n) {
  return arr(obj(obj(c).deaths).by_spell).slice(0, n || 3).map(function (d) {
    return spellText(d) + " ×" + num(d.deaths) + " (" + plural(num(d.members), "member") + ")";
  }).join(", ");
}

// The spells that hit the cast hardest, as shares of recorded (requested) damage
export function hitHardest(c, n) {
  return arr(obj(obj(c).damage_taken).top).slice(0, n || 3).map(function (t) {
    var share = typeof t.share === "number" ? " " + Math.round(t.share * 100) + "%" : "";
    return spellText(t) + share;
  }).join(", ");
}

// Failed mechanics the checks proved, with the member who failed each most
export function mechanicsText(c, n) {
  return arr(obj(c).mechanics).slice(0, n || 3).map(function (m) {
    var worst = arr(m.worst)[0];
    return str(m.key) + " (" + str(m.kind) + ") ×" + num(m.count) + (worst ? ", most " + who(worst) + " ×" + num(worst.count) : "");
  }).join("; ");
}

// Avoidable effects: who the records show hit, and how often past the allowance
export function avoidText(c, n) {
  return arr(obj(c).avoidable).filter(function (a) { return num(a.hits) > 0 || !a.resolved; }).map(function (a) {
    var label = arr(a.names).join(" / ");
    if (!a.resolved) { return label + ": spell not named yet"; }
    return label + ": " + arr(a.members).slice(0, n || 3).map(function (m) {
      return who(m) + " " + plural(num(m.hits), "hit") + (num(m.over_allowance) ? " (" + num(m.over_allowance) + " over)" : "");
    }).join(", ");
  });
}
