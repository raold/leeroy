import { $, arr, clear, el, num, obj, plural, str } from "./core.mjs";
import { face } from "./faces.mjs";
import { notSetUp, pollTab, renderKpis } from "./tabkit.mjs";
import { COLUMNS, ROWS, TABS, gridRows, itemName, logWords, money, quality, slotMap, slotsUsed, tabButtons } from "./bankgrid.mjs";
import { when } from "./words.mjs";

// -- the bank tab: the mind's snapshot, item names and the ledger ---------------------------------

var BANK_MS = 60000;
var SLOTS = COLUMNS * ROWS;
var view = null, current = 0;

function nameOf(v, g) { return str(obj(v.names)[String(g)]) || "#" + g; }
function person(v, g) {
  var box = el("span", "who");
  box.appendChild(face(g, nameOf(v, g), "list"));
  box.appendChild(el("span", "", nameOf(v, g)));
  return box;
}
function itemChip(v, id, count) {
  var c = el("span", "chip item " + quality(v.items, id), itemName(v.items, id));
  if (count > 1) { c.appendChild(el("span", "mono", " x" + count)); }
  return c;
}

function drawGrid(v, tab) {
  var box = $("bank-grid");
  clear(box);
  if (!tab) { box.appendChild(el("p", "note", "This tab has not been bought.")); return; }
  var slots = slotMap(tab);
  gridRows().forEach(function (row) {
    row.forEach(function (s) {
      var cell = el("div", "slot"), it = slots[s];
      if (it) {
        cell.className += " " + quality(v.items, it.item);
        cell.appendChild(el("span", "n mono", it.count > 1 ? String(it.count) : ""));
        cell.appendChild(el("span", "w", itemName(v.items, it.item)));
        cell.title = itemName(v.items, it.item) + (it.count > 1 ? " x" + it.count : "");
      }
      box.appendChild(cell);
    });
  });
}

function drawTabs(v) {
  var box = $("bank-tabs");
  clear(box);
  tabButtons(arr(obj(v.bank).tabs)).forEach(function (b) {
    var btn = el("button", "btn", b.text);
    btn.type = "button";
    btn.disabled = !b.bought;
    btn.setAttribute("aria-pressed", String(b.tab === current));
    btn.addEventListener("click", function () { current = b.tab; if (view) { renderBank(view); } });
    box.appendChild(btn);
  });
}

function renderBank(v) {
  view = v;
  var bank = v.bank ? obj(v.bank) : null, tabs = bank ? arr(bank.tabs) : [];
  var why = $("bank-why");
  why.hidden = !v.why;
  why.textContent = str(v.why);
  if (!tabs.some(function (t) { return obj(t).tab === current; })) {
    current = tabs.length ? num(obj(tabs[0]).tab) : 0;
  }
  var tab = tabs.filter(function (t) { return obj(t).tab === current; })[0];
  var used = tabs.reduce(function (n, t) { return n + slotsUsed(t); }, 0);
  renderKpis($("bank-kpis"), bank ? [
    { k: "Gold", v: money(bank.money), d: "in the guild bank" },
    { k: "Tabs", v: tabs.length + " of " + TABS, d: "bought" },
    { k: "Slots used", v: used + " of " + SLOTS * tabs.length, d: plural(tabs.length, "tab") },
    { k: "Read at", v: when(num(bank.read_at)) || "-", d: "by the mind" }]
    : [{ k: "Gold", v: "-", d: "no snapshot" }, { k: "Tabs", v: "-", d: "" },
       { k: "Slots used", v: "-", d: "" }, { k: "Read at", v: "-", d: "" }]);
  $("bank-main").hidden = !bank;
  if (bank) { drawTabs(v); drawGrid(v, tab); }

  var log = $("bank-log");
  clear(log);
  if (bank) {
    arr(bank.log).forEach(function (r) {
      var row = el("div", "srow");
      row.appendChild(el("span", "muted mono", when(num(r[0]))));
      row.appendChild(person(v, r[2]));
      row.appendChild(el("span", "chip", logWords(r[1])));
      row.appendChild(el("span", "mono", money(r[3])));
      log.appendChild(row);
    });
    if (!log.firstChild) { log.appendChild(el("p", "note", "The money log is empty.")); }
  } else {
    log.appendChild(el("p", "note", "Not read: the mind gave no snapshot."));
  }

  var led = $("bank-ledger");
  clear(led);
  if (arr(v.missing).indexOf("realm_bank") >= 0) {
    notSetUp(led, "the Realm has not stocked or withdrawn anything.");
    return;
  }
  arr(v.ledger).forEach(function (r) {
    var row = el("div", "srow");
    row.appendChild(el("span", "muted mono", when(num(r.ts))));
    row.appendChild(person(v, r.guid));
    row.appendChild(el("span", "chip " + (r.dir === "in" ? "ok" : "warn"), r.dir === "in" ? "in" : "out"));
    row.appendChild(el("span", "muted", str(r.reason)));
    if (num(r.item_id)) { row.appendChild(itemChip(v, r.item_id, num(r.count))); }
    if (num(r.money)) { row.appendChild(el("span", "mono", money(r.money))); }
    led.appendChild(row);
  });
  if (!led.firstChild) { led.appendChild(el("p", "note", "Nothing stocked or withdrawn yet.")); }
}

pollTab("bank", { path: "/api/bank", ms: BANK_MS, render: renderBank });
