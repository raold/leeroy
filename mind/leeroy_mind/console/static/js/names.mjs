// Pure helpers for a member's initials circle. No DOM, so node can load this file.

export function initials(name) {
  var letters = String(name || "").replace(/[^\p{L}\p{N}]/gu, "");
  return letters ? Array.from(letters).slice(0, 2).join("").toUpperCase() : "?";
}
export function paletteSlot(guid) { return Math.abs(Math.floor(Number(guid) || 0)) % 8; }
