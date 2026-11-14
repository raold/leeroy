// Godmode in the console, without the DOM: the edit kinds and string kinds, the arguments the
// mind's tune op (action "edit") and string op take, and the words for their codes. Node can load
// this file. The mind checks everything again and says why when it refuses.
import { arr, obj, str } from "./core.mjs";

export const EDIT_KINDS = {
  mood: { label: "Mood", fields: [
    { name: "offset", label: "Mood shift (-1 to 1)", type: "number", min: -1, max: 1, step: 0.1 },
    { name: "hours", label: "For hours (1 to 336)", type: "number", min: 1, max: 336, step: 1 }] },
  feeling: { label: "Feelings", fields: [
    { name: "other", label: "Toward", type: "member", you: true },
    { name: "affinity", label: "Affinity shift", type: "number", min: -200, max: 200, optional: true },
    { name: "trust", label: "Trust shift", type: "number", min: -100, max: 100, optional: true },
    { name: "attraction", label: "Attraction shift (her toward you)", type: "number", min: -100, max: 100, optional: true },
    { name: "romance_stage", label: "Romance stage (her toward you)", type: "select", optional: true,
      options: ["none", "notices", "banter", "whispers", "late_nights", "voice", "picture", "confession", "planning"] }] },
  trait: { label: "Trait", fields: [
    { name: "path", label: "Part", type: "select", options: [
      "big_five.openness.score", "big_five.conscientiousness.score", "big_five.extraversion.score",
      "big_five.agreeableness.score", "big_five.neuroticism.score", "attachment", "humour",
      "likes", "dislikes", "values", "opinions", "speech_habits", "game.wants", "game.motivation",
      "game.thinks_as", "game.favourite_spot"] },
    { name: "value", label: "Value (a score, a word, or a list split by ;)", type: "text" }] },
  life_event: { label: "Life event", fields: [{ name: "title", label: "What happens in her life", type: "text", max: 80 }] },
  memory_implant: { label: "Implant a memory", fields: [
    { name: "text", label: "What she remembers", type: "text", max: 200 },
    { name: "days_ago", label: "How many days ago", type: "number", min: 0, max: 3650, step: 1, optional: true },
    { name: "scope", label: "Who it may come up with", type: "select", options: ["open", "private", "secret"], optional: true }] },
  memory_erase: { label: "Erase a memory", fields: [{ name: "memory", label: "Memory id", type: "number", min: 1, step: 1 }] },
  feud_start: { label: "Start a feud", fields: [{ name: "other", label: "With", type: "member" }] },
  feud_end: { label: "End a feud", fields: [{ name: "other", label: "With", type: "member" }] },
  hangouts: { label: "Hangouts", fields: [
    { name: "channels", label: "Channels (irl:3; general:0)", type: "weights", optional: true },
    { name: "rooms", label: "Voice rooms (kitchen:3)", type: "weights", optional: true }] },
  skill: { label: "In-game skill", fields: [{ name: "skill", label: "Skill", type: "select", options: ["star", "middle", "fire"] }] },
  activities: { label: "Day-plan activities", fields: [
    { name: "activities", label: "Activities (fishing; inn)", type: "list", optional: true },
    { name: "with", label: "With (guids: 103; 104)", type: "guids", optional: true }] },
};

export const STRING_KINDS = {
  post: { label: "Post in a channel", fields: [
    { name: "channel", label: "Channel", type: "text" }, { name: "about", label: "About", type: "text", max: 200 }] },
  thread: { label: "Start a thread", fields: [
    { name: "channel", label: "Channel", type: "text" }, { name: "name", label: "Thread name", type: "text", max: 60 },
    { name: "about", label: "About", type: "text", max: 200 }] },
  react: { label: "React to a message", fields: [
    { name: "message", label: "Message id", type: "number", min: 1, step: 1 }, { name: "emoji", label: "Emoji", type: "text" }] },
  dm_him: { label: "DM you", fields: [{ name: "about", label: "About", type: "text", max: 200 }] },
  bring_up: { label: "Bring something up", fields: [
    { name: "topic", label: "Topic", type: "text", max: 120 },
    { name: "channel", label: "Channel (optional)", type: "text", optional: true }] },
};

const CODES = {
  off: "Godmode is off in mind.toml.", age: "Nothing may make anyone read as young.",
  looks: "Looks are never edited.", name: "Names are never edited.", identity: "Identity is never edited.",
  romance: "Her romance stance is never edited here.", explicit: "Keep it non-explicit.",
  real_name: "Use character names, not real names.", romance_members: "Romance aims only at you, never between members.",
  men_friends: "Men stay friends.", stance: "Her stance makes that absurd.", ladder: "That stage is not on the ladder.",
  feud: "She is feuding with them.", away: "She is away.", claims: "It contradicts what she has said.",
  deleted: "The message is gone.", gone: "She is no longer in the cast.", due: "It was never done in time.",
  owner: "You cancelled it.", sent: "Done.", mentioned: "She brought it up.", reacted: "Done.",
  not_yet: "Strings in the game come with a later update.", server_off: "The guild Discord's engine is off.",
  cap: "Too many waiting.", audience: "She cannot read there.", channel: "Not a conversation channel.",
};
export function codeText(code, fallback) { return CODES[code] || str(fallback) || str(code) || "refused"; }

