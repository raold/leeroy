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
import { CUSTOM, CUSTOM_CLASSES, CUSTOM_PICKER, CUSTOM_ROLES, customSegments, customUrl, isCustom } from "../../../leeroy_mind/console/static/js/comms/emoji.mjs";
import { BOT_NAMES, botName } from "../../../leeroy_mind/console/static/js/comms/discord.mjs";

// The same one list tests/discord/test_custom_emoji.py checks the Python registry against.
const NAMES = ["tank", "healer", "dps", "warrior", "paladin", "hunter", "rogue", "priest", "deathknight",
  "shaman", "mage", "warlock", "druid"];

test("the guild's own emoji mirror the Python registry exactly", () => {
  assert.deepEqual(CUSTOM_ROLES.concat(CUSTOM_CLASSES), NAMES);
  assert.deepEqual(CUSTOM, NAMES.map(n => ":" + n + ":"));
  assert.deepEqual(CUSTOM_PICKER.emoji, CUSTOM);
  assert.equal(customUrl(":tank:"), "/static/emoji/tank.svg");
  assert.equal(customUrl(":nope:"), "");
});

test("a shortcode is one emoji only when registered and the switch is on", () => {
  assert.ok(!isOneEmoji(":tank:") && isOneEmoji(":tank:", true));
  [":nope:", "tank", ":TANK:", ":tank: ", ":tank::healer:"].forEach(s => assert.ok(!isOneEmoji(s, true), s));
  assert.ok(isCustom(":druid:") && !isCustom(":druid"));
});

test("text splits into plain runs and registered shortcodes; an unknown code stays text", () => {
  assert.deepEqual(customSegments("React :tank: :nope: :dps:"),
    [{ text: "React " }, { code: ":tank:" }, { text: " :nope: " }, { code: ":dps:" }]);
  assert.deepEqual(customSegments("plain"), [{ text: "plain" }]);
  assert.deepEqual(customSegments(""), [{ text: "" }]);
  assert.deepEqual(customSegments(":mage::tank:"), [{ code: ":mage:" }, { code: ":tank:" }]);
});

test("a bot's post says which bot wrote it", () => {
  assert.deepEqual(Object.keys(BOT_NAMES), ["signups", "loot", "logs", "birthday", "firsts", "reminders"]);
  assert.equal(botName("loot"), "Loot Bot");
  assert.equal(botName("__proto__"), "Server bot");
  assert.equal(botName(undefined), "Server bot");
});

import { groupMessages } from "../../../leeroy_mind/console/static/js/comms/discord.mjs";

test("bot posts group only with their own bot's, so each keeps its name and BOT tag", () => {
  const post = (id, bot, ts) => ({ id, author: 0, bot, thread: null, ts, reply_to: null, deleted: null });
  assert.deepEqual(groupMessages([post(1, "birthday", 100), post(2, "reminders", 110), post(3, "reminders", 120)])
    .map(g => g.grouped), [false, false, true]);
  const member = (id, ts) => ({ id, author: 101, thread: null, ts, reply_to: null, deleted: null });
  assert.deepEqual(groupMessages([member(4, 100), member(5, 110)]).map(g => g.grouped), [false, true]);
});
