import test from 'node:test';
import assert from 'node:assert/strict';
import { voiceSeats } from '../../../leeroy_mind/console/static/js/comms/discord.mjs';
test('away seats carry member names and expire after fifteen minutes', () => {
  const seats = {at:1000, rooms:{lore:[101]}};
  const members = [{guid:101,name:'Test101',presence:{state:'online'}}];
  assert.equal(voiceSeats(seats,members,1900)[0].people[0].name,'Test101');
  assert.deepEqual(voiceSeats(seats,members,1901),[]);
  assert.deepEqual(voiceSeats(seats,members,999),[]);
  assert.deepEqual(voiceSeats(seats,members,1000,true),[]);
});