function list(v) { return str(v).split(";").map(s => s.trim()).filter(Boolean); }
function weights(v) {
  const out = {};
  for (const part of list(v)) {
    const [k, w] = part.split(":").map(s => s.trim());
    const n = Number(w);
    if (!k || !Number.isInteger(n) || n < 0 || n > 3) throw new Error("Weights are name:0 to name:3.");
    out[k] = n;
  }
  return out;
}

function traitValue(path, raw) {
  const text = str(raw).trim();
  if (path.endsWith(".score")) return Number(text);
  if (["likes", "dislikes", "values", "opinions", "speech_habits", "game.wants"].includes(path)) return list(text);
  return text;
}

function build(spec, values, extra = {}) {
  const args = { ...extra };
  for (const f of spec.fields) {
    const raw = values[f.name];
    const blank = raw === undefined || raw === null || str(String(raw)).trim() === "";
    if (blank) {
      if (f.optional) continue;
      throw new Error(`${f.label}: required.`);
    }
    if (f.type === "number" || f.type === "member") {
      const n = Number(raw);
      if (!Number.isFinite(n) || (f.min !== undefined && n < f.min) || (f.max !== undefined && n > f.max)) {
        throw new Error(`${f.label}: out of range.`);
      }
      args[f.name] = n;
    } else if (f.type === "weights") args[f.name] = weights(raw);
    else if (f.type === "list") args[f.name] = list(raw);
    else if (f.type === "guids") args[f.name] = list(raw).map(Number);
    else {
      const text = str(String(raw)).trim();
      if (f.max && Array.from(text).length > f.max) throw new Error(`${f.label}: at most ${f.max} characters.`);
      args[f.name] = text;
    }
  }
  return args;
}

// The tune op's edit: {action: "edit", verb, kind, args}
export function editArgs(kind, member, values, verb = "set") {
  const spec = EDIT_KINDS[kind];
  if (!spec) throw new Error("Choose a kind of edit.");
  const args = build(spec, values, { member: Number(member) });
  if (kind === "mood") { args.seconds = Math.round(args.hours * 3600); delete args.hours; }
  if (kind === "trait") args.value = traitValue(args.path, values.value);
  return { action: "edit", verb, kind, args };
}

// The string op: {action: "create", member, kind, args}
export function stringArgs(kind, member, values) {
  const spec = STRING_KINDS[kind];
  if (!spec) throw new Error("Choose a kind of string.");
  return { action: "create", member: Number(member), kind, args: build(spec, values) };
}

// One edit as a line: its kind and what it does, his own words included (this page is his).
export function editText(e, names = {}) {
  const a = obj(e.args), who = g => names[g] || (g === undefined ? "?" : "#" + g);
  switch (e.kind) {
    case "mood": return `Mood ${a.offset > 0 ? "+" : ""}${a.offset}`;
    case "feeling": return `Feelings toward ${who(a.other)}: ` + ["affinity", "trust", "attraction"]
      .filter(d => d in a).map(d => `${d} ${a[d] > 0 ? "+" : ""}${a[d]}`).concat(a.romance_stage ? [`stage ${a.romance_stage}`] : []).join(", ");
    case "trait": return `${str(a.path).replace(/_/g, " ")}: ${Array.isArray(a.value) ? a.value.join("; ") : String(a.value)}`;
    case "life_event": return `Life event: ${str(a.title)}`;
    case "memory_implant": return `Memory: ${str(a.text)}`;
    case "memory_erase": return `Erased memory #${a.memory}`;
    case "feud_start": return `Feud: ${who(a.member)} and ${who(a.other)}`;
    case "feud_end": return `Feud ended: ${who(a.member)} and ${who(a.other)}`;
    case "hangouts": return "Hangouts: " + Object.entries({ ...obj(a.channels), ...obj(a.rooms) }).map(([k, w]) => `${k} ${w}`).join(", ");
    case "skill": return `Skill: ${str(a.skill)}`;
    case "activities": return "Activities: " + arr(a.activities).join(", ") + (arr(a.with).length ? " with " + arr(a.with).map(who).join(", ") : "");
    default: return str(e.kind);
  }
}

export function editsFor(view, guid) {
  return arr(obj(obj(view).edits).active).filter(e => e.member === guid || obj(e.args).other === guid);
}

// One string's state, in words
export function stringState(i, now = Date.now() / 1000) {
  if (i.status === "pending") {
    const h = Math.max(0, Math.ceil((num(i.due) - now) / 3600));
    return `waiting (${h} h left)`;
  }
  const words = { taken: "done", not_taken: "not taken", expired: "expired", cancelled: "cancelled" }[i.status] || str(i.status);
  return i.reason ? `${words}: ${codeText(i.reason)}` : words;
}
function num(x) { return typeof x === "number" && isFinite(x) ? x : 0; }

export function stringText(i) {
  const a = obj(i.args);
  switch (i.kind) {
    case "post": return `Post in #${str(a.channel)} about ${str(a.about)}`;
    case "thread": return `Thread "${str(a.name)}" in #${str(a.channel)} about ${str(a.about)}`;
    case "react": return `React ${str(a.emoji)} to message #${a.message}`;
    case "dm_him": return `DM you about ${str(a.about)}`;
    case "bring_up": return `Bring up ${str(a.topic)}` + (a.channel ? ` in #${str(a.channel)}` : "");
    default: return str(i.kind);
  }
}

// History lines for a change's edits: ids, kinds and the state move only
export function historyEdits(c) {
  return arr(c.edits).map(e => `edit #${e.id} ${str(e.kind).replace(/_/g, " ")}: ${e.from} → ${e.to}`);
}
