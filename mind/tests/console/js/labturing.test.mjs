import test from 'node:test';
import assert from 'node:assert/strict';
import {turingView,chartPoints,CHART} from '../../../leeroy_mind/console/static/js/labmodel.mjs';

test('the Turing panel reads not set up from anything but an available report',() => {
  for (const bad of [null, undefined, 'x', [], {available:false}, {available:true}]) {
    const view=turingView(bad);
    assert.equal(view.available,false); assert.deepEqual(view.families,[]);
  }
});

test('chart points map 0-1 onto the chart and skip nights with no value',() => {
  const {width,height,pad}=CHART;
  assert.deepEqual(chartPoints([1,0]),[[pad,pad],[width-pad,height-pad]]);
  assert.deepEqual(chartPoints([0.5]),[[width/2,height/2]]);
  assert.equal(chartPoints([0.9,null,NaN,0.7]).length,2);
  assert.deepEqual(chartPoints([2,-1]).map(p => p[1]),[pad,height-pad]); // clamped
  assert.deepEqual(chartPoints(undefined),[]);
});

test('a family reads as a headline, a verdict, tells and nights in words',() => {
  const view=turingView({available:true,days:14,families:[{family:'private',label:'Whispers and DMs',
    points:[{night:'2026-11-13',auc:0.95,speakers:0,judge:null},{night:'2026-11-14',auc:0.9,speakers:4,judge:0.75}],
    latest:{night:'2026-11-14',status:'ok',auc:0.9,low:0.87,high:0.93,sample:700,human_lines:700,member_lines:1100,
      features:[{words:'lines inside a burst of two or more: him 9%, members 67%'},{words:7},null]},
    judge:{speakers:4,accuracy:0.75,windows:2,his:2,his_human:0.5,members:2,members_ai:1,balanced:0.75},
    tells:[{code:'polish',label:'writing too polished or literary',count:3,phrases:['too tidy',5]}]}]});
  assert.equal(view.available,true); const f=view.families[0];
  assert.equal(f.headline,'AUC 0.90 (approx. 0.87–0.93) on 2026-11-14 · 700 lines each');
  assert.equal(f.verdict,'Blind judge: 75% balanced accuracy (50% is chance): members called AI 100% of 2 times, you called human 50% of 2 times.');
  assert.deepEqual(f.features,['lines inside a burst of two or more: him 9%, members 67%']);
  assert.deepEqual(f.tells,['writing too polished or literary (3) · “too tidy”']);
  assert.deepEqual(f.nights,['2026-11-13 · AUC 0.95','2026-11-14 · AUC 0.90 · judge 75% balanced (4 speakers)']);
  assert.equal(f.auc.length,2); assert.equal(f.judge.length,1);
});

test('a family not measured says why, and an unjudged one says so',() => {
  const f=turingView({available:true,families:[{family:'discord',label:'Discord channels',points:[],
    latest:{night:'2026-11-14',status:'too_few',human_lines:5,member_lines:1004},judge:{speakers:0},tells:[]}]}).families[0];
  assert.equal(f.headline,'Not measured on 2026-11-14: too few lines to compare (5 of yours, 1004 of theirs)');
  assert.equal(f.verdict,'Blind judge: no windows judged yet.');
  assert.equal(turingView({available:true,families:[{label:'Game chat'}]}).families[0].headline,'No style measurement stored yet.');
});

test('the judge is charted and headlined as balanced accuracy, never raw accuracy',() => {
  // an always-AI judge: raw 75%, balanced 50%, which the chart puts on the chance line
  const f=turingView({available:true,families:[{label:'Game chat',points:[{night:'n1',auc:0.8,speakers:16,judge:0.5}],
    judge:{speakers:16,accuracy:0.75,windows:4,his:4,his_human:0,members:12,members_ai:1,balanced:0.5}}]}).families[0];
  assert.match(f.verdict,/^Blind judge: 50% balanced accuracy/);
  assert.doesNotMatch(f.verdict,/75%/);
  assert.deepEqual(f.judge,[[CHART.width/2,CHART.height/2]]);
  const one=turingView({available:true,families:[{label:'Discord channels',points:[],
    judge:{speakers:6,accuracy:0.83,windows:2,his:0,members:6,members_ai:0.83,balanced:null}}]}).families[0];
  assert.equal(one.verdict,'Blind judge: members called AI 83% of 6 times; balanced accuracy needs both sides.');
});
