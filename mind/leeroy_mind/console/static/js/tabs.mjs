import { $, el } from "./core.mjs";
import { S } from "./state.mjs";

// -- tabs -----------------------------------------------------------------------------------------
// Each tab module registers what showing and hiding its tab does: its first fetch and its timer
// start on show and stop on hide, so only the open tab polls.

var TABS = {};
var ROUTE = "comms";  // the open tab and, after a "/", the view inside it ("personas/1234")

export function registerTab(name, hooks) { TABS[name] = hooks || {}; }
export function currentRoute() { return ROUTE; }
// The address without a tab change (a member view left by its own Back control)
export function setRoute(route) {
  ROUTE = route;
  if (location.hash !== "#" + route) { history.replaceState(null, "", "#" + route); }
}

// name: a tab, or "tab/view" for a tab whose hooks have a route (the Personas member page). A
// tab without one, or an unknown tab, opens plainly.
export function showTab(route) {
  var parts = String(route || "").split("/"), name = parts[0], view = parts.slice(1).join("/");
  if (!$("tab-" + name)) { name = "comms"; view = ""; }
  if (!TABS[name] || !TABS[name].route) { view = ""; }
  var was = TABS[S.tab];
  if (was && was.hide) { was.hide(); }
  S.tab = name;
  var tabs = document.querySelectorAll('[role="tab"]');
  for (var i = 0; i < tabs.length; i++) {
    var on = tabs[i].dataset.tab === name;
    tabs[i].setAttribute("aria-selected", String(on));
    tabs[i].tabIndex = on ? 0 : -1;
    $("tab-" + tabs[i].dataset.tab).hidden = !on;
    if (on && tabs[i].parentElement.scrollWidth > tabs[i].parentElement.clientWidth) {
      tabs[i].scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }
  ROUTE = name + (view ? "/" + view : "");
  if (location.hash !== "#" + ROUTE) {
    // Leaving a member page for another tab is a step forward, so Back returns to her page;
    // any other tab change rewrites the address in place, as before.
    if (history.state && history.state.member === true) { history.pushState(null, "", "#" + ROUTE); }
    else { history.replaceState(null, "", "#" + ROUTE); }
  }
  var now = TABS[name];
  if (!document.hidden && now && now.show) { now.show(); }
  if (now && now.route) { now.route(view); }
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
  if (["ArrowRight", "ArrowLeft", "Home", "End"].indexOf(ev.key) < 0) { return; }
  ev.preventDefault();
  var tabs = Array.prototype.slice.call(document.querySelectorAll('[role="tab"]'));
  var i = tabs.findIndex(function (t) { return t.dataset.tab === S.tab; });
  var next = tabs[ev.key === "Home" ? 0 : ev.key === "End" ? tabs.length - 1 :
                  (i + (ev.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
  showTab(next.dataset.tab);
  next.focus();
});

// Older tabs use their own pollers. Their existing hide/show hooks also own visibility,
// so sleeping phones keep no hidden-tab intervals and wake with one immediate refresh.
document.addEventListener("visibilitychange", function () {
  var hooks = TABS[S.tab], action = document.hidden ? "hide" : "show";
  if (hooks && hooks[action]) { hooks[action](); }
});
