// The page's shared helpers, its requests and its bus. Nothing here runs at import (main.mjs hands
// over the key with setKey), so node can load this file and the modules built on it.
import { boundedBody, deadline, validView } from "./resilience.mjs";

var KEY = "";
var GEN = 0;               // which key is in use: one more for each key set
var SENT = new WeakMap();  // an answer -> the generation of the key its request carried
var LISTENERS = Object.create(null);
export var KEY_REFUSED = "The console refused this page's key: open the URL it printed when it started.";

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
export function setKey(key) { KEY = key; GEN++; }
export function keyGen() { return GEN; }
// The key in a "#key=..." fragment: the key, "" for a malformed one, null for any other fragment
export function hashKey(hash) {
  hash = str(hash);
  if (hash.indexOf("#key=") !== 0) { return null; }
  var m = /^#key=([A-Za-z0-9_-]{16,128})$/.exec(hash);
  return m ? m[1] : "";
}
export function api(path, opts) {
  opts = opts || {};
  var headers = { "X-Console-Key": KEY }, gen = GEN, method = opts.method || "GET";
  var ctl = new AbortController(), outer = opts.signal;
  function abort() { ctl.abort(); }
  if (outer) { if (outer.aborted) { abort(); } else { outer.addEventListener("abort", abort, { once: true }); } }
  Object.keys(opts.headers || {}).forEach(function (k) { headers[k] = opts.headers[k]; });
  var work = fetch(path, { method: method, headers: headers, body: opts.body,
                          signal: ctl.signal, cache: "no-store", credentials: "omit" })
    .then(async function (r) {
      SENT.set(r, gen);
      // The event stream has its own idle deadline. All ordinary answers are read within
      // this request's deadline, including a peer that sends headers and then stalls.
      if (path === "/api/events" || !r.headers) { return r; }
      var kind = r.headers.get("content-type") || "";
      var photo = /^\/api\/(face\?|selfies\/photo\?)/.test(path);
      var maximum = photo ? 4 * 1024 * 1024 : path.indexOf("/api/selfies/images?") === 0 ? 24 * 1024 * 1024 : 1024 * 1024;
      var bytes = await boundedBody(r, maximum);
      if (photo && r.status === 200) { r.blob = function () { return Promise.resolve(new Blob([bytes], { type: kind })); }; }
      else if (kind.indexOf("application/json") >= 0) {
        var j = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
        if (!j || typeof j !== "object" || Array.isArray(j) || (r.status === 200 && !Object.keys(j).length)) {
          throw new Error("The console's answer could not be read.");
        }
        if (method === "GET" && r.status === 200 && !validView(path, j)) {
          throw new Error("The console's answer could not be read.");
        }
        r.json = function () { return Promise.resolve(j); };
      } else if (r.status === 200) { throw new Error("The console's answer could not be read."); }
      // A well-formed error is still an answer: callers own absent/conflict semantics.
      // Only interval reads advertise retries. One-shot panels keep their own errors.
      if (method === "GET" && opts.poll && !photo && r.status !== 403 && gen === GEN) {
        var answered = r.status < 400 || r.status === 404 || r.status === 409;
        emit("request-health", { path: path, ok: answered,
          error: !answered && j ? str(j.error) : "" });
      }
      return r;
    });
  // Bank/deeper search may take 20 s; a clean stop may need more than 60 s.
  return deadline(work, opts.timeoutMs || (method === "POST" ? 120000 : 30000), abort)
    .catch(function (e) {
      abort();
      if (method === "GET" && opts.poll && path !== "/api/events" && gen === GEN && !/^\/api\/(face|selfies\/photo)/.test(path)) {
        emit("request-health", { path: path, ok: false, error: str(e.message) });
      }
      throw e;
    }).finally(function () { if (outer && path !== "/api/events") { outer.removeEventListener("abort", abort); } });
}
// A 403: the console refused the key the request carried. Only a refusal of the key in use now
// says the key is lost ("key-lost"): a request still on its way when a new key was pasted (a
// tab's poll, the stream's probe) was refused the old one, which says nothing of the new.
export function refused(r) {
  if (!r || r.status !== 403) { return false; }
  if (SENT.get(r) === GEN) { emit("key-lost"); }
  return true;
}

// A change: the answer's JSON with its status. An answer that is not JSON is the console's own
// refusal (its gate: a 403 is the key's) or a failure, worded as such.
export function post(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" },
                     body: JSON.stringify(body) })
    .then(function (r) {
      return r.json().catch(function () {
        return { ok: false, error: refused(r) ? KEY_REFUSED : "HTTP " + r.status };
      }).then(function (j) { j = obj(j); j.status = r.status; return j; });
    }).catch(function () {
      return { ok: false, status: 0, error: "The console did not confirm this action. It may have completed; check its state before trying again." };
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

// The bus: "snapshot", "status", "voice", "chat", "chat_gone", "control", "raid", "tuning",
// "selfie" and "board" carry what the console's event stream sent; "key-lost" says the console refused the
// key; "note" is one of the console's own notes for the toasts; "feed" says the Comms feed, its
// read marks or its open view changed; "typing" says who writes him a whisper changed (Comms'
// dots). Listeners run in the order they were added.
export function on(kind, fn) { (LISTENERS[kind] = LISTENERS[kind] || []).push(fn); }
export function emit(kind, data) {
  (LISTENERS[kind] || []).slice().forEach(function (fn) { fn(data); });
}
