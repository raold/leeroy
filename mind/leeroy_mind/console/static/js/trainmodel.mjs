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

var KIND_WORDS = {
  avoid_ground: "avoid the ground", move_out_cone: "out of the cone", spread: "spread", stack: "stack",
  soak: "soak", run_out_debuff: "run out", interrupt: "interrupt", dispel: "dispel",
  tank_swap_at_stacks: "tank swap", switch_adds: "switch to adds", kill_order: "kill order",
  los: "line of sight", face_away: "face away", positional: "position", kite: "kite",
  raid_cooldowns: "cooldowns", use_object: "use it", heal_target: "heal it", enrage_timer: "enrage",
  stop_casting: "stop casting"
};

// The raid playbooks (/api/training/playbooks) as words: one group per raid, one line per boss
export function playbookGroups(v) {
  return arr(obj(v).raids).map(function (r) {
    r = obj(r);
    return {
      title: str(r.name) + " (" + arr(r.difficulties).join(", ") + ")",
      bosses: arr(r.bosses).map(function (b) {
        b = obj(b);
        var mechs = arr(b.mechanics), stock = mechs.filter(function (m) { return obj(m).handled_by === "playerbots"; }).length;
        var partial = mechs.filter(function (m) { return obj(m).handled_by === "partial"; }).length;
        return {
          title: str(b.name),
          detail: plural(mechs.length, "mechanic") + ", " + stock + " handled by playerbots" + (partial ? ", " + partial + " partly" : ""),
          call: str(b.call),
          mechanics: mechs.map(function (m) {
            m = obj(m);
            var only = arr(m.only).length ? " · only " + arr(m.only).join(", ") : "";
            return {
              title: str(m.name),
              tag: KIND_WORDS[m.kind] || str(m.kind),
              who: arr(m.roles).join(", ") + only,
              stock: m.handled_by === "playerbots" ? "playerbots" : m.handled_by === "partial" ? "partly playerbots" : "",
              note: str(m.handled_note),
              text: str(m.explain)
            };
          })
        };
      })
    };
  });
}

export function playbookTotals(v) {
  var t = obj(obj(v).totals);
  return plural(num(t.raids), "raid") + " · " + plural(num(t.bosses), "boss", "bosses") + " · "
    + plural(num(t.mechanics), "mechanic") + " · " + num(t.playerbots) + " handled by playerbots, " + num(t.partial) + " partly · "
    + plural(num(t.scored), "check row") + " can fail by default (" + num(t.scored_playbooks) + " with playbook checks on)";
}
