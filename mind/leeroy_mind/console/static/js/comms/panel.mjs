// Hide or show Comms' right panel (the room's faces and the member card), remembered like the
// theme; theme.js applied the stored choice before the first paint.
import { $, remember } from "../core.mjs";

export var PANEL_KEY = "guild-console-comms-panel";

export function panelHidden() { return document.documentElement.dataset.commsPanel === "hidden"; }

export function setPanel(hidden) {
  if (hidden) { document.documentElement.dataset.commsPanel = "hidden"; }
  else { delete document.documentElement.dataset.commsPanel; }
  remember(PANEL_KEY, hidden ? "hidden" : "shown");
  showPanelButton();
}

function showPanelButton() {
  var button = $("c-panel");
  if (!button) { return; }
  var hidden = panelHidden();
  button.textContent = hidden ? "Show panel" : "Hide panel";
  button.setAttribute("aria-pressed", String(hidden));
  button.setAttribute("aria-expanded", String(!hidden));
}

export function initPanelToggle() {
  var button = $("c-panel");
  if (!button) { return; }
  button.addEventListener("click", function () { setPanel(!panelHidden()); });
  showPanelButton();
}
