import test from "node:test";
import assert from "node:assert/strict";
import { actionWords, confirmationWords } from "../../../leeroy_mind/console/static/js/opsactionmodel.mjs";
test("action words distinguish disabled, idle, pending and terminal", () => {
  assert.match(actionWords({enabled:false}), /off/);
  assert.match(actionWords({enabled:true,operation:null}), /confirmation/);
  assert.equal(actionWords({enabled:true,operation:{state:"running",message:"Waiting for terminal."}}), "Waiting for terminal.");
  assert.equal(actionWords({enabled:true,operation:{state:"done",message:"Stopped."}}), "Stopped.");
  assert.match(actionWords([]), /unavailable/);
});
test("release confirmation gives identity and consequences without dumping extras", () => {
  const text = confirmationWords("release", {id:"a".repeat(32),kind:"walk",age_s:100,members:3,native_reason:"timeout",secret:"PRIVATE"});
  ["walk","100","3 members","timeout","restarts","fences"].forEach(s=>assert.ok(text.includes(s)));
  assert.ok(!text.includes("PRIVATE"));
  assert.match(confirmationWords("offline_stop"), /terminal/);
});
