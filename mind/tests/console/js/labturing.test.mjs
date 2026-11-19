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

import {conversationView} from '../../../leeroy_mind/console/static/js/labmodel.mjs';

test('the Conversation lab reads not set up from anything but an available panel',() => {
  for (const bad of [null, undefined, 'x', [], {available:false}, {available:true}]) {
    const view=conversationView(bad);
    assert.equal(view.available,false); assert.deepEqual(view.runs,[]);
  }
});

test('a lab night reads as a headline, kinds, flags and variants in words',() => {
  const view=conversationView({available:true,tried:3,promoted:1,runs:[{night:'2026-11-16',status:'done',reason:null,
    piece:'reply_human',pairs:12,spent:1500,rubric:1.42,graded:24,human:0.25,kinds:{flirt:1.5,raid:2,bad:'x'},
    flags:{wordy:0.25,echo:0,silent:0.04},
    variants:[{id:7,outcome:'won',delta:0.31,arm:'lab.reply_human.v7',arm_status:'active',diff:'a {+b+}'},
      {id:8,outcome:'regressed',delta:0.5,arm:null,arm_status:null,diff:''},{id:9,outcome:'mystery',delta:null}]}]});
  assert.equal(view.available,true); assert.equal(view.tried,3); assert.equal(view.promoted,1);
  const r=view.runs[0];
  assert.equal(r.headline,'2026-11-16 · reply_human · done · control 1.42 of 3 (old rubric) over 24 answers · judge called her human 25%');
  assert.equal(view.note,'');
  assert.deepEqual(r.kinds,['flirt 1.50','raid 2.00']);
  assert.deepEqual(r.flags,['wordy 25%','said nothing 4%']);
  assert.deepEqual(r.variants,['Variant 7: promoted (+0.31) · arm lab.reply_human.v7 (active) · a {+b+}',
    'Variant 8: a rule check got worse (+0.50)','Variant 9: mystery']);
});


test('a measure-only night shows the judge score, says so and words a leading variant as indicative',() => {
  const view=conversationView({available:true,tried:1,promoted:0,runs:[{night:'2026-11-18',status:'done',reason:null,
    piece:'discord_dm',pairs:12,spent:900,grader:'tells',promote:false,score:3.41,scored:20,rubric:null,graded:0,human:null,
    kinds:{flirt:3.5},flags:{},variants:[{id:4,outcome:'measure_only',delta:0.26,arm:null,arm_status:null,diff:'',prefer:{n:16,share:0.625}},
      {id:5,outcome:'lost',delta:-0.1,prefer:'x'}]}]});
  assert.equal(view.runs[0].headline,'2026-11-18 · discord_dm · done · control 3.41 of 5 over 20 answers · measure only');
  assert.deepEqual(view.runs[0].variants,['Variant 4: ahead of the control (indicative; not proposed) (+0.26) · preferred to the control’s answer 63% of 16 lines',
    'Variant 5: lost (-0.10)']);
  assert.match(view.note,/^Measure only: /); assert.match(view.note,/indicative/);
  assert.match(view.hint,/scored 1-5 by the lab judge/); assert.doesNotMatch(view.hint,/0-3/);
  assert.match(view.hint,/Variants tried: 1, promoted into the learn bandit: 0\.$/);
});

test('the panel words the scale by the grader each night used, the rubric by default',() => {
  const night=(grader,extra={}) => ({night:'2026-11-18',status:'done',reason:null,piece:'p',grader,promote:false,variants:[],...extra});
  const rubric=conversationView({available:true,tried:0,promoted:0,runs:[night('rubric')]});
  assert.match(rubric.hint,/graded 0-3 by the rubric/); assert.doesNotMatch(rubric.hint,/1-5/);
  assert.match(conversationView({available:true,runs:[]}).hint,/graded 0-3 by the rubric/);
  const mixed=conversationView({available:true,runs:[night('tells'),night('rubric')]});
  assert.match(mixed.hint,/0-3 by the rubric or scored 1-5 by the lab judge, as each night says/);
  const failed=conversationView({available:true,runs:[night('tells',{status:'done',reason:'units_failed',
    variants:[{id:3,outcome:'failed',delta:null}]})]});
  assert.equal(failed.runs[0].headline,'2026-11-18 · p · done (units_failed) · measure only');
  assert.deepEqual(failed.runs[0].variants,['Variant 3: too few scored: judge calls failed']);
});
