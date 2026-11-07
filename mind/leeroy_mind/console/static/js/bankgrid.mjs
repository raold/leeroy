import { arr, num, obj, str } from "./core.mjs";

// How the Bank tab lays out and words what /api/bank holds. No DOM here: node imports this file.

export var COLUMNS = 7;
export var ROWS = 14;
export var TABS = 6;
var MONEY_WORDS = { 4: "deposit", 5: "withdraw", 6: "repair", 9: "buy tab" };

// The game's bank frame: slot s sits in column floor(s / 14), row s mod 14
export function gridRows() {
  var rows = [];
  for (var r = 0; r < ROWS; r++) {
    var row = [];
    for (var c = 0; c < COLUMNS; c++) { row.push(c * ROWS + r); }
    rows.push(row);
  }
  return rows;
}

function group(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ","); }

export function money(copper) {
  var c = typeof copper === "number" && isFinite(copper) && copper > 0 ? Math.floor(copper) : 0;
  var g = Math.floor(c / 10000), s = Math.floor(c / 100) % 100, k = c % 100, out = [];
  if (g) { out.push(group(g) + "g"); }
  if (s) { out.push(s + "s"); }
  if (k || !out.length) { out.push(k + "c"); }
  return out.join(" ");
}

// {slot: {item, count}} of a tab; a malformed slot is left out
export function slotMap(tab) {
  var m = {};
  arr(obj(tab).slots).forEach(function (s) {
    if (Array.isArray(s) && s.length === 3 && s.every(function (v) { return typeof v === "number"; })) {
      m[s[0]] = { item: s[1], count: s[2] };
    }
  });
  return m;
}
export function slotsUsed(tab) { return Object.keys(slotMap(tab)).length; }

export function quality(items, id) {
  var q = obj(obj(items)[String(id)]).quality;
  return "q" + Math.max(0, Math.min(5, typeof q === "number" ? Math.floor(q) : 1));
}
export function itemName(items, id) { return str(obj(obj(items)[String(id)]).name) || "Item #" + id; }

// Who a money log or ledger row names: "You" for him, else the member's name, else #guid
export function who(v, g) {
  v = obj(v);
  if (num(v.human) && g === v.human) { return "You"; }
  return str(obj(v.names)[String(g)]) || "#" + g;
}

export function logWords(type) { return MONEY_WORDS[type] || "event " + num(type); }

// The six tab buttons: {tab, text, bought}
export function tabButtons(tabs) {
  var by = {};
  arr(tabs).forEach(function (t) { by[num(obj(t).tab)] = obj(t); });
  var out = [];
  for (var i = 0; i < TABS; i++) {
    var t = by[i];
    out.push(t ? { tab: i, text: str(t.name) || "Tab " + (i + 1), bought: true }
               : { tab: i, text: "Tab " + (i + 1) + " (not bought)", bought: false });
  }
  return out;
}
