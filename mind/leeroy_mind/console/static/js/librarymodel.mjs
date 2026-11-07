import { arr, obj, num, str } from "./core.mjs";

// How the Library tab words and asks things. No DOM here: node imports this file.

export var KINDS = ["item", "loot", "quest", "lore", "strategy", "class", "zone", "event",
                    "documentation", "api", "command", "teleport"];
var PLURAL = { item: "items", loot: "loot tables", quest: "quests", lore: "lore", strategy: "strategies",
               class: "classes", zone: "zones", event: "events", documentation: "documentation",
               api: "API entries", command: "commands", teleport: "teleports" };
// Syntax and coordinates must stay as written: these kinds are shown preformatted
var PRE = ["command", "teleport", "documentation", "api"];
export var QUERY_MAX = 200;
export var NOTHING = "Nothing in the library matches that.";

export function isPre(kind) { return PRE.indexOf(kind) >= 0; }

// The question as the console reads it: whitespace collapsed
export function cleanQuery(text) { return str(text).split(/\s+/).filter(Boolean).join(" "); }

// null when it can be asked, else the words
export function queryProblem(text) {
  var q = cleanQuery(text);
  if (!q) { return "Type a question to search."; }
  if (q.length > QUERY_MAX) { return "A question is at most " + QUERY_MAX + " characters."; }
  return null;
}

export function searchPath(text, kind, deeper) {
  var p = "/api/library?q=" + encodeURIComponent(cleanQuery(text));
  if (KINDS.indexOf(kind) >= 0) { p += "&kind=" + encodeURIComponent(kind); }
  if (deeper) { p += "&deeper=1"; }
  return p;
}

// "N passages: items n, commands n, ..." in the fixed kind order, kinds with none left out
export function statsLine(stats) {
  var s = obj(stats), kinds = obj(s.kinds), total = num(s.total);
  var parts = KINDS.filter(function (k) { return num(kinds[k]) > 0; })
    .map(function (k) { return PLURAL[k] + " " + num(kinds[k]); });
  return total + (total === 1 ? " passage" : " passages") + (parts.length ? ": " + parts.join(", ") : "");
}

// Whether a text answer should be followed by the mind's own search at once: it found nothing
export function wantsDeeper(view, wasDeeper) {
  return !wasDeeper && !!view && view.setup === true && arr(view.hits).length === 0;
}

// What to say under an answer: {text, bad}. An automatic deeper search that could not run is not
// an error: the text found nothing, and the note says why the mind could not look further.
export function answerNote(view, deeper, automatic) {
  var v = obj(view);
  if (v.setup === false) { return { text: "Not set up yet: " + str(v.why), bad: false }; }
  if (deeper && v.deeper_why) {
    if (automatic) { return { text: NOTHING + " " + str(v.deeper_why), bad: false }; }
    return { text: str(v.deeper_why), bad: true };
  }
  if (!arr(v.hits).length) { return { text: NOTHING, bad: false }; }
  return { text: "", bad: false };
}
