import test from "node:test";
import assert from "node:assert/strict";
import { hourMinute, isoDate, isoMinute, isoSecond } from "../../../leeroy_mind/console/static/js/dates.mjs";
import { at } from "../../../leeroy_mind/console/static/js/opsmodel.mjs";
import { pausedWords } from "../../../leeroy_mind/console/static/js/gpumodel.mjs";

// local time: build the moment from local parts, so the test holds in any zone
const sec = new Date(2026, 10, 7, 9, 5, 3).getTime() / 1000, late = new Date(2026, 0, 31, 23, 59, 59).getTime() / 1000;

test("one local, 24-hour, ISO-like format", () => {
  assert.equal(isoDate(sec), "2026-11-07");
  assert.equal(hourMinute(sec), "09:05");
  assert.equal(isoMinute(sec), "2026-11-07 09:05");
  assert.equal(isoSecond(sec), "2026-11-07 09:05:03");
  assert.equal(isoMinute(late), "2026-01-31 23:59");
  for (const bad of [null, undefined, NaN, "1", Infinity]) {
    assert.equal(isoDate(bad), ""); assert.equal(isoMinute(bad), ""); assert.equal(hourMinute(bad), ""); assert.equal(isoSecond(bad), "");
  }
});

test("the tabs' own time words use it", () => {
  assert.equal(at(sec), "2026-11-07 09:05");
  assert.equal(at(null), "not recorded");
  assert.equal(pausedWords({ paused: true, since: late }), "GPU paused since 23:59");
});

test("no page module formats a time by the browser's locale (the calendar's weekday heading aside)", async () => {
  const fs = await import("node:fs"), path = await import("node:path"), url = await import("node:url");
  const root = path.resolve(path.dirname(url.fileURLToPath(import.meta.url)), "../../../leeroy_mind/console/static/js");
  const found = [];
  (function walk(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) { walk(full); continue; }
      if (!entry.name.endsWith(".mjs")) { continue; }
      const text = fs.readFileSync(full, "utf8");
      const hits = (text.match(/toLocale(?:Date|Time)?String\(/g) || []).length;
      if (hits && !(entry.name === "calendar.mjs" && hits === 1)) { found.push(entry.name); }
    }
  })(root);
  assert.deepEqual(found, []);
});
