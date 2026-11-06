// A member's face: the chosen photo, or an initials circle while there is none. The photo is
// fetched with the console key and shown from a blob URL, since an <img src> could not carry the
// key. Each photo is decoded once and kept per guid and size: a face drawn again (the feed redraws
// on every line) is a copy of that decoded image at once, never the circle first, and a member
// with no photo is asked for again only when the cache's memory of the miss runs out. Only a 404
// says there is no photo: a server error, a refused key or no connection keeps the one shown.
//
// In Comms a member's face is a button (faceButton) that opens their card: data-card and
// data-name say whose, data-speaker lets lightSpeakers light it while their voice plays.
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

// A photo is in the cache only once it decoded: one that does not is a miss like a 404
function loadFace(key) {
  var i = key.indexOf(":");
  return api("/api/face?guid=" + encodeURIComponent(key.slice(0, i)) + "&size=" + key.slice(i + 1))
    .then(function (r) {
      if (r.status === 404) { return null; }
      if (r.status !== 200) { throw new Error("HTTP " + r.status); }
      return r.blob();
    })
    .then(function (b) {
      if (!b) { return null; }
      var url = URL.createObjectURL(b), img = document.createElement("img");
      img.alt = "";
      img.src = url;
      return img.decode().then(function () { return { url: url, img: img }; },
                               function (e) { URL.revokeObjectURL(url); throw e; });
    });
}

var cache = new FaceCache({ load: loadFace, drop: function (p) { URL.revokeObjectURL(p.url); } });

export function forgetFaces() { cache.forget(); }

function photo(p, cls) {
  var img = p.img.cloneNode(false);
  img.className = cls;
  img.alt = "";
  return img;
}

export function face(guid, name, kind, opts) {
  opts = opts || {};
  var k = KINDS[kind || "feed"] || KINDS.feed;
  var cls = k.cls + (opts.speaking ? " speaking" : "");
  var box = el("span", cls, opts.you ? "YOU" : initials(name));
  box.style.setProperty("--c", opts.you ? "var(--accent)" : PALETTE[paletteSlot(guid)]);
  if (opts.you) { return box; }
  var key = guid + ":" + k.size, known = cache.peek(key), asked = cache.get(key);
  if (known) { return photo(known, cls); }  // and a reload, when one is due, serves the next draw
  asked.then(function (p) {
    if (p && box.parentNode) { box.parentNode.replaceChild(photo(p, cls), box); }
  });
  return box;
}

// A member's face that opens their card (rightview.mjs listens for data-card in Comms). inButton:
// a span, for a face inside another button (a whisper thread's). His own face is a plain face.
export function faceButton(guid, name, kind, opts) {
  opts = opts || {};
  var f = face(guid, name, kind, { you: opts.you });
  if (opts.you) { return f; }
  var b = el(opts.inButton ? "span" : "button", "facebtn" + (opts.speaking ? " speaking" : ""));
  if (!opts.inButton) {
    b.type = "button";
    b.title = name;
    b.setAttribute("aria-label", name);
  }
  b.dataset.card = String(guid);
  b.dataset.name = name;
  b.dataset.speaker = String(guid);
  b.appendChild(f);
  return b;
}

// Light (or put out) one member's faces under box without drawing anything again
export function lightSpeakers(box, guid, on) {
  var lit = box.querySelectorAll('[data-speaker="' + Number(guid) + '"]');
  for (var i = 0; i < lit.length; i++) { lit[i].classList.toggle("speaking", !!on); }
}
