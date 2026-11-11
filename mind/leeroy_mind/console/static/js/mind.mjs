import { $, api, arr, clear, el, emit, hhmm, obj, on, post, refused, setText, str } from "./core.mjs";
import { S } from "./state.mjs";
import { gameWords, mindClass, setPill } from "./strip.mjs";
import { registerTab } from "./tabs.mjs";
import { serverRows } from "./gameserver.mjs";

// -- the mind tab ---------------------------------------------------------------------------------

var HEALTH_MS = 5000;
var healthTimer = null, healthBusy = false, lastHealth = null;

function bytes(n) {
  if (typeof n !== "number") { return "-"; }
  return n >= 1e9 ? (n / 1e9).toFixed(1) + " GB" : (n / 1e6).toFixed(0) + " MB";
}
function serviceButtons(svc, can) {
  var buttons = document.querySelectorAll('button[data-svc="' + svc + '"]');
  for (var i = 0; i < buttons.length; i++) { buttons[i].disabled = !can[buttons[i].dataset.act]; }
}
function renderMind() {
  var m = S.status.mind, c = obj(S.status.control), svc = obj(c.services), busy = c.busy;
  setPill("m-state", mindClass(m.state), m.state, "");
  setText("m-proc", m.pid ? "pid " + m.pid + (arr(m.pids).length > 1 ? " (launcher " + m.pids[m.pids.length - 1] + ")" : "") : "none");
  setText("m-lock", m.lock ? "held" : "free");
  var why = svc.mind || (busy ? busy[1] + " of the " + busy[0] + " is running" : "") || m.detail || "";
  var wm = $("m-why");
  wm.textContent = why;
  wm.classList.toggle("bad", m.state === "crashed" || m.state === "unknown");
  var ok = !svc.mind && !busy;
  serviceButtons("mind", { start: ok && (m.state === "stopped" || m.state === "crashed"),
                           restart: ok && m.state === "running",
                           stop: ok && (m.state === "running" || m.state === "starting") });
  var v = S.status.voice, hv = lastHealth ? obj(lastHealth.voice) : {};
  var up = v.service || hv.lock === true;
  setPill("w-state", up ? "ok" : "", up ? "running" : "stopped", "");
  setText("w-proc", hv.pid ? "pid " + hv.pid : up ? "running" : "none");
  setText("w-ports", hv.ui_port ? "page " + hv.ui_port + ", mind " + hv.mind_port : "-");
  setText("w-link", !v.service ? "-" : v.link ? "the mind is connected" : "the mind is not connected");
  var vok = !svc.voice && !busy;
  $("w-why").textContent = svc.voice || hv.detail || "";
  serviceButtons("voice", { start: vok && !up, restart: vok && up, stop: vok && up });
  setText("h-bridge", m.state !== "running" ? "- (the mind is not running)" : S.status.bridge === true ? "connected" : S.status.bridge === false ? "down" : "unknown");
  setText("h-vlink", !v.enabled ? "voice off" : !v.service ? "voice service down" : v.link ? "linked" : "not linked");
  setText("h-game", gameWords().replace(/^you: /, "") + (S.status.game.reason ? " (" + S.status.game.reason + ")" : ""));
}
function renderLog(id, records) {
  var box = $(id);
  clear(box);
  if (!records.length) { box.appendChild(el("div", "muted", "None.")); return; }
  records.forEach(function (r) {
    var f = obj(r.fields), parts = Object.keys(f).map(function (k) { return k + "=" + f[k]; });
    var ts = str(r.ts).replace(/^\d{4}-\d\d-\d\dT/, "").replace(/\.\d+([+-].*|Z)?$/, "");
    var line = el("div", "r " + str(r.level), ts + " " + str(r.level).toLowerCase() + " " + str(r.event)
                  + (parts.length ? " " + parts.join(" ") : "") + (r.exc ? " (" + r.exc + ")" : ""));
    box.appendChild(line);
  });
  box.scrollTop = box.scrollHeight;
}
function renderServer(sv) {
  var r = serverRows(sv);
  ["world", "auth", "bridge"].forEach(function (k) {
    setText("gs-" + k, r[k]);
    $("gs-" + k + "-dot").className = "dot" + (sv[k] === true ? " ok" : sv[k] === false ? " bad" : "");
  });
  setText("gs-server", r.server);
  setText("gs-players", r.players);
  setText("gs-why", r.why);
}
function renderHealth(h) {
  lastHealth = h;
  setText("m-data", str(obj(h.mind).data_dir) || "-");
  var ep = obj(h.endpoint);
  setText("m-stop", ep.ok ? "clean: the mind's own shutdown, through its control endpoint"
          : "by force" + (ep.detail ? " (" + str(ep.detail) + ")" : ""));
  var night = h.night;
  setText("h-night", night ? night.night + ": " + night.status + (night.finished ? ", finished " + hhmm(night.finished) : "") : "no night run yet");
  renderServer(obj(h.server));
  var cfg = $("h-config");
  cfg.textContent = str(h.config);
  cfg.hidden = !h.config;
  var o = obj(h.ollama), rows = $("h-models");
  clear(rows);
  setText("h-ollama", o.ok ? arr(o.models).length + " resident at " + str(o.url) : str(o.error));
  arr(o.models).forEach(function (mdl) {
    var tr = el("tr");
    [str(mdl.name), str(mdl.role) || "-", bytes(mdl.size), bytes(mdl.vram),
     mdl.expires ? (/^0001-/.test(mdl.expires) ? "pinned" : str(mdl.expires).replace("T", " ").slice(0, 16)) : "-"]
      .forEach(function (cell) { tr.appendChild(el("td", "", cell)); });
    rows.appendChild(tr);
  });
  if (o.ok && !arr(o.models).length) {
    var tr = el("tr"), td = el("td", "muted", "No model is loaded.");
    td.colSpan = 5;
    tr.appendChild(td);
    rows.appendChild(tr);
  }
  var g = obj(h.gpu), box = $("h-gpu");
  clear(box);
  box.className = g.ok ? "" : "muted";
  if (!g.ok) { box.textContent = str(g.error) || "-"; }
  arr(g.gpus).forEach(function (gpu) {
    box.appendChild(el("div", "", str(gpu.name) + ": " + (gpu.free_mb / 1024).toFixed(1) + " GB free of "
                       + (gpu.total_mb / 1024).toFixed(1) + " GB, " + gpu.util + "% busy"));
    var bar = el("div", "meter"), fill = el("i");
    fill.style.width = Math.min(100, Math.round(100 * gpu.used_mb / Math.max(1, gpu.total_mb))) + "%";
    bar.appendChild(fill);
    box.appendChild(bar);
  });
  renderLog("h-errors", arr(h.errors));
  renderLog("h-log", arr(h.log));
  if (h.strip) { S.status = h.strip; emit("status", h.strip); }
  renderMind();
}
function pollHealth() {
  if (healthBusy || S.tab !== "mind") { return; }
  healthBusy = true;
  api("/api/health", { poll: true }).then(function (r) {
    if (refused(r) || !r.ok) { return null; }
    return r.json();
  }).then(function (h) { if (h) { renderHealth(h); } }, function () { /* the strip says it */ })
    .then(function () { healthBusy = false; });
}

