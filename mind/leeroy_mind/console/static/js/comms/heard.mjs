// His push-to-talk as the voice service recognised it, kept until the mind's own voiced line shows
// it was matched. Pure: no DOM, no storage, nothing logged (the text is chat).

export var HEARD_WAIT_MS = 6000;
var KEPT = 5;

export class Heard {
  constructor() { this._kept = []; }

  heardYou(entry, nowMs) {
    var text = entry && typeof entry.text === "string" ? entry.text.trim() : "";
    if (!text) { return; }
    var ts = entry && typeof entry.ts === "number" && isFinite(entry.ts) ? entry.ts : nowMs;
    this._kept.push({ text: text, ts: ts });
    while (this._kept.length > KEPT) { this._kept.shift(); }
  }

  // An own line on a voice room clears the entries it can answer: at or before it and still
  // inside the wait; one already overdue is left for due()
  sawOwnVoiceLine(line, nowMs) {
    if (!line || line.you !== true || typeof line.channel !== "string" || line.channel.indexOf("voice:") !== 0) { return; }
    this._kept = this._kept.filter(function (e) {
      return !(e.ts <= nowMs && nowMs - e.ts <= HEARD_WAIT_MS);
    });
  }

  due(nowMs) {
    var out = [];
    this._kept = this._kept.filter(function (e) {
      if (nowMs - e.ts > HEARD_WAIT_MS) { out.push({ text: e.text, ts: e.ts }); return false; }
      return true;
    });
    return out;
  }
}
