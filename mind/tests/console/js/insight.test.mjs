import test from 'node:test';
import assert from 'node:assert/strict';
import { badges, rate, variant } from '../../../leeroy_mind/console/static/js/insightmodel.mjs';

test('badges contain only counts and explain possible conflicts', () => {
  assert.deepEqual(badges(null), []);
  const result = badges({conflicts:2, repetition:7, text:'PRIVATE'});
  assert.deepEqual(result.map(x => x.text), ['2','7']);
  assert.match(result[0].title, /Potential/);
  assert.ok(!JSON.stringify(result).includes('PRIVATE'));
});
test('rates never turn missing evidence into zero', () => {
  assert.equal(rate(null), 'Unavailable');
  assert.equal(rate({per_100_lines:12.5, lines:8, flagged_lines:1}), '12.5 / 100 (1 of 8 lines)');
});
test('budget switch selects the exact rendered card', () => {
  const v = {variants:{small:{text:'Game:\n- observed', dropped:['Standing']}, medium:{text:'full'}}};
  assert.deepEqual(variant(v, 'small'), {text:'Game:\n- observed', dropped:'Standing'});
  assert.equal(variant(v, 'wrong').text, 'full');
});
