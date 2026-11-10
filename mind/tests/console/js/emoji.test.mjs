import test from "node:test";
import assert from "node:assert/strict";
import { PICKER, isOneEmoji } from "../../../leeroy_mind/console/static/js/comms/emoji.mjs";

test("the Python reaction grammar's exact table also holds in the browser", () => {
  const good = ["👍", "❤️", "👩🏽‍💻", "🇬🇧", "1️⃣", "🏴\u{e0067}\u{e0062}\u{e0073}\u{e0063}\u{e0074}\u{e007f}"];
  const bad = ["", "👍👍", "a", "lol", ":)", "👍 ", "‍", "🇬", "🇬🇧🇫🇷", "1", "1️", "👍‍", "🏽", "🏴\u{e0067}\u{e0062}\u{e0073}\u{e0063}\u{e0074}", "👍\n"];
  good.forEach(e => assert.ok(isOneEmoji(e), e)); bad.forEach(e => assert.ok(!isOneEmoji(e), JSON.stringify(e)));
});
test("the eighty curated choices and the added BMP bases are accepted", () => {
  assert.equal(PICKER.length, 5); assert.equal(PICKER.flatMap(g => g.emoji).length, 80);
  [...PICKER.flatMap(g => g.emoji), "©️", "®️", "ℹ️", "↔️", "Ⓜ️", "▪️", "⤴️", "〰️", "〽️", "㊗️", "㊙️", "👍🏽", "👨‍👩‍👧‍👦"]
    .forEach(e => assert.ok(isOneEmoji(e), e));
});
