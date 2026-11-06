// Photos by key, with a memory of "no photo": a photo is answered at once for ttlMs and a miss
// (a 404 or a failed load) for missMs, at most maxActive loads run together, and one load serves
// every asker of a key. While a photo is reloaded, peek still answers the old one, which is let
// go (drop) only once the new answer is in. No DOM.

export class FaceCache {
  constructor(o) {
    this.load = o.load;
    this.now = o.now || Date.now;
    this.ttlMs = o.ttlMs === undefined ? 60000 : o.ttlMs;
    this.missMs = o.missMs === undefined ? 300000 : o.missMs;
    this.maxActive = o.maxActive === undefined ? 4 : o.maxActive;
    this.drop = o.drop || function () {};
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
    if (e && (e.pending || this.now() - e.at < (e.value ? this.ttlMs : this.missMs))) { return e.promise; }
    if (!e) {
      e = { pending: false, settled: false, at: 0, value: null, promise: null };
      this.entries.set(key, e);
    }
    e.pending = true;
    e.promise = new Promise((resolve) => { e.resolve = resolve; });
    this.waiting.push({ key: key, entry: e });
    this._pump();
    return e.promise;
  }
  forget() {
    this.entries.forEach((e) => { if (e.value) { this.drop(e.value); } });
    this.entries.clear();
  }
  _pump() {
    while (this.active < this.maxActive && this.waiting.length) {
      var job = this.waiting.shift();
      this.active++;
      this._run(job);
    }
  }
  _run(job) {
    var done = (value) => {
      var e = job.entry, old = e.value, kept = this.entries.get(job.key) === e;
      e.pending = false; e.settled = true; e.value = value; e.at = this.now();
      this.active--;
      if (kept && old && old !== value) { this.drop(old); }
      if (!kept && value) { this.drop(value); }  // forgotten while it loaded (forget let the old go)
      e.resolve(value);
      this._pump();
    };
    var p;
    try { p = Promise.resolve(this.load(job.key)); } catch (x) { p = Promise.resolve(null); }
    p.then(function (v) { done(v === undefined ? null : v); }, function () { done(null); });
  }
}
