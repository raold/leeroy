// A member's face: the chosen photo, or an initials circle while there is none. The photo is
// fetched with the console key and shown from a blob URL, since an <img src> could not carry the
// key. Each photo is decoded once and kept per guid and size: a face drawn again (the feed redraws
// on every line) is a copy of that decoded image at once, never the circle first, and a member
// with no photo is asked for again only when the cache's memory of the miss runs out. Only a 404
// says there is no photo: a server error, a refused key or no connection keeps the one shown. A
// guid that names nobody (0 on a line with no member, below 0, not a whole number) is never asked
// for: it is the initials.
//
// In Comms a member's face is a button (faceButton) that opens their card: data-card and
// data-name say whose, data-speaker lets lightSpeakers light it while their voice plays.
//
// A photo picked or undone (a "selfie" event, from this page or another) is that member's photo
// no longer: every size of it is asked for again at its next draw.
import { api, el, on } from "./core.mjs";
import { FaceCache } from "./facecache.mjs";
import { deadline } from "./resilience.mjs";
import { initials, paletteSlot } from "./names.mjs";

var KINDS = {
  list: { cls: "av sm", size: "sm" },
  feed: { cls: "av", size: "sm" },
  room: { cls: "av lg", size: "md" },
  card: { cls: "av xl", size: "lg" },
  wall: { cls: "av wall", size: "md" },
};

var PALETTE = ["#2a68a8", "#6956b0", "#1f7a52", "#925708", "#a0446e", "#0d6470", "#7a5c2e", "#4e5f9a"];

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
      return deadline(img.decode(), 10000).then(function () { return { url: url, img: img }; },
                               function (e) { URL.revokeObjectURL(url); throw e; });
    });
}

var cache = new FaceCache({ load: loadFace, drop: function (p) { URL.revokeObjectURL(p.url); } });

export function forgetFaces() { cache.forget(); }

on("selfie", function (d) {
  if (!d || d.action === "more" || !(typeof d.guid === "number" && Number.isInteger(d.guid) && d.guid > 0)) { return; }
  Object.keys(KINDS).forEach(function (k) { cache.forgetKey(d.guid + ":" + KINDS[k].size); });
  // Refresh faces already on screen too; some tabs otherwise wait for their next poll.
  document.querySelectorAll('[data-face-guid="' + d.guid + '"]').forEach(function (node) {
    var spec = node.faceSpec;
    if (spec && node.parentNode) { node.parentNode.replaceChild(face(spec.guid, spec.name, spec.kind, spec.opts), node); }
  });
});

function photo(p, cls) {
  var img = p.img.cloneNode(false);
  img.className = cls;
  img.alt = "";
  return img;
}

export function face(guid, name, kind, opts) {
  opts = opts || {};
  var k = KINDS[kind || "feed"] || KINDS.feed;
  var cls = k.cls + (opts.speaking ? " speaking" : "") + (opts.you ? " you" : "");
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
  var key = g + ":" + k.size, known = cache.peek(key), asked = cache.get(key);
  if (known) { return tracked(photo(known, cls)); }  // a reload serves the next draw
  asked.then(function (p) {
    if (p && box.parentNode) { box.parentNode.replaceChild(tracked(photo(p, cls)), box); }
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
