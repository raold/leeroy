// Retained photos are chronological. The cover chooses the initial position, not the order.
import { arr, num, str } from "./core.mjs";
import { whyOff } from "./selfiemodel.mjs";

export var SWIPE_PX = 50; // intentional horizontal movement, not a tap or vertical scroll
export function ordered(photos) {
  return arr(photos).slice().sort(function (a, b) {
    return num(b.rendered) - num(a.rendered) || str(a.id).localeCompare(str(b.id));
  });
}
export function initial(photos) { return Math.max(0, photos.findIndex(function (p) { return p.cover; })); }
export function step(at, delta, count) { return Math.max(0, Math.min(count - 1, at + delta)); }
export function swipe(dx, dy) { return Math.abs(dx) >= SWIPE_PX && Math.abs(dx) > Math.abs(dy) * 1.5 ? (dx < 0 ? 1 : -1) : 0; }
export function coverWhy(photo, state, saving) {
  if (saving) { return "Saving the cover."; }
  if (!photo) { return "No photos yet."; }
  if (photo.kind !== "candidate") { return "Situational photos can be viewed, but cannot be a cover."; }
  if (photo.cover) { return "This is the current cover."; }
  return whyOff(state);
}
