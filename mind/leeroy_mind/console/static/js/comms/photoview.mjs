import { $, api, el, on, refused } from "../core.mjs";
import { FaceCache } from "../facecache.mjs";
import { aspectClass, chooseFile, fitSize, imageUrl, MAX_IMAGE_BYTES, PICK_ACCEPT, VIDEO_ACCEPT, videoLength, videoUrl } from "./photos.mjs";

// His attachment drafts, one per conversation: {kind:'image'|'video', blob, url, token, mime}. A
// photo is resized to a JPEG here; a video goes as it is (the console reads it with ffmpeg).
const drafts = new Map();
const preparing = new Map();
const cache = new FaceCache({ maxEntries:128, maxActive:2, ttlMs:3600000,
  load:async key => {
    const [id,size] = key.split(':');
    const response = await api(imageUrl(Number(id), size));
    if (refused(response)) { return null; }
    if (response.status === 404) { return null; }
    if (!response.ok) { throw new Error('The photo could not be loaded.'); }
    return URL.createObjectURL(await response.blob());
  }, drop:url => URL.revokeObjectURL(url) });

export function photoDraft(key) { return drafts.get(key) || null; }
export function photoPreparing(key) { return preparing.has(key); }
export function clearPhoto(key, expected) {
  const draft = drafts.get(key);
  if (draft && (!expected || expected === draft)) { URL.revokeObjectURL(draft.url); drafts.delete(key); }
}
async function reencode(file) {
  const url = URL.createObjectURL(file), img = new Image();
  try {
    img.src = url; await img.decode();
    const size = fitSize(img.naturalWidth, img.naturalHeight), canvas = document.createElement('canvas');
    canvas.width = size.w; canvas.height = size.h;
    canvas.getContext('2d').drawImage(img, 0, 0, size.w, size.h);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', .85));
    if (!blob || blob.size > MAX_IMAGE_BYTES) { throw new Error('That photo is over 4 MiB after resizing.'); }
    return blob;
  } finally { URL.revokeObjectURL(url); }
}
// The draft's upload token: the photo or video sent to its own upload endpoint once, then kept
export async function preparePhoto(draft) {
  if (!draft) { return null; }
  if (draft.token) { return draft.token; }
  const video = draft.kind === 'video';
  const response = await api(video ? '/api/discord/video' : '/api/discord/upload', { method:'POST', body:draft.blob,
    headers:{ 'Content-Type':video ? draft.mime : 'image/jpeg' }, timeoutMs:video ? 330000 : 65000 });
  if (refused(response)) { throw new Error('Open the console with its key before uploading.'); }
  const answer = await response.json();
  const token = video ? answer.video : answer.image;
  if (!response.ok || !/^[a-f0-9]{32}$/.test(token || '')) {
    throw new Error(answer.error || 'The ' + (video ? 'video' : 'photo') + ' could not be uploaded. Your draft is still here.');
  }
  draft.token = token;
  return draft.token;
}
export function renderPhotoDraft(key, enabled, busy, videos) {
  $('dc-photo-pick').hidden = !enabled;
  $('dc-photo-pick').disabled = busy;
  $('dc-photo-pick').textContent = videos ? 'Attach photo or video' : 'Attach photo';
  $('dc-photo-file').accept = videos ? VIDEO_ACCEPT : PICK_ACCEPT;
  const draft = enabled && photoDraft(key), video = !!draft && draft.kind === 'video';
  const img = $('dc-photo-preview-img'), clip = $('dc-video-preview');
  $('dc-photo-preview').hidden = !draft;
  img.hidden = video;
  if (draft && !video) { img.src = draft.url; } else { img.removeAttribute('src'); }
  clip.hidden = !video;
  if (video) { if (clip.getAttribute('src') !== draft.url) { clip.src = draft.url; } }
  else if (clip.hasAttribute('src')) { clip.pause(); clip.removeAttribute('src'); clip.load(); }
  $('dc-photo-remove').textContent = video ? 'Remove video' : 'Remove photo';
  $('dc-photo-remove').disabled = busy;
}
// One attach path for the button, a drop and a paste. `refusal()`: '' while he may attach, else
// why not; `videos()`: {on, off} (whether the mind takes his videos, the console's words if it
// cannot); `failed(error)` says a refusal, `noted(words)` a remark. Returns {attach(files)}.
export function setupPhotoComposer({ key, refusal, videos, changed, failed, noted }) {
  const input = $('dc-photo-file'); input.accept = PICK_ACCEPT;
  let version = 0;
  async function prepare(file, kind, mime) {
    const destination = key(), mine = ++version;
    if (kind === 'video') {
      clearPhoto(destination); preparing.delete(destination);
      while (drafts.size >= 60) { clearPhoto(drafts.keys().next().value); }
      drafts.set(destination, { kind, blob:file, url:URL.createObjectURL(file), token:null, mime }); changed();
      return;
    }
    preparing.set(destination, mine); changed();
    try {
      const blob = await reencode(file);
      if (mine !== version || preparing.get(destination) !== mine || refusal()) { return; }
      clearPhoto(destination);
      while (drafts.size >= 60) { clearPhoto(drafts.keys().next().value); }
      drafts.set(destination, { kind:'image', blob, url:URL.createObjectURL(blob), token:null }); changed();
    } catch (error) {
      if (preparing.get(destination) === mine) { failed(new Error(error.message || 'That image could not be opened.')); }
    } finally {
      if (preparing.get(destination) === mine) { preparing.delete(destination); changed(); }
    }
  }
  function attach(files) {
    const why = refusal();
    if (why) { failed(new Error(why)); return false; }
    const v = videos(), chosen = chooseFile(files, v.on, v.off);
    if (!chosen.file) { failed(new Error(chosen.why)); return false; }
    if (chosen.note) { noted(chosen.note); }
    prepare(chosen.file, chosen.kind, chosen.mime);
    return true;
  }
  $('dc-photo-pick').addEventListener('click', () => input.click());
  input.addEventListener('change', () => { if (input.files.length) { attach(input.files); } input.value = ''; });
  $('dc-photo-remove').addEventListener('click', () => { version++; clearPhoto(key()); changed(); });
  $('dc-text').addEventListener('paste', event => {
    // A copied photo or file attaches; text pastes as text.
    const files = [...(event.clipboardData?.items || [])].filter(i => i.kind === 'file').map(i => i.getAsFile()).filter(Boolean);
    if (files.length) { event.preventDefault(); attach(files); }
  });
  return { attach };
}
let returnTo = null, viewing = 0;
async function openPhoto(id, poster, trigger) {
  returnTo = trigger; const mine = ++viewing;
  $('dc-photo-view-img').removeAttribute('src');
  $('dc-photo-view-img').alt = 'Photo from ' + poster;
  $('dc-photo-view-status').textContent = 'Loading photo…';
  $('dc-photo-view').showModal(); $('dc-photo-view-close').focus();
  const url = await cache.get(id + ':lg');
  if (mine !== viewing || !$('dc-photo-view').open) { return; }
  $('dc-photo-view-status').textContent = url ? '' : 'This photo is unavailable.';
  if (url) { $('dc-photo-view-img').src = url; }
}
export function photoBox(message, poster) {
  const b = el('button', 'dc-photo ' + aspectClass(message.image.w, message.image.h));
  b.type = 'button'; b.setAttribute('aria-label', 'View photo from ' + poster);
  b.dataset.focus = message.id + ':photo';
  const img = el('img'); img.alt = 'Photo from ' + poster; img.loading = 'lazy'; b.appendChild(img);
  cache.get(message.id + ':sm').then(url => { if (url && b.isConnected) { img.src = url; } });
  b.addEventListener('click', () => openPhoto(message.id, poster, b));
  return b;
}

