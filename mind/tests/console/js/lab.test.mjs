import test from 'node:test';
import assert from 'node:assert/strict';
import {controlReason,weightValues,vote,score,reconcileVote} from '../../../leeroy_mind/console/static/js/labmodel.mjs';
import {validView} from '../../../leeroy_mind/console/static/js/resilience.mjs';
test('learning controls fail closed without a running mind and enabled control',() => {
  assert.ok(controlReason({})); assert.ok(controlReason({control:{enabled:true},mind:{state:'unknown'}}));
  assert.equal(controlReason({control:{enabled:true},mind:{state:'running'}}),'');
});
test('weights are finite, bounded and sum to one; missing/blank inputs cannot become zero silently',() => {
  const good={explicit:.4,engagement:.25,emote:.15,quality:.2}; assert.deepEqual(weightValues(good),good);
  for (const bad of [{}, {...good,quality:NaN},{...good,quality:Infinity},{...good,quality:.3},{...good,explicit:true}]) {
    assert.equal(weightValues(bad),null);
  }
});
test('feedback only preserves known reason chips and an explicit thumbs choice',() => {
  assert.deepEqual(vote(-1,['weird','invented','weird']),{value:-1,reasons:['weird']});
  assert.deepEqual(vote(0,null),{value:null,reasons:[]});
  assert.equal(score(null),'Not scored yet'); assert.equal(score(0),'0.000');
});
test('malformed Lab answers do not erase a good tab',() => {
  assert.equal(validView('/api/lab',{freeze:false}),false);
});
test('a later stored vote wins; an older snapshot cannot undo a local acknowledgement',() => {
  const current={value:1,reasons:['funny'],revision:5};
  assert.deepEqual(reconcileVote(current,{value:-1,reasons:[],revision:4}),current);
  assert.deepEqual(reconcileVote(current,{value:-1,reasons:['weird'],revision:6}),{value:-1,reasons:['weird'],revision:6});
});

test('a saved event received during a request survives its older HTTP acknowledgement',() => {
  let saved={value:1,reasons:['funny'],revision:1};
  saved=reconcileVote(saved,{value:-1,reasons:['weird'],revision:3});
  saved=reconcileVote(saved,{value:1,reasons:['funny'],revision:2});
  assert.deepEqual(saved,{value:-1,reasons:['weird'],revision:3});
});
