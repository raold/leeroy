// A member's face: the chosen photo, or an initials circle while there is none. The photo is
// fetched with the console key and shown from a blob URL, since an <img src> could not carry the
// key. One photo is kept per member (the server sends the same original for every size, and the
// page scales it), decoded once: a face drawn again (the feed redraws on every line) is a copy of
// that image at once, never the circle first.
//
// Only a 404 says there is no photo; that member is asked for again when the cache's memory of the
// miss (5 minutes) runs out. A first load with no answer (a server error, no connection, a
// timeout) is asked again within seconds, backing off to every 30 s, and the initials already
// drawn turn into the photo when it comes, with no redraw of their tab. One the key was refused
// for waits for the next key (forgetFaces). A reload with no answer, or a refused key, keeps the
// photo shown. A guid that names nobody (0 on a line with no member, below 0, not a whole number)
// is never asked for: it is the initials.
//
// A photo is checked again ten minutes after it came, at its next draw. The same bytes keep the
// image shown; another photo (or none) is drawn into every face of that member on the page before
// the old blob URL is let go, so no face on screen is left on a revoked URL. A photo still on
// screen is never evicted. An image that fails on screen anyway is drawn again: as the photo the
// cache has now, or as initials while that member is asked for again.
//
// In Comms a member's face is a button (faceButton) that opens their card: data-card and
// data-name say whose, data-speaker lets lightSpeakers light it while their voice plays.
//
// A photo picked or undone (a "selfie" event, from this page or another) is that member's photo
// no longer: it is asked for again at once, and their faces on the page are drawn again.
//
// While the strip's Hide photos is on (html[data-photos="hidden"]) every face is its initials
// and no photo is asked for; redrawFaces puts the faces on screen right when it is flipped.
import { api, el, on } from "./core.mjs";
import { FaceCache } from "./facecache.mjs";
import { deadline } from "./resilience.mjs";
import { initials, paletteSlot } from "./names.mjs";

var KINDS = {
  list: "av sm",
  feed: "av",
  room: "av lg",
  card: "av xl",
  wall: "av wall",
};

var PALETTE = ["#2a68a8", "#6956b0", "#1f7a52", "#925708", "#a0446e", "#0d6470", "#7a5c2e", "#4e5f9a"];

// Every face of a member on the page (photo or initials)
function shownOf(key) { return document.querySelectorAll('[data-face-guid="' + key + '"]'); }

function sameBytes(a, b) {
  if (!a || !b || a.size !== b.size) { return Promise.resolve(false); }
  return Promise.all([a.arrayBuffer(), b.arrayBuffer()]).then(function (x) {
    var p = new Uint8Array(x[0]), q = new Uint8Array(x[1]);
    for (var i = 0; i < p.length; i++) { if (p[i] !== q[i]) { return false; } }
    return true;
  }, function () { return false; });
}

// A photo is in the cache only once it loaded as an image: bytes that are not one are a miss like
// a 404. Its decode is waited for briefly, so a copy draws at once; a slow decode never fails it.
function picture(blob) {
  var url = URL.createObjectURL(blob), img = document.createElement("img");
  img.alt = "";
  var loaded = new Promise(function (resolve) {
    img.addEventListener("load", function () { resolve(true); });
    img.addEventListener("error", function () { resolve(false); });
  });
  img.src = url;
  return deadline(loaded, 10000).then(function (ok) {
    if (!ok) { URL.revokeObjectURL(url); return null; }
    return deadline(Promise.resolve(img.decode ? img.decode() : null), 2000).catch(function () {})
      .then(function () { return { url: url, img: img, blob: blob }; });
  }, function (e) { URL.revokeObjectURL(url); throw e; });
}

function loadFace(key, previous) {
  return api("/api/face?guid=" + encodeURIComponent(key) + "&size=lg")
    .then(function (r) {
      if (r.status === 404) { return null; }
      var no = new Error("HTTP " + r.status);
      no.final = r.status === 403;  // the key refused: asked again under the next key (forgetFaces)
      if (r.status !== 200) { throw no; }
      return r.blob();
    })
    .then(function (b) {
      if (!b) { return null; }
      return sameBytes(previous && previous.blob, b).then(function (same) { return same ? previous : picture(b); });
    });
}

