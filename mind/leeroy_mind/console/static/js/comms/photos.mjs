export const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
export const MAX_VIDEO_BYTES = 50 * 1024 * 1024;
export const PICK_ACCEPT = 'image/*';
export const VIDEO_ACCEPT = 'image/*,video/mp4,video/webm,video/quicktime,.mp4,.m4v,.mov,.webm';
// A file's own type, or its name's suffix when the system gave none, to what the console takes
const VIDEO_TYPES = { 'video/mp4':'video/mp4', 'video/x-m4v':'video/mp4', 'video/quicktime':'video/quicktime',
  'video/webm':'video/webm' };
const VIDEO_SUFFIXES = { mp4:'video/mp4', m4v:'video/mp4', mov:'video/quicktime', webm:'video/webm' };
const IMAGE_SUFFIXES = /^(?:jpe?g|png|gif|webp|bmp|avif)$/;
export function fitSize(w, h, max = 1600) {
  if (![w,h,max].every(Number.isFinite) || w <= 0 || h <= 0 || max < 16) { throw new Error('That image has no usable size.'); }
  const scale = Math.min(1, max / Math.max(w,h));
  const size = { w:Math.round(w*scale), h:Math.round(h*scale) };
  if (size.w < 16 || size.h < 16) { throw new Error('That photo is too narrow or too small (16 pixels per side minimum).'); }
  return size;
}
export function imageUrl(id, size) {
  if (!Number.isSafeInteger(id) || id < 1 || !['sm','lg'].includes(size)) { throw new Error('Invalid photo.'); }
  return '/api/discord/image?id=' + id + '&size=' + size;
}
export function videoUrl(id) {
  if (!Number.isSafeInteger(id) || id < 1) { throw new Error('Invalid video.'); }
  return '/api/discord/video?id=' + id;
}
export function aspectClass(w,h) {
  return w/h < .85 ? 'dc-photo-portrait' : w/h > 1.2 ? 'dc-photo-landscape' : 'dc-photo-square';
}
// What a picked, dropped or pasted file is: {kind:'image'}, {kind:'video', mime} or {kind:null, why}.
// `videos`: the mind takes his videos; `videosOff`: the console's words when it is on but cannot.
export function fileKind(file, videos, videosOff) {
  const type = String(file && file.type || '').toLowerCase();
  const suffix = (/\.([a-z0-9]{1,5})$/i.exec(String(file && file.name || '')) || ['', ''])[1].toLowerCase();
  const untyped = !type || type === 'application/octet-stream';
  if (type.startsWith('image/') || (untyped && IMAGE_SUFFIXES.test(suffix))) { return { kind:'image' }; }
  const mime = VIDEO_TYPES[type] || (untyped ? VIDEO_SUFFIXES[suffix] : undefined);
  if (mime) {
    if (!videos) { return { kind:null, why:videosOff || 'Videos are off in this mind: only photos can be attached.' }; }
    if (!(file.size > 0)) { return { kind:null, why:'That video is empty.' }; }
    if (file.size > MAX_VIDEO_BYTES) { return { kind:null, why:'That video is over 50 MiB.' }; }
    return { kind:'video', mime };
  }
  return { kind:null, why:videos ? 'Only photos and mp4, webm or mov videos can be attached.'
                                 : 'Only photos can be attached here.' };
}
// The one file of a drop, paste or pick that attaches (a message carries one), with a note when
// others were left; {file:null, why} when none can.
export function chooseFile(files, videos, videosOff) {
  const list = Array.from(files || []).filter(Boolean);
  if (!list.length) { return { file:null, why:'Nothing to attach.' }; }
  const kinds = list.map(f => fileKind(f, videos, videosOff));
  const i = kinds.findIndex(k => k.kind);
  if (i < 0) { return { file:null, why:kinds[0].why }; }
  const what = kinds[i].kind === 'video' ? 'video' : 'photo';
  const note = list.length < 2 ? '' : 'One photo or video per message: attached the ' +
    (i === 0 ? 'first one.' : 'first ' + what + ' among them.');
  return { file:list[i], kind:kinds[i].kind, mime:kinds[i].mime || '', note };
}
// A drag that carries files from the desktop (text and links carry none)
export function carriesFiles(types) { return Array.from(types || []).includes('Files'); }
export function videoLength(ms) {
  if (!Number.isFinite(ms) || ms <= 0) { return ''; }
  const s = Math.max(1, Math.round(ms / 1000));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}
