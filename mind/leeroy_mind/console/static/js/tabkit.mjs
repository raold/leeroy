import { $, api, clear, el, refused, str } from "./core.mjs";
import { S } from "./state.mjs";
import { registerTab } from "./tabs.mjs";

// What every data tab shares: a poll that runs only while the tab is open and the page visible
// (one request in flight), its banner, its "not set up" note and its key figures.

// Show (text) or hide (null) the banner #<name>-banner
export function banner(name, text) {
  var b = $(name + "-banner");
  if (!b) { return; }
  b.textContent = text || "";
  b.hidden = !text;
}

// A section's note that its source is missing
export function notSetUp(box, text) {
  clear(box);
  box.appendChild(el("p", "note", "Not set up yet: " + text));
}

// The key figures band: items {k, v, d}
export function renderKpis(box, items) {
  clear(box);
  items.forEach(function (x) {
    var k = el("div", "kpi");
    k.appendChild(el("span", "k", str(x.k)));
    k.appendChild(el("span", "v", str(x.v)));
    k.appendChild(el("span", "d", str(x.d)));
    box.appendChild(k);
  });
}

// Register tab `name`: while shown (and the page visible) fetch api(path) every ms (and at show).
// 403: the console refused the key ("key-lost"); 503: the banner says why; 200: render(view).
export function pollTab(name, opts) {
  var timer = null, busy = false;
  function once() {
    if (busy || S.tab !== name || document.hidden) { return; }
    busy = true;
    api(opts.path).then(function (r) {
      if (refused(r)) { return null; }
      return r.json().then(function (j) { return { status: r.status, j: j }; },
                            function () { return { status: r.status, j: {} }; });
    }).then(function (x) {
      if (!x) { return; }
      if (x.status === 200) {
        banner(name, null);
        try { opts.render(x.j); }
        catch (e) { banner(name, "This tab could not be drawn just now."); }
      }
      else if (x.status === 503) { banner(name, str(x.j.error) || "mind.db could not be read just now."); }
      else { banner(name, "HTTP " + x.status); }
    }).catch(function () { /* no connection: the strip says it */ })
      .then(function () { busy = false; });
  }
  function stop() { clearInterval(timer); timer = null; }
  registerTab(name, {
    show: function () { stop(); once(); timer = setInterval(once, opts.ms); },
    hide: stop
  });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { once(); } });
  return once;
}
