import { arr, clear, el, obj, str } from "./core.mjs";
import { SESSIONS, changeText, memberHeat, overlayText, photoText, rowText, sessionArgs, sessionText, worldText } from "./heatmodel.mjs";

// The Tuning page's Heat panel and a member's heat card. Reads the server's view only; every
// change goes through the mind's tune op (ask), which checks it again.
function button(parent, label, live, why, fn) {
  const b = el("button", "", label);
  b.type = "button"; b.disabled = !live;
  if (!live) { b.title = why; }
  b.addEventListener("click", fn);
  parent.appendChild(b);
  return b;
}
function slider(lo, hi, start, label, signed) {
  const range = el("input");
  range.type = "range"; range.min = lo; range.max = hi; range.step = 1; range.value = start;
  range.setAttribute("aria-label", label);
  const shown = el("span", "v", (signed && start > 0 ? "+" : "") + String(start));
  range.addEventListener("input", () => { shown.textContent = (signed && range.value > 0 ? "+" : "") + range.value; });
  return [range, shown];
}
export function heatPanel(box, h, live, why, ask) {
  clear(box);
  h = obj(h);
  if (h.enabled === undefined) { box.appendChild(el("p", "muted", "Unavailable.")); return; }
  box.appendChild(el("p", "lead", h.enabled ? worldText(h) : str(h.note)));
  if (!h.enabled || h.available === false) { overlays(box, h); return; }
  const can = live;
  const row = el("div", "dial"), ctl = el("div", "ctl");
  row.appendChild(el("div", "nm", "heat"));
  const start = h.world === null || h.world === undefined ? Number(h.today) : Number(h.world);
  const [range, shown] = slider(h.lo, h.hi, start, "World heat", false);
  ctl.appendChild(range); ctl.appendChild(shown);
  const means = el("p", "why", rowText(h, start)), changes = el("p", "why", "");
  range.addEventListener("input", () => {
    means.textContent = rowText(h, Number(range.value));
    changes.textContent = changeText(h, Number(range.value));
  });
  button(ctl, "Preview", can, why, () => ask({ action: "preview", scope: "world", target: "", dial: "heat", value: Number(range.value) }, "heat " + range.value));
  button(ctl, "Set", can, why, () => ask({ action: "set", scope: "world", target: "", dial: "heat", value: Number(range.value), previewed: false }, "heat " + range.value));
  button(ctl, "Back to the romance dial", can && h.set === true, why, () => ask({ action: "set", scope: "world", target: "", dial: "heat", value: null, previewed: false }, "heat: cleared"));
  row.appendChild(ctl);
  row.appendChild(el("div", "moves", str(h.moves) + " (" + str(h.when) + ")"));
  box.appendChild(row);
  box.appendChild(means); box.appendChild(changes);
  box.appendChild(el("p", "why", "Public channels: " + (h.public_cap ? "capped at flirty ([heat] public_cap is on)." : "not capped ([heat] public_cap is off: as before the dial).")));
  const photos = photoText(h.photos);
  if (photos) { box.appendChild(el("p", "why", "Photos now. " + photos)); }
  arr(h.groups).forEach(g => box.appendChild(el("p", "muted", "group " + str(g.name) + ": heat " + (g.offset > 0 ? "+" : "") + g.offset)));
  box.appendChild(el("h4", "", "Tonight"));
  const all = sessionText(obj(h.sessions).all);
  box.appendChild(el("p", "why", all ? "Everyone: " + all + "." : "No override for everyone. In a whisper or DM, \"keep it clean tonight\" or \"no limits tonight\" sets one for that member."));
  const acts = el("div", "acts");
  Object.keys(SESSIONS).forEach(code => button(acts, SESSIONS[code], live && (code !== "clear" || !!all || Object.keys(obj(obj(h.sessions).members)).length > 0), why,
    () => ask(sessionArgs(code, null), "heat " + code)));
  box.appendChild(acts);
  overlays(box, h);
}
function overlays(box, h) {
  box.appendChild(el("h4", "", "Your words"));
  box.appendChild(el("p", "muted", "Files in mind-data/prompts. Only their names and sizes are read here, never their text."));
  arr(obj(h).overlays).forEach(o => box.appendChild(el("p", "why", overlayText(o))));
}
// A member's card in the Personas detail: her resolved tiers and why anything is capped.
export function heatCard(box, h, guid, name, live, why, ask) {
  h = obj(h);
  if (!h.enabled) { return; }
  const m = memberHeat(h, guid);
  if (!m) { return; }
  const section = el("section", "heat");
  section.appendChild(el("h4", "", "Heat"));
  section.appendChild(el("p", "lead", m.title));
  if (!m.applies) { box.appendChild(section); return; }
  m.lines.forEach(t => section.appendChild(el("p", "why", t)));
  m.caps.forEach(t => section.appendChild(el("p", "why", "Capped: " + t + ".")));
  if (m.session) { section.appendChild(el("p", "why", "Tonight: " + m.session + ".")); }
  const ctl = el("div", "ctl");
  const [range, shown] = slider(h.offset_lo, h.offset_hi, m.pinned === null ? m.offset : m.pinned, name + " heat offset", true);
  ctl.appendChild(range); ctl.appendChild(shown);
  const target = String(guid);
  button(ctl, "Preview", live, why, () => ask({ action: "preview", scope: "member", target, dial: "heat", value: Number(range.value) }, name + " heat " + range.value));
  button(ctl, "Set", live, why, () => ask({ action: "set", scope: "member", target, dial: "heat", value: Number(range.value), previewed: false }, name + " heat " + range.value));
  button(ctl, "Clear", live && m.pinned !== null, why, () => ask({ action: "set", scope: "member", target, dial: "heat", value: null, previewed: false }, name + " heat: cleared"));
  section.appendChild(ctl);
  if (m.pinned === null && m.ownDial !== null) { section.appendChild(el("p", "muted", "She keeps the dial you set for her alone (" + m.ownDial + ") until you set an offset here.")); }
  const acts = el("div", "acts");
  Object.keys(SESSIONS).forEach(code => button(acts, SESSIONS[code], live && (code !== "clear" || !!m.session), why,
    () => ask(sessionArgs(code, guid), name + " heat " + code)));
  section.appendChild(acts);
  box.appendChild(section);
}
// What a heat preview adds to the preview card: the row in words, what changes, and the lists
export function heatPreview(box, r) {
  const h = obj(obj(r).heat);
  if (h.explain) { box.appendChild(el("p", "pv", str(h.explain))); }
  if (h.changes) { box.appendChild(el("p", "pv", str(h.changes))); }
  const photos = photoText(h.photos);
  if (photos) { box.appendChild(el("p", "why", photos)); }
  arr(obj(r).members).forEach(m => {
    const d = el("div", "pv");
    d.appendChild(el("h4", "", str(m.name)));
    d.appendChild(el("p", "pv", "before: " + str(obj(m.before).title) + " " + arr(obj(m.before).lines).join(" ")));
    d.appendChild(el("p", "pv", "after: " + str(obj(m.after).title) + " " + arr(obj(m.after).lines).join(" ")));
    arr(obj(m.after).caps).forEach(t => d.appendChild(el("p", "why", "Capped: " + str(t) + ".")));
    box.appendChild(d);
  });
}
