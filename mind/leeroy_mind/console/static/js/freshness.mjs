import { $, el, on } from "./core.mjs";
import { S } from "./state.mjs";

// Route names, never queries or response text. Detail reads belong to their parent tab.
var TABS = { state: "comms", feed: "comms", raid: "raid", guild: "guild", social: "social",
  personas: "personas", persona: "personas", tuning: "personas", selfies: "personas",
  insight: "personas", board: "board", calendar: "calendar", bank: "bank", realm: "realm",
  training: "training", dojo: "dojo", library: "library", health: "mind", ops: "ops", discord: "comms" };
var READS = new Map();
function draw() {
  var panel = $("tab-" + S.tab);
  if (!panel) { return; }
  var id = S.tab + "-stale", node = $(id), failed = [];
  READS.forEach(function (r) { if (r.tab === S.tab && r.failed) { failed.push(r); } });
  if (!node && failed.length) {
    node = el("p", "stale-note"); node.id = id;
    node.setAttribute("role", "status"); node.setAttribute("aria-live", "polite");
    panel.prepend(node);
  }
  if (!node) { return; }
  node.hidden = !failed.length;
  if (!failed.length) { return; }
  var last = Math.min.apply(null, failed.map(function (r) { return r.good; }));
  var age = last ? Math.max(0, Math.floor((Date.now() - last) / 1000)) : null;
  node.textContent = age === null ? "Updates unavailable. Retrying; no successful answer yet." :
    "Stale: keeping the last good data, received " + age + " seconds ago. Retrying.";
}
on("request-health", function (e) {
  var route = e.path.split("?")[0].split("/")[2], tab = TABS[route];
  if (!tab) { return; }
  var entry = READS.get(route) || { tab: tab, good: 0 };
  entry.failed = !e.ok;
  if (e.ok) { entry.good = Date.now(); }
  READS.set(route, entry); draw();
});
// One page timer; no data polls and no DOM work in hidden pages.
setInterval(function () { if (!document.hidden) { draw(); } }, 15000);
