import { arr, obj, str } from "./core.mjs";

// The Heat panel's words. Everything a value means comes from the server's view (built by
// leeroy_mind.heat, the code the mind runs): this module only picks and joins it.
export const SESSIONS = { clean: "Keep it clean", open: "No limits", clear: "Back to the dial" };

function inRange(h, value) {
  return Number.isInteger(value) && value >= Number(obj(h).lo) && value <= Number(obj(h).hi);
}
export function worldText(h) {
  h = obj(h);
  if (h.set) { return "Heat " + h.world + ", set here."; }
  if (h.follows === null || h.follows === undefined) { return "Heat is not set, and mind.toml could not be read."; }
  return str(h.note) || "Heat is not set: the romance dial (" + h.follows + ") is in force exactly as before.";
}
export function rowText(h, value) {
  return inRange(h, value) ? str(arr(obj(h).rows)[value - Number(obj(h).lo)]) : "";
}
export function changeText(h, value) {
  return inRange(h, value) ? str(arr(obj(h).changes)[value - Number(obj(h).lo)]) : "";
}
export function sessionText(row) {
  row = obj(row);
  if (!row.mode) { return ""; }
  return (row.mode === "clean" ? "clean" : "no limits") + (row.ends === "logout" ? " until logout" : " until the morning");
}
export function sessionArgs(code, guid) {
  if (!Object.hasOwn(SESSIONS, code)) { throw new Error("Choose clean, open or clear."); }
  const args = { action: "heat", session: code };
  if (guid !== null && guid !== undefined) { args.guid = Number(guid); }
  return args;
}
export function memberHeat(h, guid) {
  const m = arr(obj(h).members).find(x => x.guid === guid);
  if (!m) { return null; }
  const session = sessionText(obj(obj(obj(h).sessions).members)[String(guid)]) || (m.session ? sessionText(obj(obj(h).sessions).all) : "");
  return { applies: m.applies === true, title: str(m.title), lines: arr(m.lines).map(str),
           caps: arr(m.caps).map(str), session, offset: Number(m.offset) || 0,
           pinned: m.pinned === null || m.pinned === undefined ? null : Number(m.pinned),
           ownDial: m.own_dial === null || m.own_dial === undefined ? null : Number(m.own_dial) };
}
export function overlayText(o) {
  o = obj(o);
  const head = str(o.file) + ": ";
  if (o.present) { return head + o.variants + (o.variants === 1 ? " variant" : " variants") + ", about " + o.tokens + " tokens"; }
  return head + "not there; reads " + str(o.reads);
}
export function photoText(p) {
  p = obj(p);
  if (!arr(p.outfits).length) { return ""; }
  return "Outfits: " + arr(p.outfits).join(", ") + ". Places: " + arr(p.settings).join(", ") + ". Poses: " + arr(p.views).join(", ") + ".";
}
