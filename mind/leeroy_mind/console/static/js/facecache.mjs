// Photos by key, with a memory of "no photo": a hit or a 404 is answered at once for ttlMs, at
// most maxActive loads run together, and one load serves every asker of a key. No DOM.

export class FaceCache {
  constructor(o) {
    this.load = o.load;
    this.now = o.now || Date.now;
    this.ttlMs = o.ttlMs === undefined ? 60000 : o.ttlMs;
    this.maxActive = o.maxActive === undefined ? 4 : o.maxActive;
    this.drop = o.drop || function () {};
    this.entries = new Map();
    this.waiting = [];
    this.active = 0;
  }
  get(key) {
    var e = this.entries.get(key);
    if (e && (e.pending || this.now() - e.at < this.ttlMs)) { return e.promise; }
    if (e && e.value) { this.drop(e.value); }
    e = { pending: true, at: 0, value: null, promise: null };
    e.promise = new Promise((resolve) => { e.resolve = resolve; });
    this.entries.set(key, e);
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
      var e = job.entry;
      e.pending = false; e.value = value; e.at = this.now();
      this.active--;
      e.resolve(value);
      this._pump();
    };
    var p;
    try { p = Promise.resolve(this.load(job.key)); } catch (x) { p = Promise.resolve(null); }
    p.then(function (v) { done(v === undefined ? null : v); }, function () { done(null); });
  }
}
