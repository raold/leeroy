import { arr, num, obj, str } from "./core.mjs";

// How the Realm tab words what /api/realm holds. No DOM here: node imports this file.

var ROLES = { progression: "progression rival", gankers: "gankers", drama: "drama farm" };
var WEEK_S = 7 * 86400;

export function roleWord(r) { return ROLES[r] || str(r); }

// "1 tank, 2 healers, 0 melee, 3 ranged, item level 247"; "no raiders" for none
export function raidersText(r) {
  r = obj(r);
  var parts = [["tank", "tank", "tanks"], ["healer", "healer", "healers"], ["melee", "melee", "melee"],
               ["ranged", "ranged", "ranged"]].map(function (p) {
    var n = num(r[p[0]]);
    return n + " " + (n === 1 ? p[1] : p[2]);
  });
  var total = num(r.tank) + num(r.healer) + num(r.melee) + num(r.ranged);
  return total ? parts.join(", ") + ", item level " + num(r.ilvl) : "no raiders";
}

// A rival's standing on one boss: {text, pct}; pct is set only while it is progressing
export function standing(p, dateText) {
  p = obj(p);
  if (p.state === "killed") { return { text: "killed " + dateText(p.killed_at), pct: null }; }
  if (p.state === "progressing") { return { text: "best " + num(p.best_pct) + "%", pct: Math.max(0, Math.min(100, num(p.best_pct))) }; }
  return { text: "not started", pct: null };
}

export function poachText(p, nameOf) {
  p = obj(p);
  if (!p.officer || !p.member) { return "No offer this week."; }
  var guild = str(p.guild_name) || "guild #" + num(p.guild);
  return (str(p.officer_name) || nameOf(p.officer)) + " has offered " + nameOf(p.member) + " a place in " + guild + ".";
}

export function tradeCount(lines, now) {
  return arr(lines).filter(function (l) { return num(l.ts) >= now - WEEK_S; }).length;
}

export function firstKills(race) {
  return arr(race).filter(function (r) {
    return Object.keys(obj(r.rivals)).some(function (g) { return obj(r.rivals[g]).state === "killed"; });
  }).length;
}
