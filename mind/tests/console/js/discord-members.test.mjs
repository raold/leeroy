import test from 'node:test';
import assert from 'node:assert/strict';
import { memberStatus } from '../../../leeroy_mind/console/static/js/comms/discord.mjs';

test('member status appears before playing; absent status keeps the old wording', () => {
  assert.deepEqual(memberStatus({ status:'at work', presence:{ playing:true } }), ['at work', 'Playing WoW']);
  assert.deepEqual(memberStatus({ presence:{ playing:true } }), ['Playing WoW']);
  assert.deepEqual(memberStatus({ status:null, presence:{ playing:false } }), []);
});
