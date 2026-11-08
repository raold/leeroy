// Pure, deliberately content-free words for the Ops payload. No generic JSON renderer.
export const PANELS = [
  ["health", "Health"], ["night", "Night"], ["realm", "Realm holds"], ["raids", "Raids"],
  ["selfies", "Selfies and snaps"], ["backups", "Backups"], ["drift", "Release drift"], ["schedules", "Scheduled tasks"]
];
const obj = x => x && typeof x === "object" && !Array.isArray(x) ? x : {};
const arr = x => Array.isArray(x) ? x : [];
const str = x => typeof x === "string" ? x : "unknown";
const n = x => typeof x === "number" && Number.isFinite(x) ? String(x) : "unknown";
export function at(x) {
  return typeof x === "number" && Number.isFinite(x) ? new Date(x * 1000).toLocaleString() : "not recorded";
}
export function panelWords(value) {
  const p = obj(value);
  if (p.status === "missing") { return "Not set up yet: " + str(p.why); }
  if (p.status === "error") { return str(p.why); }
  return p.status === "ok" ? "" : "Not set up yet: this source has not answered.";
}
export function badge(value) {
  const c = obj(obj(value).counts);
  const count = x => typeof x === "number" && Number.isFinite(x) && x >= 0 ? x : null;
  const fails = count(c.fails), held = count(c.held_members);
  return { fails, held, text: (fails === null ? "?" : fails) + " FAIL · " + (held === null ? "?" : held) + " held" };
}
export function lines(name, value) {
  const p = obj(value), out = [];
  if (panelWords(p)) { return [panelWords(p)]; }
  if (name === "health") {
    arr(p.checks).forEach(c => out.push((c.status === "fail" ? "FAIL" : str(c.status)) + " · " + str(c.text)));
  } else if (name === "night") {
    out.push(str(p.night) + " · " + str(p.saved_state) + " · " + (p.running === true ? "running (observed)" : p.running === false ? "not running" : "current activity unknown"));
    out.push("Started " + at(p.started) + " · finished " + at(p.finished));
    out.push("Budget " + n(obj(p.budget).budget_s) + " slot-seconds · estimate " + n(obj(p.budget).estimate_s));
    arr(p.stages).forEach(s => out.push(str(s.stage) + ": " + n(s.units) + " units · " + n(s.failures) + " failures · " + str(s.error_code || "no error code retained") + " · " + at(s.started) + " → " + at(s.finished)));
    out.push("Earliest gap boundary " + at(p.next_after), str(p.next_rule), str(p.running_basis));
  } else if (name === "realm") {
    out.push(n(p.held_members) + " held members · " + arr(p.roots).length + " roots");
    arr(p.roots).forEach(r => out.push(str(r.id) + " · " + str(r.kind) + " · " + str(r.state) + " · " + n(Math.round(r.age_s)) + " seconds old · " + n(r.members) + " members · native " + str(r.native_reason)));
    arr(p.direct_unknowns).forEach(r => out.push("Direct unknown " + str(r.id) + " · " + str(r.kind) + " · " + str(r.reason) + " · " + at(r.at)));
    if (!p.direct_available) { out.push("Not set up yet: the direct ledger is absent."); }
    out.push("Calendar last operation " + at(obj(p.desks).calendar), "Mail last operation " + at(obj(p.desks).mail), str(p.desks_basis), str(p.release_note));
  } else if (name === "raids") {
    ["guild", "offline"].forEach(kind => {
      const r = p[kind];
      if (!r) { out.push("Not set up yet: no " + kind + " raid recorded."); return; }
      out.push(kind + " #" + n(r.id) + " · " + str(r.raid) + " · instance " + n(r.instance) + " · " + str(r.phase));
      out.push(n(r.kills) + " kills · " + n(r.wipes) + " wipes · " + n(r.members) + " members · " + str(r.outcome));
      out.push("Started " + at(r.started) + " · deadline " + at(r.deadline));
      const progress = obj(r.progress);
      out.push(r.progress ? "Progress " + (progress.stale ? "stale" : "recent") + " · observed " + at(progress.observed) + " · " + n(progress.members_in_instance) + " inside · last progress " + at(typeof progress.last_progress_ts === "number" ? progress.last_progress_ts / 1000 : null) + " · boss engaged " + (typeof progress.boss_engaged === "boolean" ? progress.boss_engaged : "unknown") : "Progress not recorded.");
    });
  } else if (name === "selfies") {
    const c = obj(p.counts);
    out.push(n(c.queued) + " queued sets · " + n(c.waiting) + " waiting for a choice · " + n(c.chosen) + " chosen · " + arr(p.held).length + " held sets");
    arr(p.queue).forEach(r => out.push("Queued #" + n(r.guid) + " set " + n(r.set) + " · " + at(r.requested)));
    arr(p.held).forEach(r => out.push("Held #" + n(r.guid) + " set " + n(r.set) + " · " + str(r.reason)));
    arr(p.stages).forEach(s => out.push(str(s.stage) + " · " + n(s.units) + " units · " + at(s.started) + " → " + at(s.finished)));
    if (p.snaps) { const s = obj(p.snaps); out.push("Snaps: " + n(s.planned) + " planned · " + n(s.ready) + " ready · " + n(s.sent_week) + " sent this week · " + n(s.expired_week) + " expired this week"); }
    else { out.push("Not set up yet: snap records are absent."); }
    out.push(p.booth ? "Booth: " + str(p.booth.outcome) + " · " + n(p.booth.sets) + " sets · " + at(p.booth.finished) : "Not set up yet: no photo booth pass recorded.");
  } else if (name === "backups") {
    const s = obj(p.snapshots);
    out.push(n(s.count) + " mind snapshots · " + n(s.bytes) + " bytes");
    Object.entries(obj(s.labels)).forEach(([label, r]) => out.push(label + " · " + at(r.at) + " · " + n(r.bytes) + " bytes"));
    out.push(p.game ? "Game dump " + str(p.game.name) + " · " + at(p.game.at) : "Not set up yet: no game dump set recorded.");
    const off = obj(p.offmachine);
    out.push(panelWords(off) || "Off-machine " + str(off.name) + " · " + at(off.at) + " · manifest " + (off.manifest_verified ? "verified" : "unverified"));
    if (off.verification) { out.push(str(off.verification)); }
  } else if (name === "drift") {
    out.push("Live branch " + str(p.branch), "Live HEAD " + str(p.head), "Cached main " + str(p.main));
    arr(p.prs).forEach(pr => out.push("#" + n(pr.number) + " · " + str(pr.title)));
    if (!arr(p.prs).length) { out.push("No unmatched PR patches in the checked history."); }
  } else if (name === "schedules") {
    arr(p.tasks).forEach(t => out.push(str(t.name) + " · " + (t.installed ? str(t.state) + " · last " + at(t.last_run) + " · result " + n(t.result) : "Not set up yet: task is not installed.")));
  }
  if (p.note) { out.push(str(p.note)); }
  return out;
}
