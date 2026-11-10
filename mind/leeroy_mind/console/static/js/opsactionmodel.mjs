export function actionWords(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) { return "Action status is unavailable."; }
  if (!value.enabled) { return typeof value.reason === "string" ? value.reason : "Ops actions are off."; }
  const op = value.operation;
  if (!op) { return "Actions require a preview and confirmation. The owner must be offline."; }
  return typeof op.message === "string" ? op.message : "Check the action's state before retrying.";
}
export function confirmationWords(action, details) {
  if (action !== "release") { return "Confirm this one action. A stop is complete only when its terminal outcome arrives."; }
  const d = details || {};
  return "Release " + d.id + " · " + d.kind + " · " + Math.round(d.age_s || 0) + " seconds old · " +
    d.members + " members · native " + d.native_reason + ". The mind stops cleanly, releases this root and restarts. " +
    "Native keeps its own body fences. Contradictory evidence can hold this root again.";
}
