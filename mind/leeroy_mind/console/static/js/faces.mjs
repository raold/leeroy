// A member's face: an initials circle at once, swapped for the chosen photo when it arrives.
// The photo is fetched with the console key and shown from a blob URL, since an <img src> could
// not carry the key.
import { api, el } from "./core.mjs";
import { FaceCache } from "./facecache.mjs";
import { initials, paletteSlot } from "./names.mjs";

var KINDS = {
  list: { cls: "av sm", size: "sm" },
  feed: { cls: "av", size: "sm" },
  room: { cls: "av lg", size: "md" },
  card: { cls: "av xl", size: "lg" },
};

var PALETTE = ["#2a68a8", "#6956b0", "#1f7a52", "#b06a0c", "#a0446e", "#0d6470", "#7a5c2e", "#4e5f9a"];

function loadFace(key) {
  var i = key.indexOf(":");
  return api("/api/face?guid=" + encodeURIComponent(key.slice(0, i)) + "&size=" + key.slice(i + 1))
    .then(function (r) { return r.status === 200 ? r.blob() : null; })
    .then(function (b) { return b ? URL.createObjectURL(b) : null; });
}

var cache = new FaceCache({ load: loadFace, drop: function (url) { URL.revokeObjectURL(url); } });

export function forgetFaces() { cache.forget(); }

export function face(guid, name, kind, opts) {
  opts = opts || {};
  var k = KINDS[kind || "feed"] || KINDS.feed;
  var cls = k.cls + (opts.speaking ? " speaking" : "");
  var box = el("span", cls, opts.you ? "YOU" : initials(name));
  box.style.setProperty("--c", opts.you ? "var(--accent)" : PALETTE[paletteSlot(guid)]);
  if (opts.you) { return box; }
  cache.get(guid + ":" + k.size).then(function (url) {
    if (!url) { return; }
    var img = document.createElement("img");
    img.className = cls;
    img.alt = "";
    // a photo that fails to decode never replaces the circle: only a load does
    img.addEventListener("load", function () { if (box.parentNode) { box.parentNode.replaceChild(img, box); } });
    img.src = url;
  });
  return box;
}
