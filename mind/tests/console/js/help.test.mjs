import test from "node:test";
import assert from "node:assert/strict";
import { bindHelp, section } from "../../../leeroy_mind/console/static/js/help.mjs";
import { validView } from "../../../leeroy_mind/console/static/js/resilience.mjs";

test("tab section keeps nested headings and literal text until its next peer", () => {
  const guide = "# Guide\r\n## Lab\r\nOne\r\n### Detail\r\n<img onerror=x>\r\n## Ops\r\nTwo";
  assert.equal(section(guide, "lab"), "One\n### Detail\n<img onerror=x>");
  assert.equal(section(guide, "ops"), "Two");
  assert.equal(section(guide, "mind"), "");
  const payload = {guide, version: "0.1", source_commit: null, checkout_commit: "a".repeat(40)};
  assert.ok(validView("/api/guide", payload));
  assert.equal(validView("/api/guide", {...payload, checkout_commit: "bad"}), false);
  assert.equal(validView("/api/guide", {...payload, guide: null}), false);
});

test("closing and reopening discards late guide loads and restores focus", async () => {
  class Element extends EventTarget {
    isConnected = true;
    focus() { this.focused = true; }
    showModal() { this.open = true; }
    close() { this.open = false; this.dispatchEvent(new Event("close")); }
    click() { this.dispatchEvent(new Event("click")); }
  }
  const [button, dialog, title, words, about, close, full] = Array.from({length: 7}, () => new Element());
  let tab = "raid";
  const pending = [];
  bindHelp(button, dialog, title, words, about, close, full, () => new Promise((yes, no) => pending.push({yes, no})),
    () => tab, () => 1);
  button.click();
  close.click();
  assert.ok(button.focused);
  tab = "bank";
  button.click();
  const data = {guide: "## Raid\nOld\n## Bank\n<img onerror=x>", version: "0.1",
    source_commit: null, checkout_commit: null};
  pending[1].yes(data);
  await Promise.resolve();
  pending[0].no(new Error("earlier request failed"));
  await Promise.resolve();
  assert.equal(title.textContent, "Bank help");
  assert.equal(words.textContent, "<img onerror=x>");
  assert.match(about.textContent, /Source commit unavailable/);
  full.click();
  assert.equal(words.textContent, data.guide);
  close.click();
  button.click();
  pending[2].no(new Error("unavailable"));
  await Promise.resolve();
  assert.match(words.textContent, /Could not read/);
});
