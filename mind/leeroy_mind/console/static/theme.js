// Applies the remembered theme before the first paint. Dark is the page's own default; this
// changes it only for a stored "light" or "dark" (written by core's remember as JSON, so with
// quotes; a bare light or dark is accepted too). Any failure leaves the default.
(function () {
  try {
    var raw = localStorage.getItem("guild-console-theme"), value = raw;
    try { value = JSON.parse(raw); } catch (e) { value = raw; }
    if (value === "light" || value === "dark") { document.documentElement.dataset.theme = value; }
  } catch (e) { /* storage off: keep the default */ }
  // Members' photos hidden (the strip's Hide photos), before the first paint so none flashes
  try {
    var shown = localStorage.getItem("guild-console-photos"), mode = shown;
    try { mode = JSON.parse(shown); } catch (e) { mode = shown; }
    if (mode === "hidden") { document.documentElement.dataset.photos = "hidden"; }
  } catch (e) { /* storage off: photos shown */ }
  // Comms' right panel hidden (its Hide panel), before the first paint so it does not flash
  try {
    var panel = localStorage.getItem("guild-console-comms-panel"), side = panel;
    try { side = JSON.parse(panel); } catch (e) { side = panel; }
    if (side === "hidden") { document.documentElement.dataset.commsPanel = "hidden"; }
  } catch (e) { /* storage off: panel shown */ }
})();
