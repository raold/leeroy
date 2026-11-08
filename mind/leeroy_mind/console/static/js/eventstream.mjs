import { Backoff, deadline } from "./resilience.mjs";

// One connection, one reconnect timer, and a bounded incomplete frame. The server pings
// every 15 s; 45 s of silence also detects a half-open connection after a laptop wakes.
export class ReconnectingStream {
  constructor(o) {
    this.o = o; this.backoff = new Backoff(o);
    this.ctl = null; this.reader = null; this.timer = null; this.stopped = true;
  }
  start() {
    if (!this.stopped) { return; }
    this.stopped = false; this.backoff.reset(); this._connect();
  }
  stop() {
    this.stopped = true;
    clearTimeout(this.timer); this.timer = null;
    if (this.ctl) { this.ctl.abort(); this.ctl = null; }
    if (this.reader) { this.reader.cancel().catch(function () {}); this.reader = null; }
  }
  async _connect() {
    this.timer = null;
    var ctl = new AbortController(); this.ctl = ctl;
    var reader;
    try {
      var r = await this.o.open(ctl.signal);
      if (this.ctl !== ctl || this.stopped) { return; }
      if (this.o.refused(r)) { this.stop(); return; }
      if (!r.ok || !r.body) { throw new Error("No event stream"); }
      reader = r.body.getReader(); this.reader = reader;
      var decoder = new TextDecoder(), buf = "";
      while (this.ctl === ctl && !this.stopped) {
        var step = await deadline(reader.read(), this.o.idleMs || 45000, () => ctl.abort());
        if (step.done) { break; }
        this.backoff.reset(); this.o.connected();
        buf += decoder.decode(step.value, { stream: true });
        if (buf.length > (this.o.maxFrame || 1024 * 1024)) { throw new Error("Event too large"); }
        var at;
        while ((at = buf.indexOf("\n\n")) >= 0) {
          this.o.dispatch(buf.slice(0, at)); buf = buf.slice(at + 2);
        }
      }
    } catch (_) { /* the page retains its last view and reconnects below */ }
    finally {
      ctl.abort();
      if (reader) { reader.cancel().catch(function () {}); }
      if (this.reader === reader) { this.reader = null; }
      if (this.ctl === ctl && !this.stopped) {
        this.ctl = null; this.o.retrying();
        this.timer = setTimeout(() => this._connect(), this.backoff.next());
      }
    }
  }
}
