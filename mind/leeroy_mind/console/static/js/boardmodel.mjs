import { arr, num, obj, str } from "./core.mjs";

// How the Board tab words what /api/board holds. No DOM here: node imports this file.

export function postCount(forum) {
  return arr(forum).reduce(function (n, t) { return n + arr(obj(t).posts).length; }, 0);
}

// The empty-state test: nothing posted, written or delivered yet
export function boardEmpty(v) {
  v = obj(v);
  return !arr(v.forum).length && !arr(v.letters).length && !arr(v.writeups).length;
}

export function qualityClass(q) {
  return "q" + Math.max(0, Math.min(5, typeof q === "number" ? Math.floor(q) : 1));
}

export function itemText(it) {
  it = obj(it);
  var n = num(it.count);
  return str(it.name) + (n > 1 ? " x" + n : "");
}

export function minutes(w) {
  w = obj(w);
  return Math.max(0, Math.round((num(w.end) - num(w.start)) / 60));
}

export var REALM_OFF = "The Realm is off in mind.toml, so nothing new is posted.";
export var NOTHING = "the Realm has written no forum posts, letters or write-ups.";
