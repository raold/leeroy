import test from 'node:test';
import assert from 'node:assert/strict';
import { Discord, voiceSeats } from '../../../leeroy_mind/console/static/js/comms/discord.mjs';
test('away seats carry member names and expire after fifteen minutes', () => {
  const seats = {at:1000, server_now:1000, received_at:1000, rooms:{lore:[101]}};
  const members = [{guid:101,name:'Test101',presence:{state:'online'}}];
  assert.equal(voiceSeats(seats,members,1900)[0].people[0].name,'Test101');
  assert.deepEqual(voiceSeats(seats,members,1901),[]);
  assert.deepEqual(voiceSeats(seats,members,999),[]);
  assert.deepEqual(voiceSeats(seats,members,1000,true),[]);
});

test('server age survives a phone wall clock skew and expires without a refresh', () => {
  const original = Date.now;
  const members = [{guid:101,name:'Test101',presence:{state:'online'}}];
  try {
    for (const skew of [-86400, -5, 5, 86400]) {
      Date.now = () => (1000 + skew)*1000;
      const discord = new Discord();
      discord.adopt({voice_seats:{at:1000,rooms:{lore:[101]}},voice_seats_now:1010,members});
      const received = discord.voice_seats.received_at;
      assert.equal(voiceSeats(discord.voice_seats,members,received)[0].people[0].guid,101);
      assert.equal(voiceSeats(discord.voice_seats,members,received+890).length,1);
      assert.deepEqual(voiceSeats(discord.voice_seats,members,received+891),[]);
    }
  } finally { Date.now = original; }
});
