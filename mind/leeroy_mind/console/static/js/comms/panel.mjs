// Hide or show Comms' right panel (the room's faces and the member card), remembered like the
// theme; theme.js applied the stored choice before the first paint. Hide sits in the panel's top
// corner; while the panel is hidden, Show sits in the feed's (the CSS shows one or the other).
import { $, remember } from "../core.mjs";

export var PANEL_KEY = "guild-console-comms-panel";

export function panelHidden() { return document.documentElement.dataset.commsPanel === "hidden"; }

export function setPanel(hidden) {
  if (hidden) { document.documentElement.dataset.commsPanel = "hidden"; }
  else { delete document.documentElement.dataset.commsPanel; }
  remember(PANEL_KEY, hidden ? "hidden" : "shown");
}

export function initPanelToggle() {
  var hide = $("c-panel"), show = $("c-panel-show");
  if (!hide || !show) { return; }
  // the clicked button disappears, so focus moves to the one that takes its place
  hide.addEventListener("click", function () { setPanel(true); show.focus(); });
  show.addEventListener("click", function () { setPanel(false); hide.focus(); });
}
