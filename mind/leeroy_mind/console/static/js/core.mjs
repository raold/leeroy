// The page's shared helpers, its requests and its bus. Nothing here runs at import (main.mjs hands
// over the key with setKey), so node can load this file and the modules built on it.

var KEY = "";
var LISTENERS = Object.create(null);

export function $(id) { return document.getElementById(id); }
export function own(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
export function arr(x) { return Array.isArray(x) ? x : []; }
export function obj(x) { return x && typeof x === "object" && !Array.isArray(x) ? x : {}; }
export function str(x) { return typeof x === "string" ? x : ""; }
export function num(x) { return typeof x === "number" && isFinite(x) ? x : 0; }
export function pad(n) { return (n < 10 ? "0" : "") + n; }
export function plural(n, one, many) { return n + " " + (n === 1 ? one : (many || one + "s")); }
// Everything shown is set as text: nothing here builds markup from what anyone said
export function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) { e.className = cls; }
  if (text !== undefined) { e.textContent = text; }
  return e;
}
export function clear(e) { while (e.firstChild) { e.removeChild(e.firstChild); } }
export function setText(id, text) { $(id).textContent = text; }
export function cell(tr, text, cls) { tr.appendChild(el("td", cls || "", text)); }
export function emptyRow(body, cols, text) {
  var tr = el("tr"), td = el("td", "muted", text);
  td.colSpan = cols;
  tr.appendChild(td);
  body.appendChild(tr);
}
export function clock(ms) {
  if (!ms) { return ""; }
  var d = new Date(ms);
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}
export function hhmm(sec) {
  if (!sec) { return ""; }
  var d = new Date(sec * 1000);
  return pad(d.getHours()) + ":" + pad(d.getMinutes());
}

// The key comes in the URL's fragment, which the browser never sends anywhere: it is kept in
// this page's memory only (not a cookie, which every 127.0.0.1 port would be handed) and goes
// with each request as a header. A reload loses it: open the printed URL again.
export function setKey(key) { KEY = key; }
export function api(path, opts) {
  opts = opts || {};
  var headers = { "X-Console-Key": KEY };
  Object.keys(opts.headers || {}).forEach(function (k) { headers[k] = opts.headers[k]; });
  return fetch(path, { method: opts.method || "GET", headers: headers, body: opts.body,
                       signal: opts.signal, cache: "no-store", credentials: "omit" });
}

export function post(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" },
                     body: JSON.stringify(body) })
    .then(function (r) {
      return r.json().catch(function () { return { ok: false, error: "HTTP " + r.status }; })
        .then(function (j) { j.status = r.status; return j; });
    });
}

// This browser's own conveniences (JSON in localStorage): the page is right without them, and
// any access may throw (storage off, full or private)
export function recall(key, fallback) {
  try {
    var raw = localStorage.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch (e) { return fallback; }
}
export function remember(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* none */ }
}

// The bus: "snapshot", "status", "voice", "chat", "chat_gone", "control", "raid" and "tuning" carry
// what the console's event stream sent; "key-lost" says the console refused the key; "note" is
// one of the console's own notes for the toasts; "feed" says the Comms feed, its read marks or its
// open view changed. Listeners run in the order they were added.
export function on(kind, fn) { (LISTENERS[kind] = LISTENERS[kind] || []).push(fn); }
export function emit(kind, data) {
  (LISTENERS[kind] || []).slice().forEach(function (fn) { fn(data); });
}
