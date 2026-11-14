import { test } from 'node:test';
import assert from 'node:assert/strict';
import { memberOutreachModel, tonightOutreachLine } from '../../../leeroy_mind/console/static/js/outreachmodel.mjs';
import { cardModel } from '../../../leeroy_mind/console/static/js/tonightmodel.mjs';

const counts = {open:2,told:3,dropped:1};

test('off adds no member panel or Tonight line', () => {
  assert.equal(memberOutreachModel(undefined),null);
  assert.equal(tonightOutreachLine(undefined),null);
  assert.equal(cardModel('waiting',{status:'ok'}).lines.length,4);
});

test('member counts project only known kinds and states, never topic fields', () => {
  const value = {status:'ok',kinds:{life:{...counts,about:'PRIVATE'},
    dm_nudge:counts,PRIVATE:counts},totals:counts,about:'PRIVATE',ref:'PRIVATE'};
  const result = memberOutreachModel(value);
  assert.deepEqual(result.rows,[{label:'Life',values:[2,3,1]},
    {label:'DM reminders',values:[2,3,1]}]);
  assert.deepEqual(result.totals,[2,3,1]);
  assert.equal(JSON.stringify(result).includes('PRIVATE'),false);
});

test('empty, absent and failed sources have distinct words', () => {
  assert.match(memberOutreachModel({status:'ok',kinds:{},totals:{open:0,told:0,dropped:0}}).why,/Nothing on this list/);
  assert.match(memberOutreachModel({status:'not_set_up'}).why,/Not set up/);
  assert.match(memberOutreachModel({status:'unavailable',why:'PRIVATE'}).why,/unavailable/);
  assert.equal(tonightOutreachLine({status:'ok',count:0}),'Things members want to tell you: 0');
  assert.match(tonightOutreachLine({status:'not_set_up'}),/not set up yet/);
  assert.match(tonightOutreachLine({status:'unavailable'}),/unavailable/);
});

test('invalid counts cannot masquerade as zero or render arbitrary text', () => {
  for (const bad of [-1,NaN,true,'PRIVATE',Infinity]) {
    assert.match(tonightOutreachLine({status:'ok',count:bad}),/unavailable/);
    const result = memberOutreachModel({status:'ok',kinds:{life:{...counts,open:bad}},totals:counts});
    assert.deepEqual(result.rows,[]);
    assert.match(result.why,/unavailable/);
  }
});

test('Tonight count survives an unrelated missing card source and appears only once', () => {
  const result = cardModel('waiting',{status:'not_set_up'},{status:'ok',count:4,about:'PRIVATE'});
  assert.deepEqual(result.lines.map(r=>r.text),['Not set up yet.','Things members want to tell you: 4']);
  assert.equal(result.lines[1].href,'#personas');
  assert.equal(cardModel('guild',{status:'ok'},{status:'ok',count:4}).lines.some(r=>r.text.includes('Things members')),false);
});
