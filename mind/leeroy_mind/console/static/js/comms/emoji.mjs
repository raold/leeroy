// The same small grammar as discord/text.py: one flag, tag flag or joined emoji unit.
var BASE = "[\\u{1F000}-\\u{1F1E5}\\u{1F200}-\\u{1F3FA}\\u{1F400}-\\u{1FAFF}"
  + "\\u2300-\\u23FF\\u2600-\\u27BF\\u2B00-\\u2BFF\\u203C\\u2049\\u2122\\u2139"
  + "\\u2194-\\u21AA\\u24C2\\u25AA-\\u25FE\\u2934-\\u2935\\u3030\\u303D\\u3297\\u3299\\u00A9\\u00AE]";
var UNIT = "(?:[#*0-9]\\uFE0F\\u20E3|" + BASE + "\\uFE0F?[\\u{1F3FB}-\\u{1F3FF}]?)";
var ONE = new RegExp("^(?:[\\u{1F1E6}-\\u{1F1FF}]{2}|\\u{1F3F4}[\\u{E0020}-\\u{E007E}]+\\u{E007F}|" + UNIT + "(?:\\u200D" + UNIT + ")*)$", "u");
// One emoji; with `custom` ([culture] emoji on) also one registered shortcode, never an unregistered one.
export function isOneEmoji(text, custom) { return typeof text === "string" && (ONE.test(text) || (!!custom && isCustom(text))); }
export var PICKER = [
  { name: "Faces", emoji: ["😀", "😄", "😁", "😂", "🤣", "😊", "😉", "😍", "🥰", "😎", "🤔", "🙃", "😭", "😅", "😴", "🥳"] },
  { name: "Gestures", emoji: ["👍", "👎", "👏", "🙌", "👋", "🤝", "🙏", "💪", "🤞", "👌", "✌️", "🤘", "🫶", "🫡", "🤷", "🤦"] },
  { name: "Hearts and symbols", emoji: ["❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍", "💔", "💕", "✨", "⭐", "🔥", "✅", "❌", "‼️"] },
  { name: "Food and life", emoji: ["☕", "🍵", "🍺", "🍷", "🍕", "🍔", "🍟", "🍰", "🍪", "🥓", "🎂", "🐈", "🐕", "🌧️", "☀️", "🌙"] },
  { name: "Games and things", emoji: ["🎮", "🎲", "🏆", "⚔️", "🛡️", "🏹", "🪄", "💎", "💰", "🎵", "🎉", "🎁", "📌", "🔔", "👀", "💀"] }
];
// The guild's own emoji ([culture] emoji), mirrored from discord/custom_emoji.py: a test fails on
// any drift. Original SVGs in /static/emoji/<name>.svg. With the switch off nothing here is used.
export var CUSTOM_ROLES = ["tank", "healer", "dps"];
export var CUSTOM_CLASSES = ["warrior", "paladin", "hunter", "rogue", "priest", "deathknight", "shaman", "mage", "warlock", "druid"];
export var CUSTOM = CUSTOM_ROLES.concat(CUSTOM_CLASSES).map(function (n) { return ":" + n + ":"; });
export var CUSTOM_PICKER = { name: "The guild's own", emoji: CUSTOM };
var SHORTCODE = /:[a-z0-9_]{2,14}:/g;
export function isCustom(text) { return CUSTOM.indexOf(text) >= 0; }
export function customUrl(code) { return isCustom(code) ? "/static/emoji/" + code.slice(1, -1) + ".svg" : ""; }
// Text split into plain runs and registered shortcodes ({text} or {code}); an unknown code stays text.
export function customSegments(text) {
  var out = [], last = 0, m;
  text = String(text == null ? "" : text); SHORTCODE.lastIndex = 0;
  while ((m = SHORTCODE.exec(text)) !== null) {
    if (!isCustom(m[0])) { SHORTCODE.lastIndex = m.index + 1; continue; }
    if (m.index > last) { out.push({ text: text.slice(last, m.index) }); }
    out.push({ code: m[0] }); last = m.index + m[0].length;
  }
  if (last < text.length || !out.length) { out.push({ text: text.slice(last) }); }
  return out;
}
