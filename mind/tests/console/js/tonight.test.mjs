import { test } from 'node:test';
import assert from 'node:assert/strict';
import { SECTIONS, cardModel } from '../../../leeroy_mind/console/static/js/tonightmodel.mjs';

test('words for every section state, and the empty raid night', () => {
  for (const name of SECTIONS) {
    assert.match(cardModel(name,{status:'not_set_up'}).lines[0].text,/Not set up yet/);
    assert.match(cardModel(name,{status:'unavailable'}).lines[0].text,/unavailable/);
    assert.ok(cardModel(name,{status:'ok'}).title);
  }
  assert.match(cardModel('raid',{status:'ok',events:[]}).lines[0].text,/No raid on the calendar tonight/);
  assert.match(cardModel('kit',{status:'unavailable',why:'mind_down'}).lines[0].text,/mind is not running/);
  assert.match(cardModel('kit',{status:'unavailable',why:'no_character'}).lines[0].text,/character could not be found/);
});
test('a count links to its tab', () => {
  const lines = cardModel('waiting',{status:'ok',dms:2,letters:3,selfies:4,polls:null}).lines;
  assert.deepEqual(lines.map(l=>l.href),['#comms','#board','#personas','#guild']);
  assert.match(lines[0].text,/2/); assert.match(lines[3].text,/not set up/);
});
test('guild presence and night failures stay unknown without evidence', () => {
  const lines = cardModel('guild',{status:'ok',night:{date:'2026-11-12',failed_units:null}}).lines;
  assert.match(lines[0].text,/unknown/); assert.match(lines[1].text,/unknown/); assert.match(lines[2].text,/unknown failed/);
});
