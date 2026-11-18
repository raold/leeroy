import { num, obj, str } from "./core.mjs";

// How the Social tab words what /api/social holds. No DOM here: node imports this file.

export function stageText(s) { return s && s !== "none" ? str(s) : ""; }

export function romanceText(r, nameA, nameB) {
  r = obj(r);
  return nameA + ": " + (stageText(r.ab) || "-") + " · " + nameB + ": " + (stageText(r.ba) || "-");
}

// What is going on between a member and him, as chips: {kind, text, at} (at: a time to show, or 0)
export function feelings(y, now) {
  y = obj(y);
  var out = [];
  if (num(y.together_since) > 0) { out.push({ kind: "together", text: "together", at: y.together_since }); }
  if (num(y.hurt_until) > now) { out.push({ kind: "hurt", text: y.hurt_deep ? "deeply hurt" : "hurt", at: y.hurt_until }); }
  if (num(y.rush_until) > now) { out.push({ kind: "rush", text: "rush", at: y.rush_until }); }
  return out;
}

// A member romance's one stage (drama.couples), in the guild's words.
var MEMBER_STAGE = { notices: "a spark", banter: "flirting", whispers: "a crush", late_nights: "dating",
  confession: "together", ex: "broke up" };
export function memberStageText(s) { return MEMBER_STAGE[str(s)] || stageText(s); }

// Jealousy is stored 0-100; a meter wants 0-1.
export function jealousyShare(level) { return Math.max(0, Math.min(1, num(level) / 100)); }

// Who left whom, and for whom.
export function breakupText(b, names) {
  b = obj(b);
  var n = obj(names);
  function name(g) { return str(n[String(g)]) || "#" + g; }
  return name(b.woman) + " left " + name(b.partner) + " for " + name(b.for);
}

export function feudText(f, names) {
  f = obj(f);
  var n = obj(names);
  function name(g) { return str(n[String(g)]) || "#" + g; }
  return name(f.a) + " vs " + name(f.b) + (f.cause ? ": " + f.cause : "");
}

// -- threads (recording): what would pull members together. Nothing here is in effect. ------------

export var THREADS_NOTE = "Recording only, not in effect: this is what would have pulled members together. " +
  "Nothing here changes how anyone talks, feels or behaves.";

var KIND_WORDS = { raided: "raided together", grouped_away: "grouped without you", kill_alive: "stood at a kill",
  first_kill: "stood at a first kill", wipe_stood: "last standing at a wipe", partied: "in your party",
  warm_scene: "a warm scene", took_side: "took the other's side", heard: "was agreed with", defended: "was defended",
  answered: "was replied to", replied: "replied", talked: "talked back and forth", mentioned: "was named",
  reacted: "reacted", banter: "banter that landed", joked: "joked together", complimented: "was complimented",
  spoke_well: "was spoken well of", praised: "spoke well of the other", admired: "admired the other",
  voice_room: "a small voice room", voice_late: "a late voice room", dkp_close: "a close DKP award",
  dkp_respect: "respect after a close award" };
export function kindText(k) { return KIND_WORDS[str(k)] || str(k); }

var WHY_WORDS = { draw: "the night's draw said not yet", affinity: "one dislikes the other",
  social: "only time spent together so far", feud: "the two are feuding", ex: "exes, too soon",
  pair_open: "the pair already has a thread", cap_member: "one of them is already in a thread",
  cap_live: "enough threads are live", cap_taken: "enough threads with a taken member are live",
  cap_night: "one new thread a night" };
export function whyText(w) { return WHY_WORDS[str(w)] || str(w); }

var SHAPE_WORDS = { unrequited: "unrequited", mutual_secret: "mutual, a secret", mutual_open: "mutual, in the open",
  triangle: "a triangle", slow_burn: "a slow burn" };
export function shapeText(s) { return SHAPE_WORDS[str(s)] || str(s); }

function counts(o, word) {
  o = obj(o);
  return Object.keys(o).sort(function (a, b) { return num(o[b]) - num(o[a]) || (a < b ? -1 : 1); })
    .map(function (k) { return num(o[k]) + " " + word(k); });
}

// The panel's summary line.
export function threadsHead(t) {
  t = obj(t);
  var nights = Array.isArray(t.nights) ? t.nights.length : 0;
  if (!nights) { return "No night recorded yet."; }
  var open = Array.isArray(t.open) ? t.open.length : 0;
  return nights + (nights === 1 ? " night" : " nights") + " recorded · " + num(t.events) + " events · " +
    num(t.pairs) + " pairs with a pull · " + open + " would-be " + (open === 1 ? "thread" : "threads");
}

// One recorded night: its events by kind, most first.
export function threadNightText(n) {
  n = obj(n);
  var kinds = counts(n.kinds, kindText);
  return num(n.events) + " events" + (kinds.length ? ": " + kinds.join(", ") : "");
}

// A shadow pull's chips, in order: its would-be thread, its shape, taken, the ceiling.
export function pullChips(t) {
  t = obj(t);
  var out = [];
  if (t.state === "open") { out.push("would be open"); }
  if (t.state === "waiting") { out.push("held back: " + whyText(t.why)); }
  if (t.state === "faded") { out.push("would have faded"); }
  if (t.shape) { out.push(shapeText(t.shape)); }
  if (t.taken) { out.push("one is taken"); }
  if (t.ceiling) { out.push("never past " + str(t.ceiling)); }
  return out;
}

// Why candidates were held back over the nights shown.
export function heldText(waiting) {
  var parts = counts(waiting, whyText);
  return parts.length ? parts.join(" · ") : "";
}
