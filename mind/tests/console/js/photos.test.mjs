import test from 'node:test';
import assert from 'node:assert/strict';
import { fitSize, imageUrl, MAX_IMAGE_BYTES, PICK_ACCEPT, aspectClass } from '../../../leeroy_mind/console/static/js/comms/photos.mjs';

test('photos fit within 1600 without stretching or upscaling', () => {
  assert.deepEqual(fitSize(4000, 3000), { w:1600, h:1200 });
  assert.deepEqual(fitSize(3000, 4000), { w:1200, h:1600 });
  assert.deepEqual(fitSize(32, 24), { w:32, h:24 });
  assert.throws(() => fitSize(8192, 16));
  assert.throws(() => fitSize(0, 10));
});
test('photo URLs admit message IDs and known sizes only', () => {
  assert.equal(imageUrl(41, 'sm'), '/api/discord/image?id=41&size=sm');
  assert.throws(() => imageUrl('../private', 'sm'));
  assert.throws(() => imageUrl(41, 'evil'));
  assert.equal(MAX_IMAGE_BYTES, 4194304);
  assert.equal(PICK_ACCEPT, 'image/*');
});
test('aspect classes reserve portrait, square and landscape boxes', () => {
  assert.equal(aspectClass(100,200), 'dc-photo-portrait');
  assert.equal(aspectClass(100,100), 'dc-photo-square');
  assert.equal(aspectClass(200,100), 'dc-photo-landscape');
});
