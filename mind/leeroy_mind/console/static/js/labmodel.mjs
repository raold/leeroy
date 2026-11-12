export const REASONS = ['funny','in-character','boring','repetitive','weird','too-much','wrong-facts'];
export const WEIGHTS = ['explicit','engagement','emote','quality'];

export function controlReason(status) {
  if (!status?.control?.enabled) { return 'Control is off. These controls are read-only.'; }
  if (status.control.busy) { return 'Another control action is running.'; }
  if (status.mind?.state !== 'running') { return 'The mind must be running to save changes.'; }
  return '';
}
export function weightValues(values) {
  if (!values || WEIGHTS.some(k => typeof values[k] !== 'number' || !Number.isFinite(values[k]) || values[k]<0 || values[k]>1)
      || Math.abs(WEIGHTS.reduce((sum,k) => sum+values[k],0)-1)>1e-9) { return null; }
  return Object.fromEntries(WEIGHTS.map(k => [k,values[k]]));
}
export function vote(value, reasons) {
  return {value: value === 1 || value === -1 ? value : null,
    reasons: REASONS.filter(r => Array.isArray(reasons) && reasons.includes(r))};
}
export function reconcileVote(current, incoming) {
  const value={...vote(incoming?.value,incoming?.reasons), revision: Number.isSafeInteger(incoming?.revision) ? incoming.revision : 0};
  return !current || value.revision >= (current.revision || 0) ? value : current;
}
export function score(value) { return Number.isFinite(value) ? value.toFixed(3) : 'Not scored yet'; }
export function percent(value) { return Number.isFinite(value) ? (100*value).toFixed(1)+'%' : '—'; }
