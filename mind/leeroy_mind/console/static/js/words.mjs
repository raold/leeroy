import { arr, num, obj, pad, str } from "./core.mjs";

// How the page words what mind.db holds. No DOM here: node imports this file.

export var CLASSES = { 1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest", 6: "Death Knight",
                       7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid" };

export function when(sec) {
  if (!sec) { return ""; }
  var d = new Date(sec * 1000), now = new Date();
  var day = d.toDateString() === now.toDateString() ? "" : pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " ";
  return day + pad(d.getHours()) + ":" + pad(d.getMinutes());
}
export function dkp(v) { return typeof v === "number" ? v.toFixed(1) : "-"; }
export function signed(v, places) { return typeof v === "number" ? (v > 0 ? "+" : "") + v.toFixed(places) : "-"; }
export function mode(size, heroic) { return (size ? " " + size : "") + (heroic ? " heroic" : ""); }
export function person(view, guid) {
  return obj(obj(obj(view).people)[String(guid)]);
}
export function who(view, guid) {
  if (!guid) { return "-"; }
  if (view && guid === view.human) { return "you"; }
  return str(person(view, guid).name) || "#" + guid;
}

export function moodText(m) { return m ? str(m.octant) : "not recorded yet"; }
export function moodTitle(m) {
  return m ? "pleasure " + signed(m.p, 2) + ", arousal " + signed(m.a, 2) + ", dominance " + signed(m.d, 2)
             + ", set by the night's mood stage " + when(m.updated)
           : "The night's mood stage has not set one yet.";
}
export function youText(y) {
  if (!y) { return "no row yet"; }
  var bits = ["affinity " + signed(y.affinity, 0)];
  if (y.attraction) { bits.push("attraction " + Math.round(y.attraction)); }
  if (y.romance_stage && y.romance_stage !== "none") { bits.push(y.romance_stage); }
  return bits.join(", ");
}
export function youTitle(y) {
  return y ? "trust " + Math.round(num(y.trust)) + ", respect " + Math.round(num(y.respect)) + ", familiarity "
             + Math.round(num(y.familiarity)) + (y.label ? ", " + y.label : "") : "";
}

// The member card in Comms from /api/persona's member view: the name, a note (age and region, then
// the stance) and three facts. view null: mind.db does not know them (a 404), so only the name the
// page knows (known) is shown.
export function cardText(view, known) {
  if (!view) { return { name: str(known), note: "Not in mind.db.", facts: [] }; }
  var m = obj(obj(view).member), c = view.card ? obj(view.card) : null, missing = arr(view.missing);
  var name = str(m.name) || str(known) || "#" + num(m.guid), note = "No persona yet.";
  if (c) {
    var id = obj(c.identity), stance = str(m.stance);
    note = [id.age, id.region].filter(function (x) { return x !== null && x !== undefined && x !== ""; }).join(", ");
    if (stance) { note = note ? note + " · " + stance : stance; }
  }
  var role = [m["class"] ? CLASSES[m["class"]] || "-" : "", str(m.role)].filter(Boolean).join(" · ") || "-";
  return { name: name, note: note,
           facts: [{ k: "Role", v: role },
                   { k: "Mood", v: missing.indexOf("pad_mood") >= 0 ? "unavailable" : moodText(m.mood) },
                   { k: "Toward you", v: missing.indexOf("relationship") >= 0 ? "unavailable" : youText(m.you) }] };
}
