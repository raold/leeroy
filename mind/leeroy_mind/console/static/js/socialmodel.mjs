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
