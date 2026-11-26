import test from 'node:test';
import assert from 'node:assert/strict';
import { fitSize, imageUrl, MAX_IMAGE_BYTES, MAX_VIDEO_BYTES, PICK_ACCEPT, VIDEO_ACCEPT, aspectClass, carriesFiles, chooseFile, fileKind, videoLength, videoUrl } from '../../../leeroy_mind/console/static/js/comms/photos.mjs';
import { FaceCache } from '../../../leeroy_mind/console/static/js/facecache.mjs';

test('40 photo requests all finish even when queued beyond the cache limit', async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const cache = new FaceCache({ maxEntries:32, maxActive:2,
    load:async key => { await gate; return 'blob:photo-' + key; } });
  const requests = Array.from({ length:40 }, (_, i) => cache.get(i));
  release();
  assert.deepEqual(await Promise.all(requests), Array.from({ length:40 }, (_, i) => 'blob:photo-' + i));
  await cache.get(40);
  assert.equal(cache.entries.size, 32); // settled entries are evicted on the next load
});

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

const file = (name, type, size = 100) => ({ name, type, size });
test('a dropped or pasted file is a photo, one of the three video kinds, or refused in words', () => {
  assert.deepEqual(fileKind(file('cat.png', 'image/png'), false), { kind:'image' });
  assert.deepEqual(fileKind(file('Screenshot', 'image/webp'), true), { kind:'image' });
  assert.deepEqual(fileKind(file('IMG_1.HEIC.jpg', ''), true), { kind:'image' });
  assert.deepEqual(fileKind(file('clip.mp4', 'video/mp4'), true), { kind:'video', mime:'video/mp4' });
  assert.deepEqual(fileKind(file('clip.M4V', 'video/x-m4v'), true), { kind:'video', mime:'video/mp4' });
  assert.deepEqual(fileKind(file('clip.mov', ''), true), { kind:'video', mime:'video/quicktime' });
  assert.deepEqual(fileKind(file('clip.webm', 'application/octet-stream'), true), { kind:'video', mime:'video/webm' });
  // a declared type wins over the name; an avi is no video the console takes
  assert.equal(fileKind(file('clip.mp4', 'application/pdf'), true).kind, null);
  assert.match(fileKind(file('clip.avi', 'video/x-msvideo'), true).why, /mp4, webm or mov/);
  assert.match(fileKind(file('notes.pdf', 'application/pdf'), false).why, /Only photos/);
  assert.match(fileKind(file('clip.mp4', 'video/mp4'), false).why, /Videos are off/);
  assert.equal(fileKind(file('clip.mp4', 'video/mp4'), false, 'Videos need ffmpeg.').why, 'Videos need ffmpeg.');
  assert.match(fileKind(file('big.mp4', 'video/mp4', MAX_VIDEO_BYTES + 1), true).why, /over 50 MiB/);
  assert.equal(fileKind(file('none.mp4', 'video/mp4', 0), true).kind, null);
});
test('one file attaches per message, the first that can, with a note when others were left', () => {
  const one = chooseFile([file('a.png', 'image/png')], true);
  assert.equal(one.kind, 'image'); assert.equal(one.note, '');
  const two = chooseFile([file('a.png', 'image/png'), file('b.mp4', 'video/mp4')], true);
  assert.equal(two.file.name, 'a.png'); assert.match(two.note, /One photo or video per message: attached the first one/);
  const skip = chooseFile([file('a.pdf', 'application/pdf'), file('b.mp4', 'video/mp4')], true);
  assert.equal(skip.file.name, 'b.mp4'); assert.equal(skip.mime, 'video/mp4'); assert.match(skip.note, /first video among them/);
  const none = chooseFile([file('a.pdf', 'application/pdf')], true);
  assert.equal(none.file, null); assert.match(none.why, /Only photos and mp4/);
  assert.equal(chooseFile([], true).file, null);
});
test('only drags carrying files are taken; text and links drop as the browser drops them', () => {
  assert.equal(carriesFiles(['Files']), true);
  assert.equal(carriesFiles(['text/plain', 'text/uri-list']), false);
  assert.equal(carriesFiles(undefined), false);
});
test('video URLs, lengths and the picker', () => {
  assert.equal(videoUrl(9), '/api/discord/video?id=9');
  assert.throws(() => videoUrl('9'));
  assert.equal(videoLength(12400), '0:12'); assert.equal(videoLength(125000), '2:05');
  assert.equal(videoLength(null), ''); assert.equal(videoLength(400), '0:01');
  assert.match(VIDEO_ACCEPT, /video\/quicktime/); assert.match(VIDEO_ACCEPT, /^image\/\*/);
});
