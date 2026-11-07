import test from "node:test";
import assert from "node:assert/strict";
import { COLUMNS, ROWS, gridRows, money, slotMap, slotsUsed, quality, itemName, logWords, tabButtons, who } from "../../../leeroy_mind/console/static/js/bankgrid.mjs";

test("the grid lays slots out as the game's bank frame does", () => {
  const g = gridRows();
  assert.equal(COLUMNS, 7);
  assert.equal(ROWS, 14);
  assert.equal(g.length, 14);
  assert.deepEqual(g[0], [0, 14, 28, 42, 56, 70, 84]);
  assert.deepEqual(g[13], [13, 27, 41, 55, 69, 83, 97]);
  assert.equal(new Set(g.flat()).size, 98);
});
test("money reads as gold, silver and copper", () => {
  assert.equal(money(1234567), "123g 45s 67c");
  assert.equal(money(0), "0c");
  assert.equal(money(123456789012), "12,345,678g 90s 12c");
  assert.equal(money(5000), "50s");
  assert.equal(money(10000), "1g");
  assert.equal(money(-5), "0c");
  assert.equal(money("x"), "0c");
});
test("a tab's slots map by slot and count as used", () => {
  const tab = { tab: 0, name: "A", slots: [[0, 2589, 20], [14, 4306, 5], ["x"], [3, 1]] };
  const m = slotMap(tab);
  assert.deepEqual(m[14], { item: 4306, count: 5 });
  assert.equal(m[1], undefined);
  assert.equal(slotsUsed(tab), 2);
  assert.deepEqual(slotMap(null), {});
  assert.equal(slotsUsed(undefined), 0);
});
test("quality is clamped to q0..q5 and a name falls back to the id", () => {
  assert.equal(quality({ "5": { name: "A", quality: 9 } }, 5), "q5");
  assert.equal(quality({}, 5), "q1");
  assert.equal(quality({ "5": { name: "A", quality: 3 } }, 5), "q3");
  assert.equal(itemName({ "5": { name: "Linen Cloth", quality: 1 } }, 5), "Linen Cloth");
  assert.equal(itemName({}, 777777), "Item #777777");
});
test("his own rows say You, others their name or #guid", () => {
  const v = { human: 9, names: { "9": "Hisname", "1101": "Member1101" } };
  assert.equal(who(v, 9), "You");
  assert.equal(who(v, 1101), "Member1101");
  assert.equal(who(v, 5), "#5");
  assert.equal(who({ human: 0, names: {} }, 0), "#0");
  assert.equal(who({ names: { "9": "Hisname" } }, 9), "Hisname");
});
test("log rows read in words", () => {
  assert.equal(logWords(4), "deposit");
  assert.equal(logWords(5), "withdraw");
  assert.equal(logWords(6), "repair");
  assert.equal(logWords(9), "buy tab");
  assert.equal(logWords(7), "event 7");
});
test("six tab buttons, bought ones named and the rest greyed", () => {
  const b = tabButtons([{ tab: 0, name: "Food", slots: [] }, { tab: 2, name: "", slots: [] }]);
  assert.equal(b.length, 6);
  assert.deepEqual(b[0], { tab: 0, text: "Food", bought: true });
  assert.deepEqual(b[1], { tab: 1, text: "Tab 2 (not bought)", bought: false });
  assert.deepEqual(b[2], { tab: 2, text: "Tab 3", bought: true });
});
