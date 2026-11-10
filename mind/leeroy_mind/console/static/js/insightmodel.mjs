import { arr, num, obj, str } from "./core.mjs";
export const DETECTORS = ["contradiction", "repetition", "nonsequitur", "unawareness"];
export function badges(value) {
  var v = obj(value), out = [];
  if (num(v.conflicts) > 0) { out.push({text: String(v.conflicts), title: "Potential open claim conflicts: live claims with opposite polarity on the same subject"}); }
  if (num(v.repetition) > 0) { out.push({text: String(v.repetition), title: "Repetition flags: this member's rate is above the guild's 90th percentile"}); }
  return out;
}
export function rate(value) {
  var v = obj(value);
  return typeof v.per_100_lines === "number" && isFinite(v.per_100_lines)
    ? v.per_100_lines.toFixed(1) + " / 100 (" + num(v.flagged_lines) + " of " + num(v.lines) + " lines)" : "Unavailable";
}
export function variant(value, budget) {
  var v = obj(obj(value).variants), chosen = obj(v[budget] || v.medium);
  return {text: str(chosen.text) || "No visible observations at this budget.",
          dropped: arr(chosen.dropped).filter(x => typeof x === "string").join(", ") || "none"};
}
