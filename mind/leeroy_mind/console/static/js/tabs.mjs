import { $, el } from "./core.mjs";
import { S } from "./state.mjs";

// -- tabs -----------------------------------------------------------------------------------------
// Each tab module registers what showing and hiding its tab does: its first fetch and its timer
// start on show and stop on hide, so only the open tab polls.

var TABS = {};

export function registerTab(name, hooks) { TABS[name] = hooks || {}; }

export function showTab(name) {
  if (!$("tab-" + name)) { name = "comms"; }
  var was = TABS[S.tab];
  if (was && was.hide) { was.hide(); }
  S.tab = name;
  var tabs = document.querySelectorAll('[role="tab"]');
  for (var i = 0; i < tabs.length; i++) {
    var on = tabs[i].dataset.tab === name;
    tabs[i].setAttribute("aria-selected", String(on));
    tabs[i].tabIndex = on ? 0 : -1;
    $("tab-" + tabs[i].dataset.tab).hidden = !on;
  }
  if (location.hash !== "#" + name) { history.replaceState(null, "", "#" + name); }
  var now = TABS[name];
  if (now && now.show) { now.show(); }
}

// A count on a tab's button; none when n is not above 0
export function setBadge(name, n) {
  var tab = $("t-" + name);
  if (!tab) { return; }
  var badge = tab.querySelector(".badge");
  if (!(n > 0)) {
    if (badge) { badge.remove(); }
    return;
  }
  if (!badge) {
    badge = el("span", "badge");
    tab.appendChild(badge);
  }
  badge.textContent = String(n);
}

document.querySelector(".tabs").addEventListener("click", function (ev) {
  var b = ev.target.closest('[role="tab"]');
  if (b) { showTab(b.dataset.tab); }
});
document.querySelector(".tabs").addEventListener("keydown", function (ev) {
  if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") { return; }
  var tabs = Array.prototype.slice.call(document.querySelectorAll('[role="tab"]'));
  var i = tabs.findIndex(function (t) { return t.dataset.tab === S.tab; });
  var next = tabs[(i + (ev.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
  showTab(next.dataset.tab);
  next.focus();
});
