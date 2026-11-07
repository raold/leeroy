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

export function feudText(f, names) {
  f = obj(f);
  var n = obj(names);
  function name(g) { return str(n[String(g)]) || "#" + g; }
  return name(f.a) + " vs " + name(f.b) + (f.cause ? ": " + f.cause : "");
}
