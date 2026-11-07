import test from "node:test";
import assert from "node:assert/strict";
import { MIND_DOWN, MORE_TITLE, NO_PHOTO, PAGE, RECENT, countsText, currentText, forgetShots, historyRows,
         leadText, pageOf, pickLabel, recentRows, refocusSelector, whyOff } from "../../../leeroy_mind/console/static/js/selfiemodel.mjs";
import { photoKindText, when } from "../../../leeroy_mind/console/static/js/words.mjs";

// The "Choose selfies" card's words and pages from /api/selfies ({view, mind, control}) and a
// member's photos from /api/selfies/member. Invented members only.

var NAMES = { 101: "Arkon", 102: "Belra", 103: "Coros" };

function sel(o) {
  o = o || {};
  return { view: Object.assign({ available: true, missing: [],
                                 counts: { chosen: 1, waiting: 2, queued: 1, no_persona: 1, no_looks: 3, no_kind: 0 },
                                 queue: [], recent: [] }, o.view || {}),
           mind: o.mind || { ok: true, enabled: true, recent: [] },
           control: o.control || { enabled: true, reason: "" } };
}

test("the counts read chosen, waiting, rendering tonight, no looks yet and no persona", () => {
  assert.equal(countsText(sel().view.counts), "1 chosen · 2 waiting · 1 rendering tonight · 3 no looks yet · 1 no persona");
  assert.equal(countsText({}), "0 chosen · 0 waiting · 0 rendering tonight · 0 no looks yet · 0 no persona");
});

test("members with looks but no photo kind yet are counted only when there are some", () => {
  assert.equal(countsText({ chosen: 0, waiting: 0, queued: 0, no_persona: 0, no_looks: 1, no_kind: 2 }),
               "0 chosen · 0 waiting · 0 rendering tonight · 1 no looks yet · 2 no photo kind yet · 0 no persona");
});

test("the lead says how many members have photos waiting, or that none are", () => {
  assert.equal(leadText(sel().view),
               "2 members have four photos waiting. Click the one that fits; it becomes their photo everywhere.");
  assert.equal(leadText(sel({ view: { counts: { waiting: 1 } } }).view),
               "1 member has four photos waiting. Click the one that fits; it becomes their photo everywhere.");
  assert.equal(leadText(sel({ view: { counts: { waiting: 0 } } }).view),
               "No photos are waiting. The night renders four for every member without one.");
});

test("an older mind.db says photos are unavailable and how to add them", () => {
  var v = sel({ view: { available: false, missing: ["selfie_set", "selfie_pick"] } }).view;
  assert.equal(leadText(v), "This mind.db has no photo tables yet (selfie_set, selfie_pick): "
               + "`db migrate` with the mind stopped adds them.");
});

test("changes can be sent only with control on and the mind up; control's reason comes first", () => {
  assert.equal(whyOff(sel()), "");
  assert.equal(MIND_DOWN, "The mind isn't running, so it can't set photos. Start it from the Mind tab.");
  assert.equal(whyOff(sel({ mind: { ok: false, error: MIND_DOWN } })), MIND_DOWN);
  assert.equal(whyOff(sel({ mind: { ok: false } })), MIND_DOWN);
  // control off comes first, as the server and the dials card say it (final review minor 1)
  assert.equal(whyOff(sel({ mind: { ok: false, error: MIND_DOWN },
                            control: { enabled: false, reason: "process control is off (--no-control)" } })),
               "process control is off (--no-control)");
  assert.equal(whyOff(sel({ mind: { ok: false, error: "no control endpoint here" }, control: { enabled: false } })),
               "control is off");
  assert.equal(whyOff(sel({ control: { enabled: false, reason: "process control is off (--no-control)" } })),
               "process control is off (--no-control)");
  assert.equal(whyOff(sel({ control: { enabled: false } })), "control is off");
  assert.equal(whyOff(sel({ mind: { ok: false, error: "Photos are off in this mind." } })), "Photos are off in this mind.");
  assert.equal(whyOff(null), "Reading the photos.");
});

test("five members a page, the page kept in range", () => {
  assert.equal(PAGE, 5);
  var q = [];
  for (var g = 101; g <= 112; g++) { q.push({ guid: g, name: "M" + g, set: 1, reason: "first", rendered: g }); }
  var p = pageOf(q, 0);
  assert.deepEqual(p.rows.map(function (r) { return r.guid; }), [101, 102, 103, 104, 105]);
  assert.equal(p.pages, 3);
  assert.equal(p.text, "members 1-5 of 12");
  p = pageOf(q, 2);
  assert.deepEqual(p.rows.map(function (r) { return r.guid; }), [111, 112]);
  assert.equal(p.text, "members 11-12 of 12");
  assert.equal(pageOf(q, 9).at, 2);    // a page that shrank away shows the last one
  assert.equal(pageOf(q, -1).at, 0);
  var one = pageOf(q.slice(0, 5), 0);
  assert.equal(one.pages, 1);
  assert.equal(one.text, "");
  assert.deepEqual(pageOf(null, 0), { rows: [], at: 0, pages: 1, text: "" });
});

