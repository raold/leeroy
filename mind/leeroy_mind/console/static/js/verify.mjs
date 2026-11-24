import { el } from "./core.mjs";
import { summary, grouped, featureTitle, featureLines } from "./verifymodel.mjs";

const expanded = new Set();
export function renderVerify(card, p) {
  card.classList.add("verify-ledger");
  summary(p).forEach(s => card.appendChild(el("p", "note", s)));
  grouped(p).forEach(f => {
    const box = el("details", "verify-" + (f.status === "failed" ? "failed" : "feature")), id = String(f.id);
    box.open = expanded.has(id);
    box.appendChild(el("summary", "", featureTitle(f)));
    featureLines(f).forEach(s => box.appendChild(el("p", "ops-line", s)));
    box.addEventListener("toggle", () => { if (box.isConnected) { box.open ? expanded.add(id) : expanded.delete(id); } });
    card.appendChild(box);
  });
}
