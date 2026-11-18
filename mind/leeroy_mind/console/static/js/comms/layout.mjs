// Comms in a small window. Two measured lengths go to the stylesheet as custom properties:
// --strip-h, the status strip's height (under 900 px the page itself scrolls and the header
// sticks that far above the window's top, so only the tab bar stays), and --comms-h, the height
// the window has left for the feed's row (up to 1100 px wide, where Comms does not fill the
// window by itself), so the reply box is in sight without scrolling.
// Under 900 px the channel list is a drawer: Channels opens it over the feed, and choosing a
// conversation, Close, the backdrop or Escape closes it.
import { $, el, on } from "../core.mjs";
import { feedHeight } from "./layoutmodel.mjs";
import { currentView } from "./feedview.mjs";

var root = document.documentElement, header = document.querySelector(".top"),
    strip = document.querySelector(".top-inner"), comms = document.querySelector(".comms");

function fit() {
  header.style.setProperty("--strip-h", strip.offsetHeight + "px");
  if ($("tab-comms").hidden) { return; }
  var style = getComputedStyle(comms);
  var top = comms.getBoundingClientRect().top + document.querySelector("main").scrollTop + window.scrollY;
  comms.style.setProperty("--comms-h", feedHeight(window.innerHeight, top, parseFloat(style.paddingTop) || 0) + "px");
}
window.addEventListener("resize", fit);
if (typeof ResizeObserver === "function") {
  var watch = new ResizeObserver(fit);
  [header, $("comms-kpis"), $("comms-banner")].forEach(function (e) { watch.observe(e); });
}
on("comms-show", fit);
on("snapshot", fit);
fit();

// -- the drawer -----------------------------------------------------------------------------------
var opener = $("c-drawer"), closer = $("c-drawer-close");
function isOpen() { return root.dataset.commsDrawer === "open"; }
function setDrawer(open, refocus) {
  if (open) { root.dataset.commsDrawer = "open"; } else { delete root.dataset.commsDrawer; }
  opener.setAttribute("aria-expanded", String(open));
  if (open) { closer.focus({ preventScroll: true }); }
  else if (refocus) { opener.focus({ preventScroll: true }); }
}
opener.addEventListener("click", function () { setDrawer(!isOpen(), false); });
closer.addEventListener("click", function () { setDrawer(false, true); });
$("c-backdrop").addEventListener("click", function () { setDrawer(false, true); });
document.addEventListener("keydown", function (ev) {
  if (ev.key === "Escape" && isOpen()) { ev.preventDefault(); setDrawer(false, true); }
});
// A conversation chosen: the feed is what he asked for
$("comms-left").addEventListener("click", function (ev) {
  if (isOpen() && ev.target.closest("[data-view]") && !ev.target.closest("[data-card]")) { setDrawer(false, false); }
});
window.addEventListener("resize", function () { if (isOpen() && window.innerWidth > 900) { setDrawer(false, false); } });
on("comms-hide", function () { if (isOpen()) { setDrawer(false, false); } });
// Another conversation opened by anything else (a new group made in the drawer, a member card)
var shownView = currentView();
on("feed", function () {
  var now = currentView();
  if (now !== shownView) { shownView = now; if (isOpen()) { setDrawer(false, false); } }
});
// The Channels button carries the Comms tab's own unread count
function count() {
  var from = $("t-comms").querySelector(".badge"), mark = opener.querySelector(".badge");
  if (!from) { if (mark) { mark.remove(); } opener.setAttribute("aria-label", "Channels"); return; }
  if (!mark) { mark = el("span", "badge"); opener.appendChild(mark); }
  mark.textContent = from.textContent;
  opener.setAttribute("aria-label", "Channels, " + from.textContent + " unread");
}
on("feed", count);
on("discord-read", count);
on("discord-view", count);
