import test from "node:test";
import assert from "node:assert/strict";
import { kindWord, dayGroups, nextRaid, birthdaysThisWeek, signupCounts, youText } from "../../../leeroy_mind/console/static/js/calendarmodel.mjs";

test("kinds read as words", () => {
  assert.equal(kindWord("raid"), "Raid night");
  assert.equal(kindWord("meeting"), "Officer meeting");
  assert.equal(kindWord("birthday"), "Birthday");
  assert.equal(kindWord("anniversary"), "Guild anniversary");
  assert.equal(kindWord("offline"), "Offline raid");
  assert.equal(kindWord("x"), "x");
});
test("events group by local day, in order", () => {
  const d = (h, day) => new Date(2026, 10, day, h).getTime() / 1000;
  const g = dayGroups([{ id: 2, at: d(21, 5) }, { id: 1, at: d(9, 5) }, { id: 3, at: d(20, 6) }]);
  assert.deepEqual(g.map(x => x.events.map(e => e.id)), [[1, 2], [3]]);
  assert.equal(g[0].day, "2026-11-05");
});
test("the next raid night is the first raid not yet begun", () => {
  const up = [{ id: 1, kind: "raid", at: 50 }, { id: 2, kind: "meeting", at: 150 }, { id: 3, kind: "raid", at: 200 }];
  assert.equal(nextRaid(up, 100).id, 3);
  assert.equal(nextRaid([], 100), null);
});
test("birthdays within seven days are counted", () => {
  const up = [{ kind: "birthday", at: 100 }, { kind: "birthday", at: 100 + 8 * 86400 }, { kind: "raid", at: 100 }];
  assert.equal(birthdaysThisWeek(up, 0), 1);
});
test("sign-up counts read as words", () => {
  assert.deepEqual(signupCounts({ signed_up: [1, 2], tentative: [3], declined: [], other: [] }),
    ["2 signed up", "1 tentative"]);
  assert.deepEqual(signupCounts({}), []);
});
test("his own answer", () => {
  assert.equal(youText("signed_up"), "you: signed up");
  assert.equal(youText("tentative"), "you: tentative");
  assert.equal(youText("declined"), "you: declined");
  assert.equal(youText(null), "");
});
