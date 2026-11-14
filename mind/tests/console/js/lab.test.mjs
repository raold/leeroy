import test from 'node:test';
import assert from 'node:assert/strict';
import {controlReason,weightValues,vote,score,reconcileVote,reportView,versionLabel,rollbackWords} from '../../../leeroy_mind/console/static/js/labmodel.mjs';
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

test('the weekly report reads as numbers and ids, with the diversity floor and held reasons',() => {
  assert.equal(reportView(null),null); assert.equal(reportView({rates:{}}),null);
  const view=reportView({version:1,week:'2026-W46',previous_week:'2026-W45',state_version:12,
    rates:{week:{lines:40,repetition:.05,stock:0,nonsequitur:0,unawareness:0,guard:.1},previous:{lines:30,repetition:.02}},
    measured:{contradiction:'not_measured',nonsequitur:'partial',unawareness:'not_measured'},
    diversity:{ok:0,repetition_rose:1,stock_rose:0,rose:['discord:repetition']},
    promoted:[{kind:'arm',id:'length.1.v1',context:['whisper','none','natural'],probability:.95,uses:31}],
    retired:[{kind:'lesson',id:7,guid:101,reason:'not_reinforced'},{kind:'arm',id:'humour.2.v1',reason:'share',share:.01,share_previous:.02,probability:.2}],
    rolled_back:[{kind:'arm',id:'tease.1.v1',context:['guild','none','chatty'],reasons:['repetition']}],
    held:{count:3,items:[{kind:'lesson',id:9,guid:102,reasons:['uses','diversity','owner_rollback']}]}});
  assert.match(view.heading,/2026-W46, compared with 2026-W45\. The learning state before it is version 12\./);
  assert.equal(view.lines,'40 lines last week, 30 the week before.');
  assert.match(view.diversity,/repetition in Discord rooms rose significantly/);
  assert.ok(view.rates.includes('Repetition 5.0% (week before 2.0%)'));
  assert.ok(view.rates.some(r => r.startsWith('Contradiction: not measured')));
  assert.ok(view.rates.some(r => r.startsWith('Non sequiturs: partial')));
  assert.ok(view.rates.includes('Unawareness: not measured.'));
  assert.deepEqual(view.promoted,['arm length.1.v1 in whisper / none / natural · P 0.950 · 31 uses']);
  assert.deepEqual(view.retired,['lesson 7 (member #101) · not reinforced in 14 days','arm humour.2.v1 · share 1.0%, week before 2.0% · P 0.200']);
  assert.deepEqual(view.rolled_back,['arm tease.1.v1 in guild / none / chatty · repetition']);
  assert.deepEqual(view.held,{count:3,items:['lesson 9 (member #102) · under 30 rewarded uses, diversity floor, reverted by your rollback (waits for 30 new uses)']});
  assert.match(reportView({week:'2026-W46',diversity:{ok:1}}).diversity,/held/);
});
test('saved versions say what they hold',() => {
  assert.equal(versionLabel({version:4,reason:'cycle',week:'2026-W46'}),'Version 4 · before the weekly cycle for 2026-W46');
  assert.equal(versionLabel({version:5,reason:'rollback',restored_from:4}),'Version 5 · saved before a rollback to 4');
  assert.match(rollbackWords(4,false),/Reward weights stay as they are\./);
  assert.match(rollbackWords(4,true),/Reward weights return to that version too\./);
  assert.match(rollbackWords(4,false),/later decisions of yours and the guards, your vetoes and freezes stay/);
});