var cache = new FaceCache({
  load: loadFace,
  ttlMs: 600000,
  retryMs: [1000, 2000, 5000, 10000, 30000],
  drop: function (p) { URL.revokeObjectURL(p.url); },
  replaced: function (key) { redraw('[data-face-guid="' + key + '"]'); },
  inUse: function (key, p) {
    var shown = shownOf(key);
    for (var i = 0; i < shown.length; i++) { if (shown[i].src === p.url) { return true; } }
    return false;
  },
});

// Every photo let go and asked for again (a new key): the faces on the page are drawn again
export function forgetFaces() {
  cache.forget();
  if (typeof document !== "undefined" && document.querySelectorAll) { redrawFaces(); }
}

export function photosHidden() {
  var root = typeof document === "undefined" ? null : document.documentElement;
  return !!(root && root.dataset && root.dataset.photos === "hidden");
}

function redraw(selector) {
  document.querySelectorAll(selector).forEach(function (node) {
    var spec = node.faceSpec;
    if (spec && node.parentNode) { node.parentNode.replaceChild(face(spec.guid, spec.name, spec.kind, spec.opts), node); }
  });
}

// Every face on screen drawn again, as photos or initials (Hide photos was flipped)
export function redrawFaces() { redraw("[data-face-guid]"); }

on("selfie", function (d) {
  if (!d || d.action === "more" || !(typeof d.guid === "number" && Number.isInteger(d.guid) && d.guid > 0)) { return; }
  cache.forgetKey(String(d.guid));
  // Refresh faces already on screen too; some tabs otherwise wait for their next poll.
  redraw('[data-face-guid="' + d.guid + '"]');
});

// An image on screen that fails (its data gone): the photo the cache has now, or initials while
// that member is asked for again when the failed image was the cache's own
function broken(ev) {
  var img = ev.target, spec = img.faceSpec;
  if (!spec) { return; }
  var key = String(spec.guid), p = cache.peek(key);
  if (p && p.url === img.src) { cache.forgetKey(key); }
  if (img.parentNode) { img.parentNode.replaceChild(face(spec.guid, spec.name, spec.kind, spec.opts), img); }
}

function photo(p, cls) {
  var img = p.img.cloneNode(false);
  img.className = cls;
  img.alt = "";
  img.addEventListener("error", broken);
  return img;
}

export function face(guid, name, kind, opts) {
  opts = opts || {};
  var cls = (KINDS[kind || "feed"] || KINDS.feed) + (opts.speaking ? " speaking" : "") + (opts.you ? " you" : "");
  var box = el("span", cls, opts.you ? "YOU" : initials(name));
  box.style.setProperty("--c", opts.you ? "var(--accent)" : PALETTE[paletteSlot(guid)]);
  var g = typeof guid === "string" && guid.trim() !== "" ? Number(guid) : guid;
  if (opts.you || !(typeof g === "number" && Number.isInteger(g) && g > 0)) { return box; }
  function tracked(node) {
    node.dataset.faceGuid = String(g);
    node.faceSpec = { guid: g, name: name, kind: kind, opts: opts };
    return node;
  }
  tracked(box);
  if (photosHidden()) { return box; }
  var key = String(g), known = cache.peek(key), asked = cache.get(key);
  if (known) { return tracked(photo(known, cls)); }  // a reload that changes it draws this again
  asked.then(function (p) {
    if (p && box.parentNode && !photosHidden()) { box.parentNode.replaceChild(tracked(photo(p, cls)), box); }
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

// A table cell holding a member's face and name (.who). opts go to face; opts.text is the words
// shown when they are not the bare name (tags, "(you)"); opts.build(guid, name, kind, opts) replaces
// the face (the gallery's opener). Nobody (guid 0 or none) is a plain "-".
export function nameCell(tr, guid, name, opts) {
  opts = opts || {};
  var td = el("td");
  if (!guid) {
    td.textContent = opts.text || name || "-";
  } else {
    var box = el("div", "who");
    box.appendChild((opts.build || face)(guid, name, "list", opts));
    box.appendChild(el("span", "", opts.text || name));
    td.appendChild(box);
  }
  tr.appendChild(td);
  return td;
}

// Light (or put out) one member's faces under box without drawing anything again
export function lightSpeakers(box, guid, on) {
  var lit = box.querySelectorAll('[data-speaker="' + Number(guid) + '"]');
  for (var i = 0; i < lit.length; i++) { lit[i].classList.toggle("speaking", !!on); }
}
