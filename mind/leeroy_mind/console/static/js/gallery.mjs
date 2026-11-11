// A native modal keeps the background inert. Every image is fetched with the console key;
// only the shown full image and its neighbours are retained. Closing revokes every blob URL.
import { api, clear, el, emit, on, post, refused, str } from "./core.mjs";
import { face } from "./faces.mjs";
import { coverWhy, initial, ordered, step, swipe } from "./gallerymodel.mjs";

var active = null;

export function photoButton(guid, name, kind) {
  var button = el("button", "facebtn gallery-opener");
  button.type = "button";
  button.setAttribute("aria-label", "Browse photos of " + name);
  button.dataset.gallery = String(guid);
  button.appendChild(face(guid, name, kind));
  button.addEventListener("click", function (ev) {
    ev.stopPropagation();
    openGallery(guid, name, button);
  });
  button.addEventListener("keydown", function (ev) { ev.stopPropagation(); });
  return button;
}

function button(text, action) {
  var b = el("button", "", text);
  b.type = "button";
  b.addEventListener("click", action);
  return b;
}

export function openGallery(guid, name, opener) {
  if (active) { active.dialog.close(); }
  var g = { guid: guid, name: name, opener: opener, photos: [], at: 0, state: null,
            saving: false, loading: false, again: false, seq: 0, full: new Map(), thumbs: new Map(), closed: false };
  active = g;
  var d = g.dialog = el("dialog", "photo-gallery");
  d.setAttribute("aria-labelledby", "gallery-title");
  var header = el("header", "gallery-head"), title = el("h2", "", name);
  title.id = "gallery-title";
  header.appendChild(title);
  g.close = button("Close", function () { d.close(); });
  header.appendChild(g.close);
  d.appendChild(header);
  g.stage = el("div", "gallery-stage");
  g.image = el("img", "gallery-image");
  g.image.alt = name;
  g.image.hidden = true;
  g.stage.appendChild(g.image);
  d.appendChild(g.stage);
  var nav = el("div", "gallery-nav");
  g.prev = button("Previous", function () { move(g, -1); });
  g.next = button("Next", function () { move(g, 1); });
  g.caption = el("span", "");
  g.caption.setAttribute("aria-live", "polite");
  nav.append(g.prev, g.caption, g.next);
  d.appendChild(nav);
  var actions = el("div", "gallery-actions");
  g.badge = el("span", "chip", "Cover");
  g.make = button("Make cover", function () { makeCover(g); });
  g.why = el("span", "muted");
  g.why.id = "gallery-why";
  g.make.setAttribute("aria-describedby", "gallery-why");
  actions.append(g.badge, g.make, g.why);
  d.appendChild(actions);
  g.status = el("p", "why", "Reading photos.");
  g.status.setAttribute("role", "status");
  d.appendChild(g.status);
  g.strip = el("div", "gallery-strip");
  g.strip.setAttribute("aria-label", "Photos, newest first");
  g.strip.setAttribute("role", "group");
  d.appendChild(g.strip);
  d.addEventListener("keydown", function (ev) {
    if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
      ev.preventDefault(); move(g, ev.key === "ArrowRight" ? 1 : -1);
    }
    if (ev.key === "Tab") {
      var bs = Array.from(d.querySelectorAll("button:not(:disabled)")), first = bs[0], last = bs[bs.length - 1];
      if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
      else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
    }
  });
  var start = null;
  g.stage.addEventListener("touchstart", function (ev) {
    start = ev.touches.length === 1 ? [ev.touches[0].clientX, ev.touches[0].clientY] : null;
  }, { passive: true });
  g.stage.addEventListener("touchend", function (ev) {
    if (start && ev.changedTouches.length === 1) {
      var t = ev.changedTouches[0], delta = swipe(t.clientX - start[0], t.clientY - start[1]);
      if (delta) { move(g, delta); }
    }
    start = null;
  }, { passive: true });
  g.stage.addEventListener("touchcancel", function () { start = null; });
  d.addEventListener("close", function () {
    g.closed = true;
    clearImages(g.full); clearImages(g.thumbs);
    d.remove();
    if (active === g) { active = null; }
    var back = g.opener.isConnected ? g.opener : document.querySelector('[data-gallery="' + g.guid + '"]');
    if (back) { back.focus(); }
  });
  document.body.appendChild(d);
  d.showModal();
  g.close.focus();
  draw(g);
  refresh(g, true);
}

function clearImages(cache) {
  cache.forEach(function (entry) { entry.live = false; if (entry.url) { URL.revokeObjectURL(entry.url); } });
  cache.clear();
}

function imageURL(g, cache, path) {
  if (!path) { return Promise.resolve(null); }
  if (cache.has(path)) { return cache.get(path).promise; }
  while (cache.size >= 64) {
    var oldest = cache.keys().next().value, old = cache.get(oldest);
    old.live = false;
    if (old.url) { URL.revokeObjectURL(old.url); }
    cache.delete(oldest);
  }
  var entry = { live: true, url: null };
  entry.promise = api(path).then(function (r) {
    if (refused(r) || r.status !== 200) { return null; }
    return r.blob();
  }).then(function (blob) {
    if (!blob || g.closed || !entry.live) { return null; }
    entry.url = URL.createObjectURL(blob);
    return entry.url;
  }).catch(function () { return null; });
  cache.set(path, entry);
  return entry.promise;
}

