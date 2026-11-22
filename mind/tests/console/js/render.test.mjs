import { test } from 'node:test';
import assert from 'node:assert/strict';
import { confirmLines, durationWords, kindsWords, outcomeWords, renderModel, renderResult,
         renderStatus } from '../../../leeroy_mind/console/static/js/rendermodel.mjs';
import { lines } from '../../../leeroy_mind/console/static/js/opsmodel.mjs';

const backlog = {first: 3, more: 6, snaps: 2, held: 1, refused: 0, total: 11, images: 38,
                 estimate_s: 840};
const idle = {selfies: true, snaps: true, running: false, stopping: false, progress: null, backlog,
              refusal: null, code: null, last: null, enabled: true, mind_down: false};
const running = {...idle, backlog: null, running: true,
                 progress: {done: 2, total: 11, phase: 'sets', first: 3, more: 6, snaps: 2}};

test('unknown, malformed and read-only status cannot start a render', () => {
  for (const value of [undefined, null, {}, [], {...idle, running: 'no'}, {...idle, enabled: 1},
                       {...idle, selfies: undefined}, {...idle, refusal: 7}]) {
    assert.equal(renderStatus(value), null);
    assert.equal(renderModel(value).disabled, true);
  }
  const readOnly = renderModel({...idle, enabled: false, refusal: 'Control is off.'});
  assert.equal(readOnly.disabled, true); assert.equal(readOnly.why, 'Control is off.');
  assert.equal(renderModel(idle, {keyLost: true}).disabled, true);
  assert.equal(renderModel(idle, {busy: true}).disabled, true);
});

test('idle shows the backlog count and confirms before rendering', () => {
  const view = renderModel(idle);
  assert.equal(view.label, 'Render selfies (11)'); assert.equal(view.action, 'render');
  assert.equal(view.disabled, false);
  assert.match(view.title, /3 first sets, 6 sets of four more, 2 situational photos/);
  const words = confirmLines(idle);
  assert.equal(words[0], 'Renders 11 now: 3 first sets, 6 sets of four more, 2 situational photos.');
  assert.match(words[1], /1 held set stays queued/);
  assert.match(words[2], /Rough time: about 14 minutes, from earlier renders\./);
  for (const estimate_s of [null, 0]) {
    assert.equal(confirmLines({...idle, backlog: {...backlog, estimate_s}})[2],
                 'Rough time: no estimate yet (no render has been timed).');
  }
  assert.match(confirmLines({...idle, backlog: {...backlog, refused: 2}})[2],
               /^2 sets the photo check refused today wait for the night/);
  assert.match(words.at(-1), /log into WoW or post in Discord, or when an answer owed to you/);
});

test('refusals stay readable on a disabled button', () => {
  for (const refusal of ['Log out of WoW first: photos render only while you are out of the game.',
                         'The GPU is paused. Resume the GPU first.', 'Nothing is queued to render.',
                         'Checking what can render…']) {
    const view = renderModel({...idle, refusal, backlog: {...backlog, total: 0}});
    assert.equal(view.disabled, true); assert.equal(view.why, refusal); assert.equal(view.title, refusal);
    assert.equal(view.label, 'Render selfies');
  }
  assert.equal(renderModel({...idle, selfies: false, backlog: null,
    refusal: 'Selfies are off ([selfies] enabled in mind.toml).'}).label, 'Render selfies');
});

test('a render waiting to resume offers its cancel, even while the GPU is held', () => {
  for (const refusal of [null, 'An offline raid holds the GPU. Wait for it to end (Raid tab).']) {
    const view = renderModel({...idle, resume: true, refusal});
    assert.equal(view.action, 'stop'); assert.equal(view.disabled, false);
    assert.equal(view.label, 'Render resumes when the GPU is free · Cancel');
    assert.match(view.title, /^Cancel the stopped render/);
  }
  assert.equal(renderModel({...idle, resume: false}).action, 'render');
  assert.equal(renderModel({...running, resume: true}).label, 'Rendering 3 of 11 · Stop');
});

test('a run shows its progress and becomes Stop, never refused', () => {
  const view = renderModel({...running, refusal: 'Nothing is queued to render.'});
  assert.equal(view.label, 'Rendering 3 of 11 · Stop'); assert.equal(view.action, 'stop');
  assert.equal(view.disabled, false); assert.equal(view.running, true);
  assert.equal(renderModel({...running, progress: {done: 11, total: 11}}).label, 'Rendering 11 of 11 · Stop');
  const stopping = renderModel({...running, stopping: true});
  assert.equal(stopping.label, 'Stopping…'); assert.equal(stopping.disabled, true);
});

test('outcomes name what stopped a run and keep counts', () => {
  assert.equal(outcomeWords({outcome: 'done', sets: 9, snaps: 2, failed: 1, held: 0, seconds: 840}),
               'Selfie render done in 14 minutes: 9 sets and 2 photos rendered, 1 failed.');
  assert.equal(outcomeWords({outcome: 'stopped', sets: 1, snaps: 0, failed: 0, held: 0}),
               'Selfie render stopped: 1 set rendered. The rest stays queued.');
  for (const [by, words] of [['login', 'your WoW login'], ['discord', 'your Discord post'],
       ['discord_answer', 'an answer owed to you on Discord'], ['discord_group', "a group DM's answer"],
       ['offline_raid', 'an offline raid'], ['voice', 'the voice service'], ['gpu_pause', 'Free GPU']]) {
    assert.match(outcomeWords({outcome: 'preempted', by, sets: 0}), new RegExp('stopped by ' + words + ': nothing rendered'));
  }
  assert.match(outcomeWords({outcome: 'failed', sets: 0, failed: 2}), /^Selfie render failed: nothing rendered, 2 failed\.$/);
  assert.equal(outcomeWords({outcome: 'done', sets: 3, snaps: 0, total: 5, done: 3}),
               'Selfie render done: 3 sets rendered, 2 not reached and still queued.');
  assert.equal(outcomeWords(null), '');
});

test('his click answers in words', () => {
  assert.equal(renderResult({ok: true, render: running}, 'render'),
               'Rendering 11 now: 3 first sets, 6 sets of four more, 2 situational photos.');
  assert.equal(renderResult({ok: false, message: 'A night is running.'}, 'render'), 'A night is running.');
  assert.match(renderResult({ok: false}, 'render'), /did not confirm/);
  assert.equal(renderResult({ok: true, render: {...idle, last: {outcome: 'stopped', sets: 2}}}, 'render_stop'),
               'Selfie render stopped: 2 sets rendered. The rest stays queued.');
  assert.equal(renderResult({ok: true, pending: true, message: 'Stopping: the photo worker is still shutting down.'}, 'render_stop'),
               'Stopping: the photo worker is still shutting down.');
});

test('words for kinds and durations', () => {
  assert.equal(kindsWords({first: 1, more: 0, snaps: 1}), '1 first set, 1 situational photo');
  assert.equal(durationWords(30), '1 minute'); assert.equal(durationWords(7200), '2 hours');
  assert.equal(durationWords(NaN), '');
});

test('Ops shows the last run beside the booth', () => {
  const words = lines('selfies', {status: 'ok', manual: {outcome: 'preempted', by: 'discord', sets: 2,
    snaps: 0, failed: 0, held: 0, done: 2, total: 5, finished: 1794567660}}).join(' ');
  assert.match(words, /Render selfies .*stopped by your Discord post: 2 sets rendered.*\(2 of 5\)/);
  assert.doesNotMatch(lines('selfies', {status: 'ok'}).join(' '), /Render selfies/);
});
