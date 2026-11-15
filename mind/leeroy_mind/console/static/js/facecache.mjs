// Photos by key, with a memory of "no photo": a photo is answered at once for ttlMs and a miss
// for missMs, at most maxActive loads run together, and one load serves every asker of a key.
// load(key, previous) answers a photo, or null for none (a 404), and throws when it got no answer
// (a server error, no connection, a timeout); it may answer previous itself when the photo did not
// change, and that photo is then kept as it is. A first load that throws is a miss, or, with
// retryMs (a list of delays, the last one repeated), is asked again after the next delay while its
// askers keep waiting, until an answer comes or the key is forgotten; an error marked final (a
// refused key, which asking again cannot change) is a miss all the same. A reload that throws keeps
// the photo it had for another ttlMs. While a photo is reloaded, peek still answers the old one;
// when a reload brings another answer (or a photo for a miss), replaced(key, answer) is called
// with the new answer in place, and only then is the old photo let go (drop). Beyond maxEntries
// the oldest settled keys are let go, except a photo inUse(key, photo) says is still shown; a
// burst may exceed the cap. No DOM.

export class FaceCache {
  constructor(o) {
    this.load = o.load;
    this.now = o.now || function () { return Date.now(); };
    this.ttlMs = o.ttlMs === undefined ? 60000 : o.ttlMs;
    this.missMs = o.missMs === undefined ? 300000 : o.missMs;
    this.retryMs = Array.isArray(o.retryMs) && o.retryMs.length ? o.retryMs : null;
    this.maxActive = o.maxActive === undefined ? 4 : o.maxActive;
    this.maxEntries = o.maxEntries === undefined ? 256 : Math.max(1, o.maxEntries);
    this.drop = o.drop || function () {};
    this.replaced = o.replaced || function () {};
    this.inUse = o.inUse || function () { return false; };
    this.entries = new Map();
    this.waiting = [];
    this.active = 0;
  }
  // The latest answer for key at once: a photo, null for none, undefined before the first one
  peek(key) {
    var e = this.entries.get(key);
    return e && e.settled ? e.value : undefined;
  }
  get(key) {
    var e = this.entries.get(key);
    if (e) { this.entries.delete(key); this.entries.set(key, e); }
    if (e && (e.pending || this.now() - e.at < (e.value ? this.ttlMs : this.missMs))) { return e.promise; }
    if (!e) {
      // A queued, active or retrying load still has consumers waiting for its URL, and a photo
      // still on screen is still used. Only the others can be evicted.
      for (const [key, entry] of this.entries) {
        if (this.entries.size < this.maxEntries) { break; }
        if (!entry.pending && !(entry.value && this.inUse(key, entry.value))) { this.forgetKey(key); }
      }
      e = { pending: false, settled: false, at: 0, value: null, promise: null, tries: 0, timer: null };
      this.entries.set(key, e);
    }
    e.pending = true;
    e.promise = new Promise((resolve) => { e.resolve = resolve; });
    this.waiting.push({ key: key, entry: e });
    this._pump();
    return e.promise;
  }
  forget() {
    Array.from(this.entries.keys()).forEach((key) => { this.forgetKey(key); });
  }
  // forget for one key only: its photo is let go and its next get loads it again
  forgetKey(key) {
    var e = this.entries.get(key);
    if (!e) { return; }
    if (e.timer) { clearTimeout(e.timer); e.timer = null; }
    if (e.value) { this.drop(e.value); }
    e.value = null;
    this.entries.delete(key);
    this.waiting = this.waiting.filter((job) => job.entry !== e);
    if (e.pending) { e.resolve(null); }
  }
  _pump() {
    while (this.active < this.maxActive && this.waiting.length) {
      var job = this.waiting.shift();
      this.active++;
      this._run(job);
    }
  }
  _run(job) {
    var e = job.entry;
    var done = (value) => {
      var old = e.value, known = e.settled, kept = this.entries.get(job.key) === e;
      e.pending = false; e.settled = true; e.value = value; e.at = this.now(); e.tries = 0;
      this.active--;
      // What showed the old answer (a photo, or the initials of a miss) shows the new one first
      if (kept && known && old !== value) { this.replaced(job.key, value); }
      if (kept && old && old !== value) { this.drop(old); }
      if (!kept && value) { this.drop(value); }  // forgotten while it loaded (forget let the old go)
      e.resolve(kept ? value : null);  // a dropped photo is never handed out
      this._pump();
    };
    var failed = (x) => {
      var kept = this.entries.get(job.key) === e;
      if (!kept || e.value || !this.retryMs || (x && x.final)) { done(kept ? e.value : null); return; }
      // No answer yet and no photo to keep: its askers wait for the next try
      var delay = this.retryMs[Math.min(e.tries, this.retryMs.length - 1)];
      e.tries++;
      this.active--;
      e.timer = setTimeout(() => {
        e.timer = null;
        if (this.entries.get(job.key) === e && e.pending) { this.waiting.push(job); this._pump(); }
      }, delay);
      this._pump();
    };
    var p;
    try { p = Promise.resolve(this.load(job.key, e.value)); } catch (x) { p = Promise.reject(x); }
    p.then(function (v) { done(v === undefined ? null : v); }, failed);
  }
}
