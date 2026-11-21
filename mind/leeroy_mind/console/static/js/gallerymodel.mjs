// Retained photos are chronological. The cover chooses the initial position, not the order.
import { arr, num, plural, str } from "./core.mjs";
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
// The photo critic's defect codes for a flagged candidate ("limbs,hands"), or "" for a clean one.
// Codes only: letters and underscores, at most ten of them.
export function flagCodes(photo) {
  var flag = photo && photo.flag;
  if (typeof flag !== "string") { return ""; }
  return flag.split(",").filter(function (c) { return /^[a-z_]{1,20}$/.test(c); }).slice(0, 10).join(", ");
}
// A varied set's candidate says its look (what she wears, how she was photographed); others nothing
export function lookWords(photo) {
  var look = photo && photo.look;
  return look && typeof look.label === "string" ? look.label.slice(0, 80) : "";
}
// "Show all sets": this browser remembers it under one key ("all" or "current"). Off, the gallery
// lists only photos of her current looks (#314); on, her earlier looks' too, each marked
export var ALL_KEY = "guild-console-gallery-all";
export function wantsAll(stored) { return stored === "all"; }
export function galleryPath(guid, all) { return "/api/selfies/gallery?guid=" + encodeURIComponent(String(guid)) + (all ? "&all=1" : ""); }
export function earlierWords(photo) { return photo && photo.earlier === true ? "earlier looks" : ""; }
// Under the toggle: how many photos of earlier looks it hides or shows
export function earlierHint(view, all) {
  var n = num(view && view.earlier);
  if (!n) { return all ? "No photos of earlier looks." : ""; }
  return all ? "Showing " + plural(n, "photo") + " of earlier looks too."
             : plural(n, "photo") + " of earlier looks hidden.";
}
// Beside Make cover for a photo of earlier looks (the pick takes it as any other)
export function coverNote(photo) {
  return photo && photo.kind === "candidate" && !photo.cover && photo.earlier === true
    ? "Earlier looks. New photos take their face from the cover." : "";
}
