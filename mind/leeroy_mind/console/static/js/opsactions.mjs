import { $, api, el, post, refused } from "./core.mjs";
import { S } from "./state.mjs";
import { actionWords, confirmationWords } from "./opsactionmodel.mjs";

let current = null, preview = null, sending = false, statusBusy = false, epoch = 0;
let enabled = false, running = false;
function message(words) { $("ops-action-message").textContent = words; }
function controls() {
  document.querySelectorAll("[data-ops-action]").forEach(b => { b.disabled = !enabled || running || sending; });
}
function reset() {
  epoch++;
  current = null; preview = null;
  $("ops-action-form").reset(); $("ops-action-confirm").hidden = true;
  $("ops-action-preview").hidden = false; $("ops-action-typed-wrap").hidden = true;
  $("ops-action-words").textContent = "";
  $("ops-action-reason").disabled = false; $("ops-action-label").disabled = false;
  $("ops-action-typed").disabled = false;
  $("ops-action-preview").disabled = false; $("ops-action-confirm").disabled = false;
}
export function actionButton(label, action, id) {
  const b = el("button", "btn", label); b.type = "button"; b.dataset.opsAction = action;
  b.disabled = !enabled || running || sending;
  b.addEventListener("click", () => {
    reset(); current = { action, ...(id ? { id } : {}) };
    $("ops-action-title").textContent = label;
    $("ops-action-reason-wrap").hidden = action !== "release";
    $("ops-action-label-wrap").hidden = action !== "snapshot";
    $("ops-action-dialog").showModal();
  });
  return b;
}
export function refreshActions() { return refresh(); }
async function refresh() {
  if (statusBusy || S.tab !== "ops" || document.hidden) { return; }
  statusBusy = true;
  try {
    const r = await api("/api/ops/action", { poll: true });
    if (refused(r)) { return; }
    const value = await r.json();
    if (r.status !== 200) { message(value.error || "Action status is unavailable."); return; }
    enabled = value.enabled === true; running = value.operation?.state === "running";
    message(actionWords(value)); controls();
  } catch (_) { message("Action status is unavailable. Check it before retrying."); }
  finally { statusBusy = false; }
}
async function send(confirm) {
  if (!current || sending) { return; }
  const requestEpoch = epoch, action = { ...current };
  sending = true; controls();
  $("ops-action-preview").disabled = true; $("ops-action-confirm").disabled = true;
  try {
    let body;
    if (confirm) {
      body = { ...preview.args, phase: "confirm", token: preview.token };
      if (action.action === "release") { body.typed_id = $("ops-action-typed").value; }
    } else {
      body = { ...action, phase: "preview" };
      if (action.action === "release") { body.reason = $("ops-action-reason").value; }
      if (action.action === "snapshot") { body.label = $("ops-action-label").value; }
    }
    $("ops-action-reason").disabled = true; $("ops-action-label").disabled = true;
    $("ops-action-typed").disabled = true;
    const answer = await post("/api/ops/action", body);
    if (requestEpoch !== epoch) { if (confirm) { refreshActions(); } return; }
    if (!answer.ok) {
      if (confirm) {
        // Refusal may consume the token. Keep the draft, but require a fresh review.
        preview = null;
        $("ops-action-preview").hidden = false; $("ops-action-confirm").hidden = true;
        $("ops-action-typed-wrap").hidden = true;
      }
      $("ops-action-words").textContent = answer.error || "The action was refused.";
      return;
    }
    if (!confirm) {
      const { phase, ...args } = body;
      preview = { args, token: answer.token };
      $("ops-action-words").textContent = confirmationWords(action.action, answer.details);
      $("ops-action-typed-wrap").hidden = action.action !== "release";
      $("ops-action-preview").hidden = true; $("ops-action-confirm").hidden = false;
      $("ops-action-reason").disabled = true; $("ops-action-label").disabled = true;
      $("ops-action-typed").disabled = false;
      (action.action === "release" ? $("ops-action-typed") : $("ops-action-confirm")).focus();
    } else {
      running = true; $("ops-action-dialog").close();
      message("The action has started; its outcome will appear here.");
      await refreshActions();
    }
  } catch (_) {
    if (requestEpoch === epoch) { $("ops-action-words").textContent = "No answer arrived. Check action status before retrying."; }
  }
  finally {
    sending = false; controls();
    if (requestEpoch === epoch) {
      $("ops-action-preview").disabled = false; $("ops-action-confirm").disabled = false;
      $("ops-action-reason").disabled = !!preview; $("ops-action-label").disabled = !!preview;
      $("ops-action-typed").disabled = false;
      if (confirm && !preview) { $("ops-action-preview").focus(); }
    }
  }
}
$("ops-action-global").append(actionButton("Render now", "render"), actionButton("Take a snapshot", "snapshot"));
$("ops-action-preview").addEventListener("click", () => send(false));
$("ops-action-confirm").addEventListener("click", () => send(true));
$("ops-action-cancel").addEventListener("click", () => $("ops-action-dialog").close());
$("ops-action-form").addEventListener("submit", e => e.preventDefault());
$("ops-action-dialog").addEventListener("close", reset);
$("ops-action-dialog").addEventListener("keydown", event => {
  if (event.key !== "Tab") { return; }
  const fields = Array.from($("ops-action-dialog").querySelectorAll("button, input"))
    .filter(e => !e.disabled && e.getClientRects().length);
  const first = fields[0], last = fields[fields.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});
setInterval(refreshActions, 2000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) { refreshActions(); } });
