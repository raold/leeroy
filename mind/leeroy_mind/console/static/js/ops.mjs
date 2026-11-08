import { $, clear, el } from "./core.mjs";
import { pollTab, renderKpis } from "./tabkit.mjs";
import { PANELS, at, badge, lines, panelWords } from "./opsmodel.mjs";

function renderOps(value) {
  const count = badge(value), tab = $("t-ops"), panels = value.panels || {};
  let mark = tab.querySelector(".badge");
  if (!mark) { mark = el("span", "badge"); tab.appendChild(mark); }
  mark.textContent = count.text;
  tab.setAttribute("aria-label", "Ops: " + count.text);
  renderKpis($("ops-kpis"), [
    { k: "Health FAILs", v: count.fails === null ? "unknown" : String(count.fails), d: "Health refreshes at most once a minute" },
    { k: "Held members", v: count.held === null ? "unknown" : String(count.held), d: "Unique members held by Realm roots" }
  ]);
  const box = $("ops-panels");
  clear(box);
  PANELS.forEach(([name, title]) => {
    const p = panels[name] || {}, card = el("article", "panel ops-panel");
    card.appendChild(el("h3", "", title));
    card.appendChild(el("p", "note", "Checked " + at(p.checked_at) + " · budget " + (p.budget_s || "unknown") + " s"));
    lines(name, p).forEach(line => card.appendChild(el("p", p.status === "error" ? "ops-error" : "ops-line", line)));
    if (name === "realm" && !panelWords(p)) {
      (p.roots || []).forEach(r => {
        if (r.release_command) {
          const label = el("label", "ops-command", "Command for " + r.id);
          const field = el("input", "");
          field.type = "text"; field.readOnly = true; field.value = r.release_command;
          field.addEventListener("focus", () => field.select());
          label.appendChild(field); card.appendChild(label);
        }
      });
    }
    box.appendChild(card);
  });
}

pollTab("ops", { path: "/api/ops", ms: 15000, render: renderOps });