var pending = null, confirmReturn = null;
function closeConfirm() {
  if ($("confirm").hidden) { return; }
  $("confirm").hidden = true; pending = null;
  document.querySelector(".top").inert = false;
  document.querySelector("main").inert = false;
  if (confirmReturn && confirmReturn.isConnected) { confirmReturn.focus(); }
  confirmReturn = null;
}
export function askConfirm(text, yes, then, opener) {
  confirmReturn = opener || document.activeElement;
  pending = then;
  $("confirm-text").textContent = text;
  $("confirm-yes").textContent = yes;
  $("confirm").hidden = false;
  document.querySelector(".top").inert = true;
  document.querySelector("main").inert = true;
  $("confirm-no").focus();
}
function act(svc, action, confirm) {
  var opener = document.activeElement;
  var why = $(svc === "mind" ? "m-why" : "w-why");
  why.textContent = action + " requested…";
  why.classList.remove("bad");
  serviceButtons(svc, {});
  post("/api/control", { service: svc, action: action, confirm: !!confirm }).then(function (j) {
    if (j.confirm) {
      askConfirm(j.confirm, action.charAt(0).toUpperCase() + action.slice(1),
                 function () { act(svc, action, true); }, opener);
      why.textContent = "";
    } else {
      why.textContent = j.ok ? j.message : (j.error || "it did not work");
      why.classList.toggle("bad", !j.ok);
    }
    renderMind();
    pollHealth();
  }, function () { why.textContent = "The console is not reachable."; why.classList.add("bad"); });
}
$("tab-mind").addEventListener("click", function (ev) {
  var b = ev.target.closest("button[data-svc]");
  if (b && !b.disabled) { act(b.dataset.svc, b.dataset.act, false); }
});
$("confirm-no").addEventListener("click", closeConfirm);
$("confirm").addEventListener("keydown", function (ev) {
  if (ev.key !== "Tab") { return; }
  ev.preventDefault();
  (document.activeElement === $("confirm-no") ? $("confirm-yes") : $("confirm-no")).focus();
});
$("confirm-yes").addEventListener("click", function () {
  var p = pending;
  closeConfirm();
  if (typeof p === "function") { p(); }
});
document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") { closeConfirm(); } });

on("status", function () { if (S.tab === "mind") { renderMind(); } });
on("snapshot", function () { if (S.tab === "mind") { renderMind(); } });
on("control", function (d) {
  var words = d.service + " " + d.action;
  if (d.phase === "running") { emit("note", words + ": requested"); }
  else if (d.phase === "done") { emit("note", words + ": " + str(d.message)); }
  else { emit("note", words + " failed: " + str(d.message)); }
  if (S.tab === "mind") { pollHealth(); }
});
registerTab("mind", {
  show: function () { renderMind(); pollHealth(); healthTimer = setInterval(pollHealth, HEALTH_MS); },
  hide: function () { clearInterval(healthTimer); healthTimer = null; }
});
