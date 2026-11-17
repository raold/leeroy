// The feed row's height in a small window: what the window has left under the Comms grid's top
// (measured with the page scrolled to its start), less the grid's own padding and a small gap,
// and never so short that the feed has no lines.
export var FEED_MIN = 240, GAP = 8;
export function feedHeight(windowHeight, gridTop, padding) {
  var h = Math.floor(windowHeight - gridTop - padding - GAP);
  return Number.isFinite(h) ? Math.max(FEED_MIN, h) : FEED_MIN;
}
