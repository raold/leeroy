import { test } from 'node:test';
import assert from 'node:assert/strict';
import { gpuStatus, gpuModel, gpuResult, pausedWords } from '../../../leeroy_mind/console/static/js/gpumodel.mjs';
import { lines } from '../../../leeroy_mind/console/static/js/opsmodel.mjs';
import { cardModel } from '../../../leeroy_mind/console/static/js/tonightmodel.mjs';

const running = {paused:false,since:null,enabled:true,refusal:null};
const paused = {...running,paused:true,since:1794567660};
test('unknown, malformed and read-only status cannot issue GPU changes', () => {
  for (const value of [undefined,{},[],{...running,paused:'false'},{...running,since:NaN},{...running,refusal:7}]) {
    assert.equal(gpuStatus(value),null); assert.equal(gpuModel(value).disabled,true);
  }
  const readOnly = gpuModel({...running,enabled:false,refusal:'This console is read-only.'});
  assert.equal(readOnly.disabled,true); assert.match(readOnly.why,/read-only/);
});
test('pause and resume are driven by server state, with a persistent banner', () => {
  assert.equal(gpuModel(running).action,'pause'); assert.equal(gpuModel(running).banner,'');
  const view = gpuModel(paused,{format:()=> '07:41'});
  assert.equal(view.action,'resume'); assert.equal(view.label,'GPU free · Resume');
  assert.match(view.banner,/freed at 07:41/); assert.match(view.banner,/resume, log into WoW or post in Discord/);
  assert.equal(gpuModel(paused,{busy:true}).disabled,true);
  assert.equal(gpuModel(running,{keyLost:true}).disabled,true);
});
test('refusal remains readable and does not fabricate a paused state', () => {
  const view = gpuModel({...running,refusal:'Log out of WoW first: your game keeps the model loaded'});
  assert.equal(view.paused,false); assert.equal(view.label,'Free GPU'); assert.match(view.why,/Log out/);
  assert.equal(gpuResult({ok:false,error:'gpu_in_game',refusal:view.why}),view.why);
});
test('results distinguish unloading with a stopped mind from a persistent pause', () => {
  assert.equal(gpuResult({ok:true,paused:true,freed_bytes:17.1*1073741824}),'Freed 17.1 GB');
  assert.equal(gpuResult({ok:true,mind_down:true,unloaded:['chat','embed']}),'The mind is stopped; unloaded 2 models.');
  assert.match(gpuResult({ok:true,paused:false}),/resuming/);
  assert.match(gpuResult({ok:false}),/did not confirm/);
  assert.equal(gpuModel({...paused,mind_down:true}).action,'pause');
  assert.equal(gpuModel({...paused,mind_down:true}).label,'Free GPU');
});
test('partial unloads and later native loads keep a reachable retry instead of claiming free memory', () => {
  for (const state of [{...paused,unload_failed:true},{...paused,loaded:[{name:'chat',size_vram:1}]}]) {
    const view = gpuModel(state);
    assert.equal(view.action,'pause'); assert.equal(view.label,'Retry Free GPU');
    assert.match(view.banner,/may still be loaded/); assert.doesNotMatch(view.banner,/GPU freed/);
  }
});
test('Ops switches and Tonight presence show the same retained pause', () => {
  assert.match(pausedWords(paused,()=> '07:41'),/GPU paused since 07:41/);
  assert.match(lines('switches',{status:'ok',rows:[],gpu:paused})[0],/GPU paused since/);
  assert.ok(cardModel('guild',{status:'ok',gpu:paused}).lines.some(l=>l.text.startsWith('GPU paused since')));
  assert.equal(pausedWords(running),'');
});
