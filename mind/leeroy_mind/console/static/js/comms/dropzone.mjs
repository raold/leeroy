import { carriesFiles } from "./photos.mjs";

// Files dragged from the desktop onto the open conversation (its feed, header or composer)
// attach as the Attach button's would: `drop(files)` is that button's path. While they are over
// it an overlay says "Drop to attach", or why nothing can be attached now. Text and links
// dragged in drop as the browser drops them. A file dropped anywhere else on the page is refused
// there, so it never opens in place of the console. `open()`: a Discord conversation is shown;
// `state()`: {why, hint}, why nothing can be attached (or '') and what can.
export function setupDropZone({ zone, overlay, title, hint, open, state, drop }) {
  function hide() { overlay.hidden = true; }
  function show() {
    const now = state();
    title.textContent = now.why ? "Can't attach here" : 'Drop to attach';
    hint.textContent = now.why || now.hint;
    overlay.hidden = false;
  }
  function files(ev) { return open() && carriesFiles(ev.dataTransfer && ev.dataTransfer.types); }
  zone.addEventListener('dragenter', ev => { if (files(ev)) { ev.preventDefault(); show(); } });
  zone.addEventListener('dragover', ev => {
    if (!files(ev)) { return; }
    ev.preventDefault(); ev.dataTransfer.dropEffect = 'copy';
    if (overlay.hidden) { show(); }
  });
  // The overlay covers the zone and its children take no pointer, so leaving it leaves the zone
  overlay.addEventListener('dragleave', ev => { if (ev.target === overlay) { hide(); } });
  zone.addEventListener('drop', ev => {
    if (!files(ev)) { return; }
    ev.preventDefault(); hide();
    drop(ev.dataTransfer.files);
  });
  window.addEventListener('dragover', ev => {
    if (carriesFiles(ev.dataTransfer && ev.dataTransfer.types) && !(open() && zone.contains(ev.target))) {
      ev.preventDefault(); ev.dataTransfer.dropEffect = 'none';
    }
  });
  window.addEventListener('drop', ev => {
    if (carriesFiles(ev.dataTransfer && ev.dataTransfer.types)) { ev.preventDefault(); hide(); }
  });
  return { hide };
}
