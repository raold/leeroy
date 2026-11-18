// Render selfies (console/RENDER.md) rides the shared status stream, as the GPU control does: no
// browser timer and no state kept in the page. Pure: node tests it.
const count = (n) => Number.isInteger(n) && n >= 0;
const plural = (n, one, many) => n + ' ' + (n === 1 ? one : many);

export function renderStatus(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      typeof value.enabled !== 'boolean' || typeof value.running !== 'boolean' ||
      typeof value.selfies !== 'boolean' ||
      !(value.refusal === null || value.refusal === undefined || typeof value.refusal === 'string')) { return null; }
  return value;
}

// "3 first sets, 6 sets of four more, 2 situational photos"
export function kindsWords(k) {
  const parts = [];
  if (count(k?.first) && k.first) { parts.push(plural(k.first, 'first set', 'first sets')); }
  if (count(k?.more) && k.more) { parts.push(plural(k.more, 'set of four more', 'sets of four more')); }
  if (count(k?.snaps) && k.snaps) { parts.push(plural(k.snaps, 'situational photo', 'situational photos')); }
  return parts.join(', ');
}

export function durationWords(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) { return ''; }
  const minutes = Math.max(1, Math.round(seconds / 60));
  if (minutes < 90) { return plural(minutes, 'minute', 'minutes'); }
  return (Math.round(minutes / 6) / 10) + ' hours';
}

export function renderModel(value, {busy=false, keyLost=false} = {}) {
  const v = renderStatus(value);
  const running = v?.running === true, stopping = running && v.stopping === true;
  const p = v?.progress && typeof v.progress === 'object' ? v.progress : {};
  const total = Math.max(1, count(p.total) ? p.total : 0), done = count(p.done) ? p.done : 0;
  const backlog = v?.backlog && count(v.backlog.total) ? v.backlog : null;
  const label = stopping ? 'Stopping…' :
    running ? 'Rendering ' + Math.min(done + 1, total) + ' of ' + total + ' · Stop' :
    backlog && backlog.total && v.selfies ? 'Render selfies (' + backlog.total + ')' : 'Render selfies';
  const why = keyLost ? "Open the console URL again to restore this page's key." :
    busy ? 'Waiting for the render control to finish.' :
    !v ? 'Render status has not answered yet.' :
    !v.enabled ? v.refusal || 'Render selfies is disabled in this read-only console.' :
    running ? (stopping ? 'The photo worker is shutting down.' : '') :
    v.refusal || '';
  // A run can always be stopped (until it is stopping); a new one only without a refusal.
  const disabled = busy || keyLost || !v || !v.enabled || (running ? stopping : !!v.refusal);
  return {action: running ? 'stop' : 'render', running, stopping, label, why, disabled,
          title: why || (running ? 'Stop rendering: a photo mid-render stays queued.' :
            'Render the queued selfies now: ' + (backlog ? kindsWords(backlog) : 'the backlog') + '.')};
}

// The confirm dialog: what renders, a rough time, and what stops it.
export function confirmLines(value) {
  const v = renderStatus(value), b = v?.backlog && count(v.backlog.total) ? v.backlog : null;
  const lines = [];
  lines.push(b && b.total ? 'Renders ' + b.total + ' now: ' + kindsWords(b) + '.' :
    'Renders everything queued now.');
  if (b && count(b.held) && b.held) {
    lines.push(plural(b.held, 'held set stays', 'held sets stay') + ' queued until the night can render it.');
  }
  if (b && Number.isFinite(b.estimate_s)) {
    lines.push('Rough time: about ' + durationWords(b.estimate_s) + (b.estimate_measured === true
      ? ', from earlier renders.' : ' (a first guess: no render has been timed yet).'));
  }
  lines.push('It stops if you log into WoW or post in Discord; an offline raid, the voice service '
    + 'or Free GPU stop it too. Whatever is not rendered stays queued.');
  return lines;
}

const BY = {login: 'your WoW login', discord: 'your Discord post', offline_raid: 'an offline raid',
  voice: 'the voice service', gpu_pause: 'Free GPU', shutdown: 'the mind stopping',
  night: 'the night', booth: 'the photo booth', lease: 'another GPU job'};

// The toast (and the Ops line) for a finished run's record.
export function outcomeWords(last) {
  if (!last || typeof last !== 'object' || Array.isArray(last)) { return ''; }
  const made = [];
  if (count(last.sets) && last.sets) { made.push(plural(last.sets, 'set', 'sets')); }
  if (count(last.snaps) && last.snaps) { made.push(plural(last.snaps, 'photo', 'photos')); }
  const left = count(last.total) && count(last.done) ? last.total - last.done : 0;
  const counts = (made.length ? made.join(' and ') + ' rendered' : 'nothing rendered')
    + (count(last.failed) && last.failed ? ', ' + last.failed + ' failed' : '')
    + (count(last.held) && last.held ? ', ' + last.held + ' held' : '');
  // A run that ended early (a worker that would not start) says what it never reached.
  const unreached = left > 0 ? ', ' + left + ' not reached and still queued' : '';
  const time = Number.isFinite(last.seconds) ? ' in ' + durationWords(last.seconds) : '';
  if (last.outcome === 'done') { return 'Selfie render done' + time + ': ' + counts + unreached + '.'; }
  if (last.outcome === 'stopped') { return 'Selfie render stopped: ' + counts + '. The rest stays queued.'; }
  if (last.outcome === 'preempted') {
    return 'Selfie render stopped by ' + (BY[last.by] || BY.lease) + ': ' + counts + '. The rest stays queued.';
  }
  return 'Selfie render failed: ' + counts + unreached + '.';
}

// The toast for his own click's answer.
export function renderResult(answer, action) {
  if (!answer?.ok) {
    return answer?.message || answer?.error || 'The console did not confirm the render action. Check its state before trying again.';
  }
  if (action === 'render_stop') {
    return answer.pending ? answer.message || 'Stopping.' : outcomeWords(answer.render?.last) || 'Selfie render stopped.';
  }
  const p = answer.render?.progress;
  return p && count(p.total) ? 'Rendering ' + p.total + ' now: ' + kindsWords(p) + '.' : 'Rendering the queued selfies.';
}
