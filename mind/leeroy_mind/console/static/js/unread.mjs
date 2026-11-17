// The unread signal outside the page: a count in the tab's title and a dot on its icon for
// whispers, DMs and mentions, and (behind the top bar's Notify switch) a browser notification
// that names who wrote, never what.
//
// In sight and focused the title is plain: the page's own badges say it. In sight without the
// focus it counts what the page already knows (the event stream is still open). Hidden, the
// stream is closed and nothing else polls, so ONE light read (/api/unread, at most every 20 s)
// counts what came since the page went out of sight.
import { $, api, emit, on, recall, refused, remember } from "./core.mjs";
import { FEED } from "./comms/feedview.mjs";
import { DISCORD } from "./comms/discordstate.mjs";
import { HIDDEN_POLL_MS, adoptTops, iconHref, notifyBody, shouldNotify, titleText, total, unreadPath } from "./unreadmodel.mjs";

var NOTIFY_KEY = "guild-console-notify";
var base = 0, marks = { chat: null, discord: null }, timer = null, busy = false, keyLost = false;
var shown = 0, lastKey = "", notifyOn = recall(NOTIFY_KEY, "off") === "on";

function icon() { return document.querySelector('link[rel="icon"]'); }
function show(n) {
  shown = n;
  var text = titleText(n);
  if (document.title !== text) { document.title = text; }
  var link = icon(), href = iconHref(n > 0);
  if (link && link.getAttribute("href") !== href) { link.setAttribute("href", href); }
}
// What the page itself knows is unread: whisper threads, DMs and group DMs
function known() {
  var whispers = FEED.threads().reduce(function (n, t) { return n + (t.unread > 0 ? t.unread : 0); }, 0);
  var dms = DISCORD.channels().reduce(function (n, c) {
    return n + (/^(?:dm|gdm):/.test(String(c.id)) && c.unread > 0 ? c.unread : 0);
  }, 0);
  return whispers + dms;
}
function supported() { return typeof Notification === "function"; }
function announce(latest, now, before) {
  if (!notifyOn || !supported() || Notification.permission !== "granted" || !shouldNotify(latest, lastKey, now, before)) { return; }
  try {
    var n = new Notification("Guild Console", { body: notifyBody(latest), tag: "guild-console" });
    n.onclick = function () { window.focus(); n.close(); };
  } catch (e) { /* a browser that only notifies from a service worker: the title still counts */ }
}
function poll() {
  if (busy || keyLost || !document.hidden) { return; }
  busy = true;
  api(unreadPath(marks)).then(function (r) {
    if (refused(r) || r.status !== 200) { return null; }
    return r.json();
  }).then(function (j) {
    if (!j || !document.hidden) { return; }
    marks = adoptTops(marks, j);
    var before = shown, now = total(base, j);
    show(now);
    announce(j.latest, now, before);
    if (j.latest && typeof j.latest.key === "string") { lastKey = j.latest.key; }
  }).catch(function () { /* no connection: the count stays as it was */ })
    .then(function () { busy = false; });
}
function hide() {
  base = known();
  marks = { chat: FEED.floor === null ? null : FEED.top(),
            discord: DISCORD.loaded ? DISCORD.channels().reduce(function (m, c) { return Math.max(m, c.last_id > 0 ? c.last_id : 0); }, 0) : null };
  show(base);
  clearInterval(timer);
  timer = setInterval(poll, HIDDEN_POLL_MS);
  poll();
}
function settle() {
  if (document.hidden) { return; }
  clearInterval(timer);
  timer = null;
  show(document.hasFocus() ? 0 : known());
}
document.addEventListener("visibilitychange", function () { if (document.hidden) { hide(); } else { settle(); } });
window.addEventListener("focus", settle);
window.addEventListener("blur", settle);
on("feed", settle);
on("discord-read", settle);
on("discord-view", settle);
on("key-lost", function () { keyLost = true; });
on("snapshot", function () { keyLost = false; });

// -- the Notify switch: permission is asked for on its click only ---------------------------------
function drawSwitch() {
  var b = $("notify");
  b.setAttribute("aria-pressed", String(notifyOn));
  b.textContent = notifyOn ? "Notify: on" : "Notify: off";
  b.title = !supported() ? "This browser has no notifications."
    : notifyOn ? "A notification names who wrote while this page is out of sight; never the message."
    : "Notify me of whispers, DMs and mentions while this page is out of sight.";
}
$("notify").addEventListener("click", function () {
  if (notifyOn) { notifyOn = false; remember(NOTIFY_KEY, "off"); drawSwitch(); return; }
  if (!supported()) { emit("note", "This browser has no notifications."); return; }
  Promise.resolve(Notification.permission === "granted" ? "granted" : Notification.requestPermission()).then(function (answer) {
    if (answer !== "granted") {
      emit("note", "Notifications are blocked for this page: allow them in the browser's site settings.");
      return;
    }
    notifyOn = true;
    remember(NOTIFY_KEY, "on");
    drawSwitch();
  }, function () { emit("note", "The browser did not answer about notifications."); });
});
// A switch left on whose permission the browser has since taken back reads off
if (notifyOn && (!supported() || Notification.permission !== "granted")) { notifyOn = false; }
drawSwitch();
settle();
