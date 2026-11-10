import { arr, obj, str } from "./core.mjs";
import { cleanReply } from "./boardmodel.mjs";

export const SCOPES = { everywhere: "Everywhere", game: "Game only", discord: "Discord only", whispers: "Whispers with you only" };
export function nudgeArgs(kind, target, fields, verb = "set") {
  const text = cleanReply(fields.text), seconds = Number(fields.hours) * 3600;
  if (!text || Array.from(text).length > 200 || Array.from(str(fields.text)).length > 1024) {
    throw new Error("Write 1–200 characters after cleaning.");
  }
  if (!Number.isInteger(seconds) || seconds < 3600 || seconds > 1209600) {
    throw new Error("Choose from one hour to fourteen days.");
  }
  if (!Object.hasOwn(SCOPES, fields.scope) || !["light", "firm"].includes(fields.strength)) {
    throw new Error("Choose a listed strength and scope.");
  }
  return { action: "nudge", verb, target_kind: kind, target: String(target), text,
    strength: fields.strength, scope: fields.scope, seconds };
}
export function activeFor(view, kind, target) {
  const ids = kind === "member" ? arr(arr(view.members).find(m => String(m.guid) === String(target))?.nudges) : [];
  return arr(obj(view.nudges).active).filter(n => (n.target_kind === kind && n.target === String(target)) || ids.includes(n.id));
}
export function timeLeft(ends, now = Date.now() / 1000) {
  const minutes = Math.max(0, Math.ceil((ends - now) / 60));
  if (!minutes) return "Expired";
  return minutes < 60 ? `${minutes} min left` : `${Math.ceil(minutes / 60)} h left`;
}
