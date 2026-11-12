import { $, api, el, on, refused } from "../core.mjs";
import { FaceCache } from "../facecache.mjs";
import { fitSize, imageUrl, MAX_IMAGE_BYTES, PICK_ACCEPT, aspectClass } from "./photos.mjs";

const drafts = new Map();
const cache = new FaceCache({ maxEntries:32, maxActive:2, ttlMs:3600000,
  load:async key => {
    const [id,size] = key.split(':');
    const response = await api(imageUrl(Number(id), size));
    if (refused(response)) { return null; }
    if (response.status === 404) { return null; }
    if (!response.ok) { throw new Error('The photo could not be loaded.'); }
    return URL.createObjectURL(await response.blob());
  }, drop:url => URL.revokeObjectURL(url) });

export function photoDraft(key) { return drafts.get(key) || null; }
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
export async function preparePhoto(draft) {
  if (!draft) { return null; }
  if (draft.token) { return draft.token; }
  const response = await api('/api/discord/upload', { method:'POST', body:draft.blob,
    headers:{ 'Content-Type':'image/jpeg' }, timeoutMs:65000 });
  if (refused(response)) { throw new Error('Open the console with its key before uploading.'); }
  const answer = await response.json();
  if (!response.ok || !/^[a-f0-9]{32}$/.test(answer.image || '')) {
    throw new Error(answer.error || 'The photo could not be uploaded. Your draft is still here.');
  }
  draft.token = answer.image;
  return draft.token;
}
export function renderPhotoDraft(key, enabled, busy) {
  $('dc-photo-pick').hidden = !enabled;
  $('dc-photo-pick').disabled = busy;
  const draft = enabled && photoDraft(key);
  $('dc-photo-preview').hidden = !draft;
  if (draft) { $('dc-photo-preview-img').src = draft.url; }
  else { $('dc-photo-preview-img').removeAttribute('src'); }
  $('dc-photo-remove').disabled = busy;
}
export function setupPhotoComposer({ key, enabled, changed, failed }) {
  const input = $('dc-photo-file'); input.accept = PICK_ACCEPT;
  let version = 0;
  async function pick(file) {
    if (!file || !enabled()) { return; }
    const destination = key(), mine = ++version;
    try {
      const blob = await reencode(file);
      if (mine !== version || !enabled()) { return; }
      clearPhoto(destination);
      while (drafts.size >= 60) { clearPhoto(drafts.keys().next().value); }
      drafts.set(destination, { blob, url:URL.createObjectURL(blob), token:null }); changed();
    } catch (error) { failed(new Error(error.message || 'That image could not be opened.')); }
  }
  $('dc-photo-pick').addEventListener('click', () => input.click());
  input.addEventListener('change', () => { pick(input.files[0]); input.value = ''; });
  $('dc-photo-remove').addEventListener('click', () => { version++; clearPhoto(key()); changed(); });
  $('dc-text').addEventListener('paste', event => {
    const item = [...(event.clipboardData?.items || [])].find(i => i.type.startsWith('image/'));
    if (item && enabled()) { event.preventDefault(); pick(item.getAsFile()); }
  });
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
$('dc-photo-view-close').addEventListener('click', () => $('dc-photo-view').close());
$('dc-photo-view').addEventListener('close', () => {
  viewing++; $('dc-photo-view-img').removeAttribute('src');
  if (returnTo?.isConnected) { returnTo.focus(); } returnTo = null;
});
on('key-lost', () => { cache.forget(); for (const key of drafts.keys()) { clearPhoto(key); } $('dc-photo-view').close(); });
