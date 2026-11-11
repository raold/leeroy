import { $, api, clear, el, refused } from "./core.mjs";
import { pollTab, renderKpis } from "./tabkit.mjs";
import { PANELS, at, badge, lines, panelWords } from "./opsmodel.mjs";
import { actionButton, refreshActions } from "./opsactions.mjs";

// The tab badge shows only when something needs a look (a health FAIL or a held member).
function setBadge(value) {
  const count = badge(value), tab = $("t-ops");
  let mark = tab.querySelector(".badge");
  if (!count.show) {
    if (mark) { mark.remove(); }
    tab.removeAttribute("aria-label");
    return;
  }
  if (!mark) { mark = el("span", "badge"); tab.appendChild(mark); }
  mark.textContent = count.text;
  tab.setAttribute("aria-label", "Ops: " + count.text);
}

function renderOps(value) {
  refreshActions();
  const count = badge(value), panels = value.panels || {};
  setBadge(value);
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
    lines(name, p).forEach(line => card.appendChild(el("p", p.status === "error" || line.startsWith("FAIL · ") ? "ops-error" : "ops-line", line)));
    if (name === "realm" && !panelWords(p)) {
      (p.roots || []).forEach(r => {
        if (r.release_command) {
          const label = el("label", "ops-command", "Command for " + r.id);
          const field = el("input", "");
          field.type = "text"; field.readOnly = true; field.value = r.release_command;
          field.addEventListener("focus", () => field.select());
          label.appendChild(field); card.appendChild(label);
          if (r.state === "unknown") { card.appendChild(actionButton("Review release", "release", r.id)); }
        }
      });
    }
    if (name === "raids" && p.offline?.command_id && !["complete", "rejected"].includes(p.offline.phase)) {
      card.appendChild(actionButton("Review offline raid stop", "offline_stop", p.offline.command_id));
    }
    box.appendChild(card);
  });
}

pollTab("ops", { path: "/api/ops", ms: 15000, render: renderOps });

// The badge is kept current whichever tab is open: one light request a minute while the page is visible.
let badgeBusy = false;
function pollBadge() {
  if (badgeBusy || document.hidden) { return; }
  badgeBusy = true;
  api("/api/ops/badge", { poll: true }).then(r => {
    if (refused(r) || r.status !== 200) { return null; }
    return r.json();
  }).then(j => { if (j) { setBadge(j); } })
    .catch(() => { /* no connection: the strip says it */ })
    .then(() => { badgeBusy = false; });
}
setInterval(pollBadge, 60000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) { pollBadge(); } });
setTimeout(pollBadge, 1500);  // after main.mjs has set the key
