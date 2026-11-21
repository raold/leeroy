import test from "node:test";
import assert from "node:assert/strict";
import { overview, bossLines, bossTitle, unassignedLines, offlineLines } from "../../../leeroy_mind/console/static/js/debriefmodel.mjs";

test("unknown evidence and empty runs cannot read as successful verification", () => {
  assert.match(overview({}).join(" "), /No recorded raid/);
  assert.match(bossLines({pulls:0,mechanics:[{name:"Essences",evidence:"no_direct_action_telemetry"}]}).join(" "), /success unverified/);
  assert.match(unassignedLines({}).join(" "), /success unverified/);
  assert.match(bossTitle({boss:"Test boss",pulls:2,kills:1,wipes:1}), /2 pulls/);
});
test("moves are participations; every rez reason and short/full wipe record is readable", () => {
  assert.match(bossLines({mechanics:[{name:"Move",evidence:"response_logged",activations:2,moved_total:8,moved_max:5,cancelled:1}]}).join(" "), /8 move participations · max 5 bots/);
  const p = {group_rez:[{reason:"combat",records:2},{reason:"no_spell",records:1}],offline:[
    {event:"offline_short",source:"native",present:7,min:8,leader_present:false},
    {event:"offline_full_wipe",source:"native",dead:8,restarts:2}]};
  assert.match(unassignedLines(p).join(" "), /combat.*no_spell/);
  assert.match(offlineLines(p).join(" "), /offline_short.*present 7.*min 8.*false.*offline_full_wipe.*restarts 2/);
});
test("the report never generically dumps a raw message or private payload", () => {
  const privateText = "PRIVATE_SENTINEL";
  const p = {raid:{id:1},raw:privateText,msg:privateText,offline:[{msg:privateText,payload:privateText}],
    group_rez:[{payload:privateText}],hazards:[{msg:privateText}],unassigned_responses:[{raw:privateText}]};
  assert.ok(![...overview(p),...unassignedLines(p),...offlineLines(p),...bossLines(p)].join(" ").includes(privateText));
});