// His videos in the feed: the middle still as a poster with a Play button, then a <video controls>
// fed from a blob once he asks (a <video src> cannot carry the key). Each box outlives the feed's
// redraws, so a playing video keeps playing; at most four stay loaded.
const players = new Map();
function unload(p) {
  p.request?.abort(); p.request = null;
  if (p.video) { p.video.pause(); p.video.removeAttribute('src'); p.video.load(); p.video.remove(); p.video = null; }
  if (p.url) { URL.revokeObjectURL(p.url); p.url = ''; }
  p.still.hidden = false; p.play.hidden = false; p.play.disabled = false;
}
export function retainVideos(ids) {
  const keep = new Set(ids);
  for (const [id, p] of players) { if (!keep.has(id)) { unload(p); players.delete(id); } }
}
function trimVideos(current) {
  const loaded = [...players.values()].filter(p => p.url && p !== current).sort((a, b) => a.used - b.used);
  while (loaded.length >= 4) { unload(loaded.shift()); }
}
async function loadVideo(p) {
  if (p.request || p.video) { return; }
  const request = new AbortController(); p.request = request;
  p.play.disabled = true; p.status.textContent = 'Loading video…';
  try {
    const response = await api(videoUrl(p.id), { signal:request.signal, timeoutMs:300000 });
    if (refused(response)) { throw new Error('Open the console with its key to play this video.'); }
    if (!response.ok) { throw new Error(response.status === 404 ? 'This video is unavailable.' : 'The video could not be loaded.'); }
    const blob = await response.blob();
    if (request.signal.aborted || players.get(p.id) !== p) { return; }
    trimVideos(p);
    p.url = URL.createObjectURL(blob); p.used = performance.now();
    const video = el('video', 'dc-video-player');
    video.controls = true; video.playsInline = true; video.preload = 'auto';
    video.setAttribute('aria-label', 'Video from ' + p.poster); video.dataset.focus = p.id + ':player';
    video.addEventListener('play', () => { p.used = performance.now(); });
    video.src = p.url; p.video = video;
    p.still.hidden = true; p.play.hidden = true; p.status.textContent = '';
    p.box.insertBefore(video, p.status);
    video.focus({ preventScroll:true });
    video.play().catch(() => {});  // a blocked autoplay leaves the controls to him
  } catch (error) {
    if (error.name !== 'AbortError' && players.get(p.id) === p) { p.status.textContent = error.message || 'The video could not be loaded.'; }
  } finally {
    if (p.request === request) { p.request = null; p.play.disabled = false; }
  }
}
export function videoBox(message, poster) {
  const known = players.get(message.id);
  if (known) { known.poster = poster; return known.box; }
  const v = message.video, length = videoLength(v.ms);
  const box = el('div', 'dc-video');
  // The box takes the video's own shape, at most 360 wide and 480 tall
  box.style.setProperty('--video-ratio', v.w + ' / ' + v.h);
  box.style.setProperty('--video-w', Math.round(Math.min(360, 480 * v.w / v.h)) + 'px');
  const still = el('img', 'dc-video-still'); still.alt = ''; still.loading = 'lazy';  // the button names it
  const play = el('button', 'dc-video-play', 'Play video' + (length ? ' · ' + length : ''));
  play.type = 'button'; play.dataset.focus = message.id + ':video';
  play.setAttribute('aria-label', 'Play video from ' + poster + (length ? ', ' + length : ''));
  const status = el('p', 'dc-video-status'); status.setAttribute('role', 'status');
  box.append(still, play, status);
  const p = { id:message.id, box, still, play, status, poster, video:null, url:'', request:null, used:0 };
  players.set(message.id, p);
  cache.get(message.id + ':sm').then(url => { if (url && players.get(message.id) === p) { still.src = url; } });
  play.addEventListener('click', () => loadVideo(p));
  return box;
}
$('dc-photo-view-close').addEventListener('click', () => $('dc-photo-view').close());
$('dc-photo-view').addEventListener('close', () => {
  viewing++; $('dc-photo-view-img').removeAttribute('src');
  if (returnTo?.isConnected) { returnTo.focus(); } returnTo = null;
});
on('key-lost', () => { preparing.clear(); cache.forget(); for (const key of drafts.keys()) { clearPhoto(key); } retainVideos([]); $('dc-photo-view').close(); });
