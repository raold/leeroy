// His push-to-talk as the voice service recognised it, kept until the mind's own voiced line shows
// it was matched. Pure: no DOM, no storage, nothing logged (the text is chat).

export var HEARD_WAIT_MS = 6000;
var KEPT = 5;
var LINE_SLACK_MS = 2000;

export class Heard {
  constructor() { this._kept = []; }

  // The entry's time is the page's receipt time: the voice service's clock is not trusted
  heardYou(entry, nowMs) {
    var text = entry && typeof entry.text === "string" ? entry.text.trim() : "";
    if (!text) { return; }
    this._kept.push({ text: text, at: nowMs });
    while (this._kept.length > KEPT) { this._kept.shift(); }
  }

  clear() { this._kept = []; }

  // An own line on a voice room clears the entries it can answer: received no later than the line
  // was made (2 s tolerance; a line without a time counts as made now) and still inside the wait;
  // an older replayed line, or an entry already overdue, is left alone
  sawOwnVoiceLine(line, nowMs) {
    if (!line || line.you !== true || typeof line.channel !== "string" || line.channel.indexOf("voice:") !== 0) { return; }
    var made = typeof line.ts === "number" && isFinite(line.ts) ? line.ts * 1000 : nowMs;
    this._kept = this._kept.filter(function (e) {
      return !(made >= e.at - LINE_SLACK_MS && nowMs - e.at <= HEARD_WAIT_MS);
    });
  }

  due(nowMs) {
    var out = [];
    this._kept = this._kept.filter(function (e) {
      if (nowMs - e.at > HEARD_WAIT_MS) { out.push({ text: e.text, ts: e.at }); return false; }
      return true;
    });
    return out;
  }
}
