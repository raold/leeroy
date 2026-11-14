import { test } from 'node:test';
import assert from 'node:assert/strict';
import { controlReason, decisionRow, pollModel, sectionHidden } from '../../../leeroy_mind/console/static/js/decisionsmodel.mjs';
import { validView } from '../../../leeroy_mind/console/static/js/resilience.mjs';

const running = {control: {enabled: true}, mind: {state: 'running'}};

test('votes and overrules need control and a running mind', () => {
  assert.equal(controlReason(running), '');
  assert.match(controlReason({control: {enabled: false}, mind: {state: 'running'}}), /read-only/);
  assert.match(controlReason({control: {enabled: true}, mind: {state: 'stopped'}}), /running/);
  assert.ok(controlReason(undefined));
});

test('a ledger row shows what changed and offers only the action the mind offered', () => {
  const row = decisionRow({id: 4, kind: 'loot_rule', label: 'Loot rule', summary: 'mode loot_council', status: 'in_effect',
    state: 'in effect', settler: 'a poll', settled: 1, effect: 2, changed: ['DKP ledger rows: 12, 13', 'receipts: guild:loot:1'],
    positions: [{guid: 102, name: 'Belra', side: 'loot_council', why: 'low_attendance'}],
    action: {kind: 'overrule', label: 'Overrule future effect', words: "Can't be undone in game: overruling stops its future effect only."}});
  assert.equal(row.changed, 'DKP ledger rows: 12, 13; receipts: guild:loot:1');
  assert.equal(row.positions, 'Belra: loot council (low attendance)');
  assert.deepEqual(row.action, {kind: 'overrule', label: 'Overrule future effect',
    words: "Can't be undone in game: overruling stops its future effect only."});
  assert.equal(decisionRow({id: 5, kind: 'rank', status: 'expired', action: null}).action, null);
  assert.equal(decisionRow({id: 6, kind: 'rank', action: {kind: 'delete everything'}}).action, null);
  assert.equal(decisionRow({id: 7, changed: []}).changed, '-');
});

test('a poll card counts each option and keeps his vote final', () => {
  const poll = {id: 3, question: 'Loot rules?', status: 'open', open: true, voters: 3, he_votes: true, his_vote: null,
    options: [{code: 'dkp', count: 1}, {code: 'loot_council', count: 3}]};
  const open = pollModel(poll, '');
  assert.equal(open.canVote, true);
  assert.deepEqual(open.options.map(o => [o.label, o.count, o.share]), [['dkp', 1, 25], ['loot council', 3, 75]]);
  const voted = pollModel({...poll, his_vote: 'dkp'}, '');
  assert.equal(voted.canVote, false);
  assert.match(voted.why, /never changes/);
  assert.equal(voted.options[0].mine, true);
  assert.equal(pollModel(poll, 'Decisions are read-only in this console.').canVote, false);
  const closed = pollModel({...poll, status: 'closed', open: false, result: 'loot_council'}, '');
  assert.equal(closed.canVote, false);
  assert.deepEqual(closed.options.map(o => o.winner), [false, true]);
  assert.equal(pollModel({...poll, he_votes: false}, '').canVote, false);
});

test('the section stays hidden while decisions are off and nothing was recorded', () => {
  assert.equal(sectionHidden({enabled: false, decisions: [], polls: []}), true);
  assert.equal(sectionHidden({enabled: true, decisions: [], polls: []}), false);
  assert.equal(sectionHidden({enabled: false, decisions: [{id: 1}], polls: []}), false);
});

test('a malformed decisions answer does not replace a good view', () => {
  assert.equal(validView('/api/decisions', {decisions: [], polls: [], missing: []}), true);
  assert.equal(validView('/api/decisions', {decisions: [], polls: 'x', missing: []}), false);
  assert.equal(validView('/api/decisions', {decisions: [1], polls: [], missing: []}), false);
});
