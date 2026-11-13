import test from 'node:test';
import assert from 'node:assert/strict';
import { NOTE_BYTES, NOTE_RATE, NOTE_SECONDS, mono, resample, wav, audioUrl, noteTime, playerState, VoiceRecorder }
  from '../../../leeroy_mind/console/static/js/comms/voicenotes.mjs';
import { api } from '../../../leeroy_mind/console/static/js/core.mjs';
import { validView } from '../../../leeroy_mind/console/static/js/resilience.mjs';
import { activityLabel, typingByView } from '../../../leeroy_mind/console/static/js/comms/typing.mjs';

test('stereo mixes evenly before exact 48 kHz to 16 kHz downsampling', () => {
  const samples = mono([new Float32Array([1,0,-1,1,1,1]), new Float32Array([1,0,-1,-1,-1,-1])]);
  assert.deepEqual([...samples], [1,0,-1,0,0,0]);
  assert.deepEqual([...resample(samples,48000)], [0,0]);
  assert.deepEqual([...resample(new Float32Array([0,1,0,-1]),8000)], [0,.5,1,.5,0,-.5,-1,-1]);
});
test('non-integral resampling keeps duration and a constant signal', () => {
  const samples = new Float32Array(44100).fill(.25), result = resample(samples,44100);
  assert.equal(result.length,16000);
  assert.ok(result.every(value => value === .25));
  assert.notEqual(resample(samples,44100,44100),samples);
});
test('WAV header and signed little-endian samples are exact', () => {
  const samples = new Float32Array(3200); samples.set([1,-1,.5,-.5]);
  const bytes = wav(samples), view = new DataView(bytes), chars = (start,end) => new TextDecoder().decode(bytes.slice(start,end));
  assert.equal(chars(0,4),'RIFF'); assert.equal(chars(8,16),'WAVEfmt '); assert.equal(chars(36,40),'data');
  assert.equal(view.getUint32(4,true),6436); assert.equal(view.getUint32(16,true),16);
  assert.equal(view.getUint16(20,true),1); assert.equal(view.getUint16(22,true),1);
  assert.equal(view.getUint32(24,true),16000); assert.equal(view.getUint32(28,true),32000);
  assert.equal(view.getUint16(32,true),2); assert.equal(view.getUint16(34,true),16);
  assert.equal(view.getUint32(40,true),6400);
  assert.deepEqual([...new Uint8Array(bytes,44,8)],[255,127,0,128,0,64,0,192]);
});
test('a full minute fits the upload cap, and malformed or out-of-bounds audio is refused', () => {
  assert.ok(wav(new Float32Array(NOTE_RATE*NOTE_SECONDS)).byteLength < NOTE_BYTES);
  for (const samples of [new Float32Array(1),new Float32Array(16000*60+1),new Float32Array(3200).fill(NaN)]) {
    assert.throws(() => wav(samples));
  }
  assert.throws(() => wav(new Float32Array(3200),44100));
  assert.throws(() => mono([])); assert.throws(() => mono([new Float32Array(2),new Float32Array(3)]));
  assert.throws(() => mono([new Float32Array([Infinity])]));
  assert.throws(() => resample(new Float32Array([1]),0));
});

