import { arr, obj, str } from "./core.mjs";

// How the Calendar tab words what /api/calendar holds. No DOM here: node imports this file.

var KINDS = { raid: "Raid night", meeting: "Officer meeting", birthday: "Birthday",
              anniversary: "Guild anniversary", offline: "Offline raid" };
var WEEK_S = 7 * 86400;

export function kindWord(k) { return KINDS[k] || str(k); }

function pad(n) { return (n < 10 ? "0" : "") + n; }

// Events grouped by local day, in time order: [{day: "YYYY-MM-DD", events: [...]}]
export function dayGroups(events) {
  var out = [];
  arr(events).slice().sort(function (a, b) { return a.at - b.at; }).forEach(function (e) {
    var d = new Date(e.at * 1000), day = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
    if (!out.length || out[out.length - 1].day !== day) { out.push({ day: day, events: [] }); }
    out[out.length - 1].events.push(e);
  });
  return out;
}

export function nextRaid(upcoming, now) {
  var r = arr(upcoming).filter(function (e) { return e.kind === "raid" && e.at > now; });
  return r.length ? r[0] : null;
}

export function birthdaysThisWeek(upcoming, now) {
  return arr(upcoming).filter(function (e) { return e.kind === "birthday" && e.at >= now && e.at <= now + WEEK_S; }).length;
}

export function signupCounts(s) {
  s = obj(s);
  var out = [];
  [["signed_up", "signed up"], ["tentative", "tentative"], ["declined", "declined"], ["other", "other"]].forEach(function (p) {
    var n = arr(s[p[0]]).length;
    if (n) { out.push(n + " " + p[1]); }
  });
  return out;
}

export function youText(status) { return status ? "you: " + str(status).replace("_", " ") : ""; }
