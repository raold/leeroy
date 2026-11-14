import test from "node:test";
import assert from "node:assert/strict";
import { itemSegments, itemTip, qualityClass, qualityName } from "../../../leeroy_mind/console/static/js/comms/items.mjs";

const RING = { text: "Invented Ring", name: "Invented Ring", quality: 3, ilvl: 200, slot: "finger" };

test("only the names the console resolved become items; every other bracket stays text", () => {
  const text = "got [Invented Ring] and [Invented] then [Invented Ring] again";
  const parts = itemSegments(text, [RING]);
  assert.deepEqual(parts.map(p => p.text).join(""), text);
  assert.deepEqual(parts.filter(p => p.item).map(p => p.text), ["[Invented Ring]", "[Invented Ring]"]);
  assert.ok(parts.some(p => !p.item && p.text.includes("[Invented]")));
});

test("no items, a missing list or a case that differs from what was written means plain text", () => {
  assert.deepEqual(itemSegments("[Invented Ring]", undefined), [{ text: "[Invented Ring]" }]);
  assert.deepEqual(itemSegments("[invented ring]", [RING]), [{ text: "[invented ring]" }]);
  assert.deepEqual(itemSegments(null, [RING]), [{ text: "" }]);
  assert.deepEqual(itemSegments("x", [{ text: 5 }]), [{ text: "x" }]);
});

test("the popover says name, quality, item level and slot", () => {
  assert.deepEqual(itemTip(RING), ["Invented Ring", "Rare · Item level 200", "finger"]);
  assert.deepEqual(itemTip({ name: "Invented Shield", quality: 4, ilvl: 0, slot: "" }), ["Invented Shield", "Epic"]);
  assert.equal(qualityName(5), "Legendary"); assert.equal(qualityName(99), "Item");
  assert.equal(qualityClass(4), "q4"); assert.equal(qualityClass("4"), "q1");
});

test("a name several copies share, unresolved, lists their levels", () => {
  assert.deepEqual(itemTip({ name: "Invented Blade", quality: 4, ilvl: null, ilvls: [251, 264], slot: "" }),
    ["Invented Blade", "Epic · Item level 251 / 264"]);
});
