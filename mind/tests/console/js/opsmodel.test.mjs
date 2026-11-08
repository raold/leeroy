import test from "node:test";
import assert from "node:assert/strict";
import { PANELS, badge, lines, panelWords } from "../../../leeroy_mind/console/static/js/opsmodel.mjs";

test("all eight independent panels have missing and error words", () => {
  assert.equal(PANELS.length, 8);
  PANELS.forEach(([name]) => {
    assert.deepEqual(lines(name, { status: "missing", why: "older database" }), ["Not set up yet: older database"]);
    assert.deepEqual(lines(name, { status: "error", why: "Skipped: budget" }), ["Skipped: budget"]);
    assert.doesNotThrow(() => lines(name, { status: "ok" }));
  });
  assert.match(panelWords(null), /Not set up yet/);
});
test("badge distinguishes unknown from zero and shows both counts", () => {
  assert.equal(badge({counts: {fails: 2, held_members: 5}}).text, "2 FAIL · 5 held");
  assert.equal(badge({counts: {fails: 0, held_members: 0}}).text, "0 FAIL · 0 held");
  assert.equal(badge({}).text, "? FAIL · ? held");
  assert.equal(badge({counts: {fails: 0, held_members: 0}}).show, false);
  assert.equal(badge({counts: {fails: 0, held_members: 3}}).show, true);
  assert.equal(badge({}).show, false);
  assert.equal(badge({counts: {fails: NaN, held_members: -1}}).held, null);
});
test("saved running night and absent progress remain unknown", () => {
  assert.match(lines("night", {status: "ok", saved_state: "running", running: null}).join(" "), /current activity unknown/);
  assert.match(lines("raids", {status: "ok", offline: {id: 4}}).join(" "), /Progress not recorded/);
});
test("no generic payload dumping leaks extra memory or key fields", () => {
  const secret = "PRIVATE_SENTINEL";
  PANELS.forEach(([name]) => assert.ok(!lines(name, {status: "ok", memory: secret, key: secret}).join(" ").includes(secret)));
});
test("optional booth backup and task states have explicit words", () => {
  assert.match(lines("selfies", {status: "ok"}).join(" "), /no photo booth pass/);
  assert.match(lines("schedules", {status: "ok", tasks: [{name: "leeroy-health", installed: false}]}).join(" "), /not installed/);
  assert.match(lines("backups", {status: "ok", offmachine: {status: "missing", why: "not configured"}}).join(" "), /Not set up yet: not configured/);
});
