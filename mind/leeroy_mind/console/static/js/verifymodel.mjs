// Explicit ledger projection: feature names and evidence descriptions from features.toml, ids,
// counts, times and short codes. Never a raw log record, payload or arbitrary key.
import { at } from "./opsmodel.mjs";
const arr = x => Array.isArray(x) ? x : [];
const obj = x => x && typeof x === "object" && !Array.isArray(x) ? x : {};
const n = x => typeof x === "number" && Number.isFinite(x) ? String(x) : "unknown";
const str = x => typeof x === "string" ? x : "unknown";
const mb = x => typeof x === "number" && Number.isFinite(x) ? (x / 1048576).toFixed(1) + " MB" : "unknown size";

export const ORDER = ["failed", "seen_working", "unverified", "switched_off"];
export const LABEL = { failed: "Failed", seen_working: "Seen working", unverified: "Unverified", switched_off: "Switched off" };
const KIND = { working: "Working", failed: "Failed", ran: "Ran" };

export function summary(p) {
  const counts = obj(p.counts);
  const out = [arr(p.features).length + " shipped features · "
    + ORDER.map(s => n(counts[s]) + " " + LABEL[s].toLowerCase()).join(" · ")];
  Object.entries(obj(p.coverage)).forEach(([name, c]) => {
    const issues = Object.entries(obj(c.issues)).map(([k, v]) => k + " " + n(v)).join(", ");
    out.push("Logs: " + str(name) + " · " + n(c.files) + " files · " + mb(c.bytes) + " · back to " + at(c.from)
      + " · " + (c.complete === true ? "read whole" : "partial") + (issues ? " · " + issues : ""));
  });
  out.push("mind.db: " + str(p.database));
  if (typeof p.basis === "string") { out.push(p.basis); }
  return out;
}

// Failed first, then seen working, unverified and switched off; features.toml's order within each.
export function grouped(p) {
  return ORDER.flatMap(s => arr(p.features).filter(f => obj(f).status === s));
}

export function featureTitle(f) {
  const prs = arr(f.prs).filter(x => typeof x === "number").map(x => "#" + x).join(" ");
  return (LABEL[f.status] || "Unknown") + " · " + str(f.name) + (prs ? " · " + prs : "");
}

function span(e) {
  return e.first === e.last ? at(e.last) : at(e.first) + " to " + at(e.last);
}

export function featureLines(f) {
  const out = [str(f.id) + (typeof f.since === "number" ? " · evidence since " + at(f.since) : " · all retained evidence")];
  arr(f.switches).forEach(s => out.push("Switch " + str(s.key) + " · " + str(s.state)
    + " (configured " + str(s.configured) + ", default " + (s.default === true ? "on" : "off") + ")"));
  arr(f.evidence).forEach(e => {
    const codes = arr(e.codes).map(c => str(c.code) + " " + n(c.count)).join(", ");
    out.push((KIND[e.kind] || "Unknown") + " · " + str(e.what) + " · "
      + (e.available === false ? "unavailable: " + str(e.why)
        : e.count > 0 ? n(e.count) + " · " + span(e) + (codes ? " · " + codes : "") : "none seen"));
  });
  if (typeof f.note === "string" && f.note) { out.push(f.note); }
  return out;
}
