import { $, emit, keyGen, on, post } from "./core.mjs";
import { S } from "./state.mjs";
import { gpuModel, gpuResult, gpuStatus } from "./gpumodel.mjs";

let value = null, busy = false, keyLost = false, error = '';
const button = $('gpu'), banner = $('gpu-banner');
function render() {
  const model = gpuModel(value,{busy,keyLost});
  button.textContent = model.label;
  button.classList.toggle('gpu-paused',model.paused);
  button.setAttribute('aria-disabled',String(model.disabled));
  button.setAttribute('aria-busy',String(busy));
  button.title = error || model.why || (model.paused ? 'Resume members and model use.' : 'Pause members and unload the models to free the GPU.');
  banner.textContent = model.banner;
  banner.hidden = !model.banner;
}
function adopt() {
  const next = gpuStatus(S.status.gpu);
  if (next) { value = next; keyLost = false; error = ''; }
  else { value = null; }
  render();
}
button.addEventListener('click',async () => {
  const model = gpuModel(value,{busy,keyLost});
  if (model.disabled) { emit('note',model.why); return; }
  const generation = keyGen();
  busy = true; error = ''; render();
  const answer = await post('/api/gpu',{action:model.action});
  busy = false;
  if (generation !== keyGen() || keyLost) { render(); return; }
  const next = gpuStatus(answer);
  if (next) { value = next; }
  error = answer.ok ? '' : gpuResult(answer);
  render(); emit('note',gpuResult(answer));
});
on('snapshot',adopt);
on('status',adopt);
on('key-lost',() => { keyLost=true; render(); });
render();
