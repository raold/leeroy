// Explicit report projection: never render raw log records, payloads or arbitrary keys.
import { at } from "./opsmodel.mjs";
const arr = x => Array.isArray(x) ? x : [];
const n = x => typeof x === "number" && Number.isFinite(x) ? String(x) : "unknown";
const str = x => typeof x === "string" ? x : "unknown";

export function overview(p) {
  if (!p.raid) { return ["No recorded raid selected.", ...arr(p.issues).map(str)]; }
  const r = p.raid;
  return [str(r.name) + " · raid #" + n(r.id) + (r.offline ? " · offline" : " · with you"),
    "Started " + at(r.started) + " · ended " + at(r.ended) + " · " + str(r.outcome),
    n(r.kills) + " stored kills · " + n(r.wipes) + " stored wipes. Latest completed raid; refreshes at most once a minute.",
    "Older raid: training debrief --raid " + n(r.id) + " (substitute its saved ID).",
    ...Object.values(p.evidence || {}).filter(x => typeof x === "string"),
    "Log files read " + n(p.logs?.files) + " · matched records " + n(p.logs?.records) + " · " + str(p.logs?.note),
    "Native clock: " + str(p.logs?.native_timezone),
    ...Object.entries(p.logs?.issues || {}).map(([k,v]) => "Log coverage: " + k + " (" + n(v) + ")"),
    ...arr(p.issues).map(x => "Coverage: " + str(x))];
}
export function bossTitle(b) {
  return str(b.boss) + " · difficulty " + n(b.difficulty) + " · " + n(b.pulls) + " pulls · " + n(b.kills) + " kills · " + n(b.wipes) + " wipes";
}
export function bossLines(b) {
  const out = [];
  if (!b.pulls) { out.push("No recorded pull: unverified."); }
  out.push(n(b.incomplete) + " incomplete recordings · " + n(b.aborted) + " aborted pulls");
  arr(b.checks).forEach(x => out.push("Check coverage: " + str(x)));
  arr(b.mechanics).forEach(m => out.push(str(m.name) + ": " + (m.evidence === "response_logged"
    ? n(m.activations) + " responder activations · " + n(m.moved_total) + " move participations · max " + n(m.moved_max) + " bots per activation · " + n(m.cancelled) + " cancelled"
    : m.evidence === "no_direct_action_telemetry" ? "no direct action telemetry; success unverified"
    : "no responder record; unverified")));
  arr(b.hits).forEach(h => out.push("Member #" + n(h.guid) + " · spell " + n(h.spell) + " · " + arr(h.mechanics).map(str).join(", ") + " · " + n(h.hits) + " hits · " + n(h.requested) + " requested damage"));
  arr(b.failures).forEach(f => out.push("Proven check failure · member #" + n(f.guid) + " · " + str(f.kind) + " / " + str(f.key) + " · " + n(f.count)));
  arr(b.deaths).forEach(d => out.push("Deaths · member #" + n(d.guid) + " · spell " + n(d.spell) + " · " + n(d.count)));
  return out;
}
export function unassignedLines(p) {
  const out = ["These log observations lack a unique boss association. Counts are retained records, not totals for the encounter."];
  arr(p.group_rez).forEach(r => out.push("group_rez_blocked · " + str(r.reason) + " · fence " + str(r.fence) + " · " + n(r.records) + " records · max dead " + n(r.max_dead) + " · max casters " + n(r.max_casters) + " · last " + at(r.last)));
  if (!arr(p.group_rez).length) { out.push("No group_rez_blocked record found; success unverified."); }
  arr(p.hazards).forEach(h => out.push(str(h.event) + (h.guid ? " · member #" + n(h.guid) : " · global") + (h.reason ? " · " + str(h.reason) : "") + " · " + n(h.records) + " records"
    + ["aware", "zones", "spot_members", "detour_members", "exit_members", "readds"].filter(k => typeof h.maxima?.[k] === "number").map(k => " · max " + k + " " + n(h.maxima[k])).join("")));
  arr(p.unassigned_responses).forEach(r => out.push("Unassigned responder · " + str(r.id) + " · spell " + n(r.spell) + " · " + n(r.members) + " bots issued movement · " + at(r.at)));
  return out;
}
export function offlineLines(p) {
  const fields = ["reason", "cause", "outcome", "state"];
  const numbers = ["dead", "inside", "restarts", "kills", "wipes", "present", "min", "members", "idle_s"];
  return arr(p.offline).map(e => at(e.at) + " · " + str(e.source) + " · " + str(e.event)
    + fields.filter(k => typeof e[k] === "string").map(k => " · " + k + " " + e[k]).join("")
    + numbers.filter(k => typeof e[k] === "number").map(k => " · " + k + " " + n(e[k])).join("")
    + (typeof e.leader_present === "boolean" ? " · leader present " + String(e.leader_present) : ""));
}
