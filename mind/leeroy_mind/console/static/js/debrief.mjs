import { el } from "./core.mjs";
import { overview, bossTitle, bossLines, unassignedLines, offlineLines } from "./debriefmodel.mjs";
const open = new Set();
export function renderDebrief(card, p) {
  card.classList.add("raid-debrief");
  overview(p).forEach(s => card.appendChild(el("p", "note", s)));
  const detail = (key, title, lines) => {
    const box = el("details", ""), id = String(p.raid?.id) + ":" + key;
    box.open = open.has(id);
    box.appendChild(el("summary", "", title));
    lines.forEach(s => box.appendChild(el("p", "ops-line", s)));
    box.addEventListener("toggle", () => { if (box.isConnected) { box.open ? open.add(id) : open.delete(id); } });
    card.appendChild(box);
  };
  (p.bosses || []).forEach(b => detail(b.map + ":" + b.boss_id + ":" + b.difficulty, bossTitle(b), bossLines(b)));
  detail("unassigned", "Unassigned group resurrection, hazard and responder evidence", unassignedLines(p));
  const offline = offlineLines(p);
  detail("offline", "Offline outcomes and stops", offline.length ? offline : ["No bound offline log record found. See the saved raid outcome above; missing logs are not proof of a successful run."]);
}