function refresh(g, first) {
  if (g.closed) { return; }
  if (g.loading) { g.again = true; return; }
  g.loading = true;
  var seq = ++g.seq;
  api("/api/selfies/gallery?guid=" + g.guid).then(function (r) {
    if (refused(r)) { return null; }
    return r.json().then(function (j) {
      if (r.status !== 200) { throw new Error(str(j.error) || "Photos could not be read. Close and try again."); }
      return j;
    });
  }).then(function (state) {
    if (!state || g.closed || g.seq !== seq) { return; }
    var id = g.photos[g.at] && g.photos[g.at].id;
    g.state = state;
    var photos = ordered(state.view && state.view.photos);
    var changed = JSON.stringify(photos) !== JSON.stringify(g.photos);
    if (changed) { g.photos = photos; }
    g.at = first ? initial(g.photos) : Math.max(0, g.photos.findIndex(function (p) { return p.id === id; }));
    g.status.textContent = g.photos.length ? "" : "No photos yet.";
    if (changed) { drawStrip(g); }
    draw(g);
  }).catch(function (error) {
    if (!g.closed && g.seq === seq) { g.status.textContent = str(error.message) || "Photos could not be read. Close and try again."; }
  }).finally(function () {
    g.loading = false;
    if (g.again && !g.closed) { g.again = false; refresh(g, false); }
  });
}

function drawStrip(g) {
  var focused = document.activeElement && g.strip.contains(document.activeElement);
  g.stripStart = Math.floor(g.at / 32) * 32;
  var window = g.photos.slice(g.stripStart, g.stripStart + 32);
  var wanted = new Set(window.map(function (p) { return p.thumbnail; }));
  g.thumbs.forEach(function (entry, path) {
    if (!wanted.has(path)) { entry.live = false; if (entry.url) { URL.revokeObjectURL(entry.url); } g.thumbs.delete(path); }
  });
  clear(g.strip);
  window.forEach(function (p, offset) {
    var i = g.stripStart + offset;
    var b = button(String(i + 1), function () { g.at = i; draw(g); });
    b.dataset.photoIndex = String(i);
    b.setAttribute("aria-label", "Photo " + (i + 1));
    g.strip.appendChild(b);
    imageURL(g, g.thumbs, p.thumbnail).then(function (url) {
      if (!url || g.closed) { return; }
      var img = el("img"); img.alt = g.name; img.src = url;
      clear(b); b.appendChild(img);
    });
  });
  var back = g.strip.children[g.at - g.stripStart];
  if (focused && back) { back.focus(); }
}

function move(g, delta) { g.at = step(g.at, delta, g.photos.length); draw(g); }
function showing(g, p) { return !g.closed && g.photos[g.at] && g.photos[g.at].id === p.id; }

function draw(g) {
  var p = g.photos[g.at], why = coverWhy(p, g.state, g.saving);
  if (g.stripStart !== Math.floor(g.at / 32) * 32) { drawStrip(g); }
  g.prev.disabled = !p || g.at === 0;
  g.next.disabled = !p || g.at === g.photos.length - 1;
  g.make.disabled = !!why;
  g.why.textContent = why;
  g.badge.hidden = !p || !p.cover;
  g.caption.textContent = p ? (g.at + 1) + " / " + g.photos.length + " · " + new Date(p.rendered * 1000).toLocaleString() : "";
  Array.from(g.strip.children).forEach(function (b) { b.setAttribute("aria-current", String(Number(b.dataset.photoIndex) === g.at)); });
  if (g.image.dataset.photo === (p && p.id)) { return; }
  var selected = g.strip.children[g.at - g.stripStart];
  if (selected) { selected.scrollIntoView({ block: "nearest", inline: "nearest" }); }
  g.image.dataset.photo = p ? p.id : "";
  g.image.hidden = true;
  g.image.removeAttribute("src");
  if (!p) { return; }
  var neighbours = g.photos.slice(Math.max(0, g.at - 1), g.at + 2), wanted = new Set(neighbours.map(function (x) { return x.image; }));
  g.full.forEach(function (entry, path) {
    if (!wanted.has(path)) { entry.live = false; if (entry.url) { URL.revokeObjectURL(entry.url); } g.full.delete(path); }
  });
  imageURL(g, g.thumbs, p.thumbnail).then(function (url) {
    if (url && showing(g, p) && !g.image.getAttribute("src")) { g.image.src = url; g.image.hidden = false; }
    if (!showing(g, p)) { return; }
    neighbours.forEach(function (n) {
      imageURL(g, g.full, n.image).then(function (url) {
        if (n !== p || !showing(g, p)) { return; }
        if (url) { g.image.src = url; g.image.hidden = false; }
        else { g.status.textContent = "This photo could not be read."; }
      });
    });
  });
}

function makeCover(g) {
  var p = g.photos[g.at];
  if (coverWhy(p, g.state, g.saving)) { return; }
  g.saving = true; draw(g);
  post("/api/selfie", { action: "pick", guid: g.guid, set: p.set, index: p.index }).then(function (r) {
    if (r.ok !== true) { g.status.textContent = "Not done: " + (str(r.error) || "refused"); return; }
    g.status.textContent = str(r.message) || "Cover set.";
    if (!g.closed) { g.close.focus(); }
    emit("selfie", { action: "pick", guid: g.guid });
  }).catch(function () { g.status.textContent = "The console could not be reached."; }).then(function () {
    g.saving = false;
    if (!g.closed) { draw(g); refresh(g, false); }
  });
}

on("selfie", function (d) { if (active && d && d.guid === active.guid) { refresh(active, false); } });
on("status", function () { if (active) { refresh(active, false); } });
on("control", function () { if (active) { refresh(active, false); } });
on("key-lost", function () { if (active) { active.dialog.close(); } });
