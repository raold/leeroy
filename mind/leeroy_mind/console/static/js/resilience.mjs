// Bounded waits and reconnect delays, independent of the page and testable with fake timers.
export function deadline(work, ms, cancel) {
  var timer;
  var timeout = new Promise(function (_, reject) {
    timer = setTimeout(function () {
      if (cancel) { cancel(); }
      reject(new Error("The console did not answer in time."));
    }, ms);
  });
  return Promise.race([work, timeout]).finally(function () { clearTimeout(timer); });
}

export class Backoff {
  constructor(o) {
    o = o || {};
    this.base = o.base || 1000; this.cap = o.cap || 30000;
    this.random = o.random || Math.random; this.attempt = 0;
  }
  reset() { this.attempt = 0; }
  next() {
    var ceiling = Math.min(this.cap, this.base * Math.pow(2, Math.min(20, this.attempt++)));
    return Math.round(ceiling * (0.5 + 0.5 * this.random()));
  }
}

// Only the stable outer shape: optional sources and old databases may legitimately have
// empty arrays or null detail fields. A different JSON object must not erase a good tab.
var VIEWS = { state: ["rooms", "array"], feed: ["lines", "array"], raid: ["mind", "object"],
  guild: ["roster", "array"], personas: ["members", "array"], persona: ["member", "object"],
  social: ["feuds", "array"], board: ["forum", "array"], calendar: ["upcoming", "array"],
  bank: ["ledger", "array"], realm: ["rivals", "array"], training: ["setup", "boolean"],
  dojo: ["setup", "boolean"], library: ["setup", "boolean"], health: ["mind", "object"],
  tuning: ["view", "object"], selfies: ["view", "object"] };
export function validView(path, value) {
  var route = path.split("?")[0].slice(5), rule = VIEWS[route];
  if (!rule) { return true; }
  var field = value[rule[0]];
  return rule[1] === "array" ? Array.isArray(field) :
    rule[1] === "object" ? !!field && typeof field === "object" && !Array.isArray(field) : typeof field === rule[1];
}

export async function boundedBody(response, maximum) {
  var length = Number(response.headers.get("content-length") || 0);
  if (length > maximum) { throw new Error("The console's answer was too large."); }
  if (!response.body) { return new Uint8Array(); }
  var reader = response.body.getReader(), chunks = [], size = 0;
  try {
    while (true) {
      var part = await reader.read();
      if (part.done) { break; }
      size += part.value.byteLength;
      if (size > maximum) { throw new Error("The console's answer was too large."); }
      chunks.push(part.value);
    }
  } catch (e) { await reader.cancel().catch(function () {}); throw e; }
  var bytes = new Uint8Array(size), at = 0;
  chunks.forEach(function (c) { bytes.set(c, at); at += c.byteLength; });
  return bytes;
}
