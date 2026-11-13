// GPU controls use the shared status stream; no browser timer or stored pause state.
export function gpuStatus(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      typeof value.paused !== 'boolean' || typeof value.enabled !== 'boolean' ||
      !(value.since === null || Number.isFinite(value.since) && value.since >= 0) ||
      !(value.refusal === null || typeof value.refusal === 'string')) { return null; }
  return value;
}
export function pausedWords(value, format = t => new Date(t * 1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})) {
  return value?.paused ? 'GPU paused since ' + (Number.isFinite(value.since) ? format(value.since) : 'an earlier session') : '';
}
export function gpuModel(value, {busy=false, keyLost=false, format} = {}) {
  const v = gpuStatus(value), paused = v?.paused === true;
  const retry = paused && (v.unload_failed === true || Array.isArray(v.loaded) && v.loaded.length > 0);
  const action = paused && !retry && !v.mind_down ? 'resume' : 'pause';
  // The refusal is the pause's: Resume is never refused, so its button does not carry it.
  const why = keyLost ? "Open the console URL again to restore this page's key." :
    busy ? 'Waiting for the GPU control to finish.' : !v ? 'GPU status has not answered yet.' :
    !v.enabled ? v.refusal || 'GPU controls are disabled in this read-only console.' :
    action === 'pause' ? v.refusal || '' : '';
  const at = v && Number.isFinite(v.since) ? (format || (t => new Date(t*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})))(v.since) : 'an earlier session';
  return {paused, retry, action,
    label:v?.mind_down ? 'Free GPU' : retry ? 'Retry Free GPU' : paused ? 'GPU free · Resume' : 'Free GPU',
    disabled:busy || keyLost || !v || !v.enabled, why,
    banner:retry ? 'GPU use is paused, but models may still be loaded. Choose Retry Free GPU.' :
      paused ? 'GPU freed at '+at+'. Members are resting until you resume, log into WoW or post in Discord.' : ''};
}
export function gpuResult(value) {
  if (!value?.ok && value?.error === 'gpu_resumed' && value.message) { return value.message; }
  if (!value?.ok) { return value?.refusal || value?.message ||'The console did not confirm the GPU change. Check its state before trying again.'; }
  if (typeof value.message === 'string' && value.message) { return value.message; }
  if (value.mind_down) { return 'The mind is stopped; unloaded '+(Array.isArray(value.unloaded) ? value.unloaded.length : 0)+' models.'; }
  if (value.paused && Number.isFinite(value.freed_bytes) && value.freed_bytes >= 0) {
    return 'Freed '+(value.freed_bytes / 1073741824).toFixed(1)+' GB';
  }
  return value.paused ? 'GPU freed. Members are resting.' : 'Members are resuming.';
}
