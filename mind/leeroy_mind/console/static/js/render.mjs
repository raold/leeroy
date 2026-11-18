import { $, clear, el, emit, keyGen, on, post } from "./core.mjs";
import { S } from "./state.mjs";
import { confirmLines, outcomeWords, renderModel, renderResult, renderStatus } from "./rendermodel.mjs";

// The top bar's Render selfies (console/RENDER.md): status from the shared stream only.
let value = null, busy = false, keyLost = false, error = '', seen = null, returnTo = null;
let watching = false;  // this page saw the run going (or started it): its end is told once
const button = $('render'), dialog = $('render-confirm');
function render() {
  const model = renderModel(value, {busy, keyLost});
  button.textContent = model.label;
  button.classList.toggle('render-running', model.running);
  button.setAttribute('aria-disabled', String(model.disabled));
  button.setAttribute('aria-busy', String(busy || model.stopping));
  button.title = error || model.title;
}
function finishedAt(v) { const at = v?.last?.finished; return Number.isFinite(at) ? at : null; }
// A run that ended while this page watched is told once; an earlier run's record is not (it
// is in Ops).
function adopt() {
  const next = renderStatus(S.status?.render);
  if (next) {
    const at = finishedAt(next);
    if (next.running) { watching = true; }
    else if (at !== null && at !== seen && watching) { watching = false; emit('note', outcomeWords(next.last)); }
    seen = at ?? seen;
    value = next; keyLost = false; error = '';
  } else { value = null; }
  render();
}
async function send(action) {
  const generation = keyGen();
  busy = true; error = ''; render();
  const answer = await post('/api/selfies/render', {action});
  busy = false;
  if (generation !== keyGen() || keyLost) { render(); return; }
  const next = renderStatus(answer.render);
  if (next) {
    value = next;
    if (next.running) { watching = true; }
    // his Stop's own answer tells the outcome, so the stream does not again
    else if (action === 'render_stop' && finishedAt(next) !== null) { seen = finishedAt(next); watching = false; }
  }
  if (answer.ok && action === 'render' && !next?.running) { watching = true; }
  error = answer.ok ? '' : renderResult(answer, action);
  render(); emit('note', renderResult(answer, action));
}
button.addEventListener('click', () => {
  const model = renderModel(value, {busy, keyLost});
  if (model.disabled) { emit('note', model.why || model.title); return; }
  if (model.action === 'stop') { send('render_stop'); return; }
  returnTo = document.activeElement;
  const words = $('render-confirm-words');
  clear(words);
  confirmLines(value).forEach(line => words.appendChild(el('p', '', line)));
  dialog.showModal();
});
dialog.addEventListener('close', () => { if (returnTo?.isConnected) { returnTo.focus(); } });
$('render-confirm-cancel').addEventListener('click', () => dialog.close());
$('render-confirm-go').addEventListener('click', () => { dialog.close(); send('render'); });
on('snapshot', adopt);
on('status', adopt);
on('key-lost', () => { keyLost = true; render(); });
render();
