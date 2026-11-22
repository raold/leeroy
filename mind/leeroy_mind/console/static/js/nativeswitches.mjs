import { el } from "./core.mjs";

const expanded = new Set();
export function renderNativeSwitches(card, value) {
  for (const source of value?.sources || []) {
    const detail = el("details", "native-switches"), path = String(source.source);
    const rows = source.rows || [];
    detail.open = expanded.has(path);
    detail.appendChild(el("summary", "", path.split("/").pop()+" · "+source.status+" · "+rows.length+" switches"));
    detail.appendChild(el("p", "ops-line", "Source: "+path));
    for (const row of rows) {
      detail.appendChild(el("p", "ops-line", row.key+" · configured "+row.configured+" · running state unknown"));
    }
    detail.addEventListener("toggle", () => {
      if (detail.open) { expanded.add(path); } else { expanded.delete(path); }
    });
    card.appendChild(detail);
  }
}
