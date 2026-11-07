import { arr, num, obj, plural, str } from "./core.mjs";

// How the Dojo tab words what /api/dojo holds. No DOM here: node imports this file.

export function shortHead(h) { return str(h).slice(0, 8); }

// "20261105_181500" -> "2026-11-05 18:15"
export function stampText(s) {
  var m = /^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})\d{2}$/.exec(str(s));
  return m ? m[1] + "-" + m[2] + "-" + m[3] + " " + m[4] + ":" + m[5] : str(s) || "-";
}

// The training realm's key figure: {v, d, cls}
export function realmKpi(realm) {
  var r = obj(realm), s = str(r.state);
  if (s === "running") { return { v: "Running", d: "pid " + num(r.pid) + " on port " + num(r.port), cls: "ok" }; }
  if (s === "stopped") { return { v: "Stopped", d: "nothing listens on port " + num(r.port), cls: "warn" }; }
  return { v: "Unknown", d: "the console cannot read the TCP table", cls: "" };
}

export function buildKpi(b) {
  b = obj(b);
  if (!str(b.variant) && !str(b.file)) { return { v: "-", d: "no build receipt" }; }
  if (str(b.why)) { return { v: "-", d: b.why }; }
  return { v: str(b.variant) || "-", d: b.ok ? "ok" : "not clean" };
}

export function gateKpi(gates) {
  var g = arr(gates).filter(function (x) { return obj(x).gate === "G1"; })[0];
  g = obj(g);
  var s = str(g.state);
  return { v: s ? s.charAt(0).toUpperCase() + s.slice(1) : "-", d: g.run ? stampText(g.run) : "no G1 run" };
}

// Episodes of the newest G1 run (runs come newest first)
export function episodesKpi(runs) {
  var r = arr(runs).filter(function (x) { return obj(x).kind === "g1"; })[0];
  if (!r) { return { v: "-", d: "no G1 run" }; }
  var eps = arr(r.episodes), usable = eps.filter(function (e) { return obj(e).usable === true; }).length;
  return { v: String(eps.length), d: usable + " usable" };
}

// A baseline's chip: {text, cls}
export function movedChip(m) {
  if (m === "same") { return { text: "same", cls: "ok" }; }
  if (m === "past") { return { text: "moved past", cls: "warn" }; }
  if (m === "diverged") { return { text: "diverged", cls: "warn" }; }
  return { text: "unknown", cls: "" };
}

export function gateChip(state) {
  if (state === "passed") { return "ok"; }
  if (state === "failed") { return "bad"; }
  if (state === "evidence") { return "warn"; }
  return "";
}

export function okText(ok) { return ok === true ? "ok" : ok === false ? "failed" : "-"; }

export function buildFacts(b) {
  b = obj(b);
  var v = obj(b.verified);
  return [["when", str(b.created_utc) || "-"], ["variant", str(b.variant) || "-"],
          ["exit code", b.exit_code === null || b.exit_code === undefined ? "-" : String(num(b.exit_code))],
          ["compiler errors", String(num(b.compiler_errors))],
          ["checks verified", num(v.passed) + " of " + num(v.total)],
          ["core", shortHead(b.core) || "-"], ["playerbots", shortHead(b.playerbots) || "-"]];
}

export function episodeText(e) {
  e = obj(e);
  var w = typeof e.winner === "number" ? "team " + e.winner + " won" : "no winner";
  return arr(e.policies).join(" vs ") + " · " + (str(e.outcome) || "no outcome") + " · " + w;
}

export function problemsText(b) {
  var p = arr(obj(b).problems);
  return p.length ? plural(p.length, "problem") + ": " + p.join("; ") : "";
}