test("each photo button is named for its place and the member", () => {
  assert.equal(pickLabel(0, "Arkon"), "Use photo 1 of 4 for Arkon");
  assert.equal(pickLabel(3, "Arkon"), "Use photo 4 of 4 for Arkon");
  assert.equal(MORE_TITLE, "Pass on these and render four new ones tonight");
});

test("recent choices come from the running mind's list only, with its undoability", () => {
  var s = sel({ view: { recent: [{ id: 9, ts: 50, guid: 103, label: "pick", undoable: true, name: "Coros" }] },
                mind: { ok: true, recent: [{ id: 7, ts: 1000, guid: 101, label: "undo #6", undoable: false },
                                           { id: 6, ts: 900, guid: 102, label: "pick", undoable: true },
                                           { id: 5, ts: 800, guid: 104, label: "pick", undoable: false }] } });
  assert.deepEqual(recentRows(s, NAMES), [
    { id: 7, undoable: false, text: "#7 · " + when(1000) + " · Arkon · undo of #6" },
    { id: 6, undoable: true, text: "#6 · " + when(900) + " · Belra · photo picked" },
    { id: 5, undoable: false, text: "#5 · " + when(800) + " · #104 · photo picked" }]);
  assert.equal(recentRows(sel({ mind: { ok: false, error: MIND_DOWN } }), NAMES), null);  // unknown, never the view's
  var many = [];
  for (var i = 40; i > 0; i--) { many.push({ id: i, ts: i, guid: 101, label: "pick", undoable: false }); }
  assert.equal(recentRows(sel({ mind: { ok: true, recent: many } }), NAMES).length, RECENT);
  assert.equal(recentRows(sel({ mind: { ok: true, recent: [{ id: 2, ts: 1, guid: 101, label: "pick", undoable: "yes" }] } }),
                          NAMES)[0].undoable, false);
});

test("a member's photo now: the photo in force, or the initials circle", () => {
  assert.equal(NO_PHOTO, "No photo yet: a coloured initials circle shows instead.");
  assert.equal(currentText({ guid: 101, current: null, sets: [], history: [] }), NO_PHOTO);
  assert.equal(currentText({ guid: 101, current: { set: 2, index: 3, pick: 12 }, sets: [], history: [] }),
               "Photo 4 of 4 from set 2, chosen by change #12.");
  assert.equal(currentText(null), "");
});

test("a member's history reads as the recent choices do, each with its undoability", () => {
  var m = { guid: 101, current: null, sets: [],
            history: [{ id: 4, ts: 70, guid: 101, label: "undo #3", undoable: true },
                      { id: 3, ts: 60, guid: 101, label: "pick", undoable: false }] };
  assert.deepEqual(historyRows(m), [{ id: 4, undoable: true, text: "#4 · " + when(70) + " · undo of #3" },
                                    { id: 3, undoable: false, text: "#3 · " + when(60) + " · photo picked" }]);
  assert.deepEqual(historyRows({ history: [] }), []);
  assert.deepEqual(historyRows(null), []);
});

test("the photo kind line: kind, framing and setting", () => {
  assert.equal(photoKindText({ kind: "snapshot", framing: "upper_body", setting: "on the deck of a small boat",
                               outfit: "a rain jacket", expression: "squinting" }),
               "snapshot, upper body, on the deck of a small boat");
  assert.equal(photoKindText({ kind: "mirror_selfie", framing: "head_and_shoulders", setting: "" }),
               "mirror selfie, head and shoulders");
  assert.equal(photoKindText(null), "");
});

test("a change forgets only that member's cached sets", () => {
  var cache = new Map([["101:1", 1], ["101:2", 2], ["1011:1", 3], ["102:1", 4]]);
  forgetShots(cache, 101);
  assert.deepEqual(Array.from(cache.keys()), ["1011:1", "102:1"]);
  forgetShots(cache, "x");
  assert.equal(cache.size, 2);
});

test("after a refusal the keyboard goes back to the button that was clicked", () => {
  var pick = { action: "pick", guid: 101, set: 2, index: 3 };
  assert.equal(refocusSelector(pick, true, "sf-status"),
               '#sf-queue [data-guid="101"][data-set="2"] [data-index="3"]');
  assert.equal(refocusSelector({ action: "more", guid: 102 }, true, "sf-status"),
               '#sf-queue [data-guid="102"] [data-more]');
  assert.equal(refocusSelector({ action: "more", guid: 102 }, true, "p-photostatus"), "#p-more");
  // done, or an undo (its row is drawn again): the status line's Undo, else the line itself
  assert.equal(refocusSelector(pick, false, "sf-status"), null);
  assert.equal(refocusSelector({ action: "undo", pick: 4 }, true, "sf-status"), null);
  assert.equal(refocusSelector({ action: "undo", pick: 4 }, true, "p-photostatus"), null);
});
