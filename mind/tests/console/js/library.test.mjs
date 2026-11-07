import test from "node:test";
import assert from "node:assert/strict";
import { KINDS, isPre, cleanQuery, queryProblem, searchPath, statsLine, wantsDeeper, answerNote, NOTHING } from "../../../leeroy_mind/console/static/js/librarymodel.mjs";

test("the twelve kinds, syntax kinds preformatted", () => {
  assert.equal(KINDS.length, 12);
  for (const k of ["command", "teleport", "documentation", "api"]) { assert.equal(isPre(k), true, k); }
  for (const k of ["item", "lore", "quest", "strategy"]) { assert.equal(isPre(k), false, k); }
});
test("a question is whitespace-collapsed and 1 to 200 characters", () => {
  assert.equal(cleanQuery("  a \n b\t"), "a b");
  assert.ok(queryProblem("   "));
  assert.ok(queryProblem("a".repeat(201)));
  assert.equal(queryProblem("a".repeat(200)), null);
  assert.equal(queryProblem("weapon  skill"), null);
});
test("the path encodes the question and names only a real kind", () => {
  assert.equal(searchPath("a & b?", "", false), "/api/library?q=a%20%26%20b%3F");
  assert.equal(searchPath("x", "item", false), "/api/library?q=x&kind=item");
  assert.equal(searchPath("x", "nope", true), "/api/library?q=x&deeper=1");
  assert.equal(searchPath("x", "api", true), "/api/library?q=x&kind=api&deeper=1");
});
test("the stats line lists kinds in order and leaves out empty ones", () => {
  assert.equal(statsLine({ total: 18, kinds: { command: 3, item: 12, lore: 0, teleport: 3 } }),
               "18 passages: items 12, commands 3, teleports 3");
  assert.equal(statsLine({ total: 1, kinds: { item: 1 } }), "1 passage: items 1");
  assert.equal(statsLine(null), "0 passages");
});
test("a text search that finds nothing goes on to the mind, once", () => {
  assert.equal(wantsDeeper({ setup: true, hits: [] }, false), true);
  assert.equal(wantsDeeper({ setup: true, hits: [{}] }, false), false);
  assert.equal(wantsDeeper({ setup: true, hits: [] }, true), false);
  assert.equal(wantsDeeper({ setup: false, hits: [] }, false), false);
  assert.equal(wantsDeeper(null, false), false);
});
test("what is said under an answer", () => {
  assert.deepEqual(answerNote({ setup: true, hits: [{}] }, false), { text: "", bad: false });
  assert.deepEqual(answerNote({ setup: true, hits: [] }, false), { text: NOTHING, bad: false });
  assert.equal(NOTHING, "Nothing in the library matches that.");
  assert.equal(answerNote({ setup: false, why: "no db" }, false).text, "Not set up yet: no db");
  assert.deepEqual(answerNote({ setup: true, hits: [], deeper: true, deeper_why: "The mind is down." }, true),
                   { text: "The mind is down.", bad: true });
});
