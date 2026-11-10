// The same small grammar as discord/text.py: one flag, tag flag or joined emoji unit.
var BASE = "[\\u{1F000}-\\u{1F1E5}\\u{1F200}-\\u{1F3FA}\\u{1F400}-\\u{1FAFF}"
  + "\\u2300-\\u23FF\\u2600-\\u27BF\\u2B00-\\u2BFF\\u203C\\u2049\\u2122\\u2139"
  + "\\u2194-\\u21AA\\u24C2\\u25AA-\\u25FE\\u2934-\\u2935\\u3030\\u303D\\u3297\\u3299\\u00A9\\u00AE]";
var UNIT = "(?:[#*0-9]\\uFE0F\\u20E3|" + BASE + "\\uFE0F?[\\u{1F3FB}-\\u{1F3FF}]?)";
var ONE = new RegExp("^(?:[\\u{1F1E6}-\\u{1F1FF}]{2}|\\u{1F3F4}[\\u{E0020}-\\u{E007E}]+\\u{E007F}|" + UNIT + "(?:\\u200D" + UNIT + ")*)$", "u");
export function isOneEmoji(text) { return typeof text === "string" && ONE.test(text); }
export var PICKER = [
  { name: "Faces", emoji: ["😀", "😄", "😁", "😂", "🤣", "😊", "😉", "😍", "🥰", "😎", "🤔", "🙃", "😭", "😅", "😴", "🥳"] },
  { name: "Gestures", emoji: ["👍", "👎", "👏", "🙌", "👋", "🤝", "🙏", "💪", "🤞", "👌", "✌️", "🤘", "🫶", "🫡", "🤷", "🤦"] },
  { name: "Hearts and symbols", emoji: ["❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍", "💔", "💕", "✨", "⭐", "🔥", "✅", "❌", "‼️"] },
  { name: "Food and life", emoji: ["☕", "🍵", "🍺", "🍷", "🍕", "🍔", "🍟", "🍰", "🍪", "🥓", "🎂", "🐈", "🐕", "🌧️", "☀️", "🌙"] },
  { name: "Games and things", emoji: ["🎮", "🎲", "🏆", "⚔️", "🛡️", "🏹", "🪄", "💎", "💰", "🎵", "🎉", "🎁", "📌", "🔔", "👀", "💀"] }
];
