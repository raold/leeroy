import { $, api, keyGen, refused } from "./core.mjs";
import { S } from "./state.mjs";

export function section(guide, tab) {
  const lines = guide.split(/\r?\n/);
  const start = lines.findIndex(line => line.toLowerCase() === "## " + tab);
  if (start < 0) { return ""; }
  let end = start + 1;
  while (end < lines.length && !/^#{1,2} /.test(lines[end])) { end++; }
  return lines.slice(start + 1, end).join("\n").trim();
}

export function bindHelp(button, dialog, title, words, about, close, full, read, currentTab, generation) {
  let request = 0, documentText = "";
  button.addEventListener("click", async () => {
    const tab = currentTab(), id = ++request, key = generation();
    documentText = "";
    title.textContent = tab.charAt(0).toUpperCase() + tab.slice(1) + " help";
    words.textContent = "Reading the guide…";
    about.textContent = "";
    full.hidden = true;
    dialog.showModal();
    try {
      const answer = await read();
      if (id !== request || !dialog.open || key !== generation()) { return; }
      documentText = answer.guide;
      words.textContent = section(documentText, tab) || "This tab has no guide section.";
      words.scrollTop = 0;
      about.textContent = "About · Console version " + (answer.version || "unavailable")
        + " · Source commit " + (answer.source_commit || "unavailable")
        + (answer.checkout_commit && answer.checkout_commit === answer.source_commit ? " (managed checkout)"
          : " · Managed checkout " + (answer.checkout_commit || "unavailable"));
      full.hidden = false;
    } catch {
      if (id === request && dialog.open) { words.textContent = "Could not read the console guide. Close and reopen Help to retry."; }
    }
  });
  full.addEventListener("click", () => {
    title.textContent = "Console guide";
    words.textContent = documentText;
    full.hidden = true;
    words.scrollTop = 0;
    words.focus();
  });
  close.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    if (dialog.open) { return; }
    request++;
    documentText = "";
    if (button.isConnected) { button.focus(); }
  });
}

if (typeof document !== "undefined") {
  bindHelp($("help"), $("help-dialog"), $("help-title"), $("help-words"), $("help-about"),
    $("help-close"), $("help-full"), async () => {
      const response = await api("/api/guide");
      if (refused(response) || !response.ok) { throw new Error(); }
      return response.json();
    }, () => S.tab, keyGen);
}