function pending() { let resolve; const promise = new Promise(r => {resolve=r;}); return {promise,resolve}; }
async function flush() { for (let i=0;i<15;i++) { await Promise.resolve(); } }
function rig(overrides = {}) {
  let now=0, timer=null, stopped=0, starts=0, stops=0;
  const stream={getTracks:() => [{stop:() => {stopped++;}}]};
  const media={state:'inactive',mimeType:'audio/webm',
    start() {this.state='recording'; starts++;},
    stop() {this.state='inactive'; stops++; queueMicrotask(() => {
      this.ondataavailable?.({data:new Blob(['fake'])}); this.onstop?.();
    });}};
  const phases=[];
  const recorder=new VoiceRecorder({capture:async()=>stream,makeRecorder:()=>media,
    decode:async()=>new Blob([wav(new Float32Array(3200))],{type:'audio/wav'}),
    now:()=>now,every:fn=>{timer=fn;return 1;},clear:()=>{timer=null;},changed:r=>phases.push(r.phase),...overrides});
  return {recorder,stream,media,phases,stats:()=>({stopped,starts,stops,timer}),tick:ms=>{now=ms;timer?.();}};
}
test('a late microphone grant after Escape releases every track and never records', async () => {
  const permission=pending(), r=rig({capture:()=>permission.promise});
  const start=r.recorder.start(); assert.equal(r.recorder.phase,'asking');
  r.recorder.cancel(); permission.resolve(r.stream); await start;
  assert.equal(r.recorder.phase,'idle'); assert.equal(r.stats().stopped,1); assert.equal(r.stats().starts,0);
});
test('one stop flushes recording, releases the microphone and produces a preview', async () => {
  const r=rig(); await r.recorder.start(); assert.equal(r.recorder.phase,'recording');
  await r.recorder.start(); assert.equal(r.stats().starts,1);
  r.recorder.stop(); r.recorder.stop(); await flush();
  assert.equal(r.stats().stops,1); assert.equal(r.stats().stopped,1); assert.equal(r.stats().timer,null);
  assert.equal(r.recorder.phase,'preview'); assert.equal(r.recorder.blob.type,'audio/wav');
  assert.ok(r.phases.includes('encoding'));
});
test('the minute timer stops capture without a second click', async () => {
  const r=rig(); await r.recorder.start(); r.tick(59999); assert.equal(r.recorder.phase,'recording');
  r.tick(60001); await flush(); assert.equal(r.recorder.phase,'preview'); assert.equal(r.stats().stops,1);
});
test('a cancelled decode cannot restore a discarded draft or leak into another conversation', async () => {
  const encoding=pending(), r=rig({decode:()=>encoding.promise});
  await r.recorder.start(); r.recorder.stop(); await flush(); assert.equal(r.recorder.phase,'encoding');
  r.recorder.cancel(); encoding.resolve(new Blob(['old recording'])); await flush();
  assert.equal(r.recorder.phase,'idle'); assert.equal(r.recorder.blob,null);
});
test('permission and recorder failures are worded and release captured tracks', async () => {
  const denied=rig({capture:async()=>{throw Object.assign(new Error(),{name:'NotAllowedError'});}});
  await denied.recorder.start(); assert.match(denied.recorder.error,/permission was refused/);
  assert.equal(denied.recorder.phase,'idle');
  const failed=rig({makeRecorder:()=>{throw new Error('Recording is unavailable.');}});
  await failed.recorder.start(); assert.equal(failed.stats().stopped,1); assert.equal(failed.recorder.phase,'idle');
});
test('oversized decoded notes never become sendable drafts', async () => {
  const r=rig({decode:async()=>new Blob([new Uint8Array(NOTE_BYTES+1)])});
  await r.recorder.start(); r.recorder.stop(); await flush();
  assert.equal(r.recorder.phase,'idle'); assert.match(r.recorder.error,/too large/); assert.equal(r.recorder.blob,null);
});
test('player positions clamp to duration and loading and paused states have clear labels', () => {
  assert.equal(noteTime(60),'1:00'); assert.equal(noteTime(NaN),'0:00');
  assert.deepEqual(playerState({seconds:99,duration:1,playing:true}),{
    seconds:1,duration:1,label:'Pause voice note',elapsed:'0:01',total:'0:01',disabled:false,error:''});
  assert.equal(playerState({loading:true}).disabled,true);
  assert.equal(playerState({seconds:-1,duration:NaN}).seconds,0);
  assert.equal(audioUrl(41),'/api/discord/voice?id=41');
  for (const id of ['../voice',0,-1,Infinity,1.5]) { assert.throws(()=>audioUrl(id)); }
});
test('authenticated voice responses allow 2 MiB audio but refuse oversize and wrong MIME', async t => {
  let size=1500000, mime='audio/wav';
  t.mock.method(globalThis,'fetch',async()=>new Response(new Uint8Array(size),{headers:{'content-type':mime}}));
  assert.equal((await (await api(audioUrl(41))).blob()).size,size);
  size=NOTE_BYTES+1; await assert.rejects(api(audioUrl(41)));
  size=400; mime='text/html'; await assert.rejects(api(audioUrl(41)),/could not be read/);
});
test('malformed voice metadata cannot replace a valid Discord view', () => {
  const data={categories:[],dms:[],members:[],human:{guid:1,name:'Owner'},missing:[],voice_notes:true,
    channel:'dm:101',messages:[{reactions:[],voice_note:{origin:'mind',ms:1000,bytes:32044,transcript_ok:true}}],threads:[],pins:[],quotes:[]};
  assert.equal(validView('/api/discord?channel=dm:101',data),true);
  for (const bad of [{ms:0},{bytes:NOTE_BYTES+1},{origin:'unknown'},{transcript_ok:'yes'}]) {
    assert.equal(validView('/api/discord?channel=dm:101',{...data,messages:[{reactions:[],voice_note:{...data.messages[0].voice_note,...bad}}]}),false);
  }
});
test('a member recording a DM has a recording strip that still expires normally', () => {
  const entries=typingByView([{guid:101,name:'Arkon',channel:'dm:101',to:1,since:10,age:2,phase:'recording'}],{human:1});
  assert.equal(activityLabel(entries.get('dm:101')),'Arkon is recording a voice message');
  assert.equal(activityLabel([]),''); assert.equal(activityLabel([{name:'Arkon'}]),'Arkon is typing');
  assert.equal(typingByView([{guid:101,channel:'dm:101',to:1,since:10,age:31,phase:'recording'}],{human:1}).size,0);
});
