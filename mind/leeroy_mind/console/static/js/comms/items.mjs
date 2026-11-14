// Culture's item tooltips ([culture] loot): a message's [Name]s the console found, exactly, in
// the library's item table. Only those become item names; every other bracket stays text.
var QUALITY = ["Poor", "Common", "Uncommon", "Rare", "Epic", "Legendary", "Artifact", "Heirloom"];
var BRACKET = /\[([^\[\]\n]{1,100})\]/g;

export function qualityName(quality) {
  return Number.isInteger(quality) && QUALITY[quality] ? QUALITY[quality] : "Item";
}

// [{text}] and [{text, item}] pieces of a message, in order; nothing is dropped or added.
export function itemSegments(text, items) {
  var s = typeof text === "string" ? text : "", known = new Map();
  (Array.isArray(items) ? items : []).forEach(function (it) {
    if (it && typeof it.text === "string" && typeof it.name === "string" && !known.has(it.text)) { known.set(it.text, it); }
  });
  if (!known.size) { return [{ text: s }]; }
  var out = [], last = 0, m;
  BRACKET.lastIndex = 0;
  while ((m = BRACKET.exec(s)) !== null) {
    var it = known.get(m[1]);
    if (!it) { continue; }
    if (m.index > last) { out.push({ text: s.slice(last, m.index) }); }
    out.push({ text: m[0], item: it });
    last = m.index + m[0].length;
  }
  if (last < s.length || !out.length) { out.push({ text: s.slice(last) }); }
  return out;
}

// The popover's lines: name, then quality and item level, then the slot when the library has one.
// A name several copies share, with no way to tell which, lists their levels instead of one.
export function itemTip(it) {
  var levels = Array.isArray(it.ilvls) ? it.ilvls.filter(function (n) { return Number.isInteger(n) && n > 0; }) : [];
  var level = Number.isInteger(it.ilvl) && it.ilvl > 0 ? " · Item level " + it.ilvl
    : levels.length ? " · Item level " + levels.join(" / ") : "";
  return [it.name, qualityName(it.quality) + level].concat(typeof it.slot === "string" && it.slot ? [it.slot] : []);
}

export function qualityClass(quality) {
  return Number.isInteger(quality) && quality >= 0 && quality <= 7 ? "q" + quality : "q1";
}
