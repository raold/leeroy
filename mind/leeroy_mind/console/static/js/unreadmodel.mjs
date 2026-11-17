// Pure rules of the unread signal outside the page (unread.mjs): the tab title's count, the
// favicon's dot and the words of a notification. Never a message's text.

export var TITLE = "Guild Console";
export var HIDDEN_POLL_MS = 20000;  // the one request a hidden page makes, at most this often

function count(x) { return typeof x === "number" && Number.isFinite(x) && x > 0 ? Math.floor(x) : 0; }

// What he already had unread when the page went out of sight, plus what came since
export function total(base, since) {
  var s = since && typeof since === "object" ? since : {};
  return count(base) + count(s.whispers) + count(s.dms) + count(s.mentions);
}
export function titleText(n) {
  n = count(n);
  return n ? "(" + (n > 99 ? "99+" : n) + ") " + TITLE : TITLE;
}
// The page's icon, with a dot at its corner while something is unread
export function iconHref(dot) {
  var svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='#0e141a'/>"
    + "<path d='M16 4.5 26 9v7c0 6-4.6 10-10 11.5C10.6 26 6 22 6 16V9Z' fill='none' stroke='#4fb2bd' stroke-width='2.6' stroke-linejoin='round'/>"
    + "<path d='M11.2 15h9.6M11.2 19.4h6' stroke='#4fb2bd' stroke-width='2.3' stroke-linecap='round'/>"
    + (dot ? "<circle cx='25' cy='7' r='6.5' fill='#e5484d' stroke='#0e141a' stroke-width='2'/>" : "") + "</svg>";
  return "data:image/svg+xml," + encodeURIComponent(svg);
}
// A notification's body: who, never what
export function notifyBody(latest) {
  var name = latest && typeof latest.name === "string" ? latest.name.replace(/[\u0000-\u001f\u007f]/g, "").trim().slice(0, 40) : "";
  return (name || "A member") + " sent a message";
}
// One notification per newest message: only for a message not yet announced, and only when the
// count went up
export function shouldNotify(latest, lastKey, now, before) {
  return !!latest && typeof latest.key === "string" && latest.key !== "" && latest.key !== lastKey && count(now) > count(before);
}
// The read's query: the newest ids the page holds, each left out until it has one
export function unreadPath(marks) {
  var parts = [];
  ["chat", "discord"].forEach(function (k) {
    if (typeof marks[k] === "number" && Number.isSafeInteger(marks[k]) && marks[k] >= 0) { parts.push(k + "=" + marks[k]); }
  });
  return "/api/unread" + (parts.length ? "?" + parts.join("&") : "");
}
// The answer's tops become the page's marks where it had none
export function adoptTops(marks, answer) {
  var out = { chat: marks.chat, discord: marks.discord };
  if (out.chat === null && Number.isSafeInteger(answer.chat_top)) { out.chat = answer.chat_top; }
  if (out.discord === null && Number.isSafeInteger(answer.discord_top)) { out.discord = answer.discord_top; }
  return out;
}
