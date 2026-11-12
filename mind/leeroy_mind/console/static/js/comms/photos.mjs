export const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
export const PICK_ACCEPT = 'image/*';
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
export function aspectClass(w,h) {
  return w/h < .85 ? 'dc-photo-portrait' : w/h > 1.2 ? 'dc-photo-landscape' : 'dc-photo-square';
}
