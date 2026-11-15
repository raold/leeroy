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

// The photo kind a member chose (the persona card's photo): its kind, framing and setting
export function photoKindText(p) {
  if (!p) { return ""; }
  return [str(p.kind).replace(/_/g, " "), str(p.framing).replace(/_/g, " "), str(p.setting)]
    .filter(Boolean).join(", ");
}

var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
              "October", "November", "December"];
var SUBSTANCE = { alcohol: "drinks", weed: "smokes weed", caffeine: "runs on caffeine", tobacco: "smokes" };
var WHEN = { raid_nights: "on raid nights", weekends: "at weekends", days_off: "on days off",
             after_midnight: "after midnight", daily: "every day" };

// A persona birthday ("MM-DD") in words ("January 10"); anything else as it is
export function birthdayText(mmdd) {
  var m = /^(\d\d)-(\d\d)$/.exec(str(mmdd));
  if (!m || +m[1] < 1 || +m[1] > 12) { return str(mmdd); }
  return MONTHS[+m[1] - 1] + " " + (+m[2]);
}

// The member's bio from the persona card: a short line about them (their life thread) and its rows,
// each left out when empty. card null: no bio.
export function bioText(card) {
  if (!card) { return { about: "", rows: [] }; }
  var c = obj(card), life = obj(c.life), game = obj(c.game), play = obj(c.play), rows = [];
  function add(k, v) { v = str(v).trim(); if (v) { rows.push({ k: k, v: v }); } }
  function list(v, sep) { return arr(v).map(str).filter(Boolean).join(sep || ", "); }
  add("Job", life.job);
  add("Home", life.household);
  add("Days", [str(life.rhythm).trim().replace(/\.$/, ""), life.night_owl === true ? "night owl" : ""]
    .filter(Boolean).join(" · "));
  add("Birthday", birthdayText(life.birthday));
  add("Habits", arr(c.habits).map(function (h) {
    h = obj(h);
    return SUBSTANCE[h.substance] ? SUBSTANCE[h.substance] + (WHEN[h.when] ? " " + WHEN[h.when] : "") : "";
  }).filter(Boolean).join(", "));
  add("Likes", list(c.likes));
  add("Dislikes", list(c.dislikes));
  add("Values", list(c.values));
  add("Wants", list(game.wants, "; "));
  add("Plays", [typeof game.years_played === "number" ? game.years_played + " years" : "",
                str(play.main_spec) + (play.main_spec && play.off_spec ? " (off: " + play.off_spec + ")" : "")]
    .filter(Boolean).join(", "));
  add("Off raids", list(game.outside_raids).replace(/_/g, " "));
  add("Hangs out", game.favourite_spot);
  return { about: str(life.life_thread).trim(), rows: rows };
}

// Who a member is in one line (owner, 2026-11-15): her age, where she lives (her US state, else her
// region: "UK", or "US" before she has a state), her stance toward him and her relationship status,
// as "38 · Texas · lust · married", each part left out when empty (a stance of none too, so it never
// reads "none · married"). gender: put after the age (the Personas tab's player line).
export function whoText(identity, stance, status, gender) {
  var id = obj(identity), age = typeof id.age === "number" ? String(id.age) : str(id.age);
  return [age, str(gender), str(id.home) || str(id.region), stance === "none" ? "" : str(stance), str(status)]
    .map(function (x) { return x.trim(); }).filter(Boolean).join(" · ");
}

// The Personas detail's lead line: the night's mood and her row toward him. Her stance is on the
// player line (whoText) and nowhere else there.
export function leadText(m, missing) {
  m = obj(m); missing = arr(missing);
  return "Mood: " + (missing.indexOf("pad_mood") >= 0 ? "unavailable" : moodText(m.mood))
    + (m.mood ? " (" + moodTitle(m.mood) + ")" : "")
    + " · toward you: " + (missing.indexOf("relationship") >= 0 ? "unavailable" : youText(m.you));
}

// The Personas detail's romance row: her style and how she takes jealousy (the stance is the player line's)
export function romanceText(romance) {
  var ro = obj(romance);
  return [str(ro.style), str(ro.jealousy_mode).replace(/_/g, " ")].filter(Boolean).join(", ") || "-";
}

// The member card in Comms from /api/persona's member view: the name, a note (whoText: age, place,
// stance and relationship status), three facts and the bio (bioText). view null: mind.db does not know them (a 404),
// so only the name the page knows (known) is shown.
export function cardText(view, known) {
  if (!view) { return { name: str(known), note: "Not in mind.db.", facts: [], bio: bioText(null) }; }
  var m = obj(obj(view).member), c = view.card ? obj(view.card) : null, missing = arr(view.missing);
  var name = str(m.name) || str(known) || "#" + num(m.guid), note = "No persona yet.";
  if (c) { note = whoText(c.identity, m.stance, c.status); }
  var role = [m["class"] ? CLASSES[m["class"]] || "-" : "", str(m.role)].filter(Boolean).join(" · ") || "-";
  return { name: name, note: note, bio: bioText(c),
           facts: [{ k: "Role", v: role },
                   { k: "Mood", v: missing.indexOf("pad_mood") >= 0 ? "unavailable" : moodText(m.mood) },
                   { k: "Toward you", v: missing.indexOf("relationship") >= 0 ? "unavailable" : youText(m.you) }] };
}

// A selfie-wall tile in words: dimmed only for a member out of the cast, "no persona" when none is stored
export function tileState(m) {
  m = obj(m);
  return { off: !m.in_cast, chip: m.persona ? "" : "no persona",
           label: str(m.name) + (m.in_cast ? "" : " (not in the cast)"),
           sub: m["class"] ? (CLASSES[m["class"]] || "-") + (m.role ? " · " + m.role : "") : "-" };
}
