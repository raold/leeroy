import test from "node:test";
import assert from "node:assert/strict";
import { SAMPLE_ORDER, VOICE_NONE, canUndoVoice, voiceRows }
  from "../../../leeroy_mind/console/static/js/voicemodel.mjs";

// an invented sheet
const speech = { register: "plain, clipped", shape: "short flat statements",
                 says: ["noted", "fair enough"], never: ["awesome"], brings_up: ["the allotment", "night shifts"],
                 with_him: "teases him about his sums", stance: "single and in no hurry",
                 samples: { annoyed: "noted. not twice", answer: "depends. what did the log say", warm: "glad you logged on" } };

test("the panel lists her whole sheet, samples in a fixed order", () => {
  assert.deepEqual(voiceRows({ speech }), [
    ["register", "plain, clipped"], ["shape", "short flat statements"], ["says", "noted, fair enough"],
    ["never says", "awesome"], ["brings up", "the allotment; night shifts"],
    ["with him", "teases him about his sums"], ["stance", "single and in no hurry"],
    ["sample: answer", "depends. what did the log say"], ["sample: warm", "glad you logged on"],
    ["sample: annoyed", "noted. not twice"]]);
  assert.deepEqual(SAMPLE_ORDER, ["answer", "tease", "tired", "flirt", "warm", "annoyed"]);
});

test("empty parts are left out and a member without a sheet says so", () => {
  const rows = voiceRows({ speech: { ...speech, never: [], stance: "", samples: {} } });
  assert.deepEqual(rows.map(r => r[0]), ["register", "shape", "says", "brings up", "with him"]);
  assert.deepEqual(voiceRows({ speech: null }), [["voice", VOICE_NONE]]);
  assert.deepEqual(voiceRows({}), [["voice", VOICE_NONE]]);
  assert.deepEqual(voiceRows(null), [["voice", "-"]]);          // no readable persona
  assert.deepEqual(voiceRows({ speech: "dry" }), [["voice", VOICE_NONE]]);
  assert.deepEqual(voiceRows({ speech: { says: "noted", samples: [1] } }), []);  // nothing listable
});

test("undo goes only to voice rows not undone yet", () => {
  assert.equal(canUndoVoice({ id: 7, kind: "voice", undone: false }), true);
  assert.equal(canUndoVoice({ id: 7, kind: "voice", undone: true }), false);
  assert.equal(canUndoVoice({ id: 7, kind: "looks", undone: false }), false);
  assert.equal(canUndoVoice({ kind: "voice" }), false);
  assert.equal(canUndoVoice(null), false);
});
