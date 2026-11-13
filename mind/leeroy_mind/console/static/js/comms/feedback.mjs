import { el, on, post } from "../core.mjs";
import { S } from "../state.mjs";
import { BoundedMap } from "../resilience.mjs";
import { controlReason, REASONS, vote, reconcileVote } from "../labmodel.mjs";

// UI receipts bridge an older feed snapshot until the next stored snapshot arrives.
// Keys are chat_line IDs, never Discord message IDs. No text or votes enter localStorage.
const receipts = new BoundedMap(1000), pending = new Set(), drafts = new BoundedMap(1000);
function draw(box) {
  const id = Number(box.dataset.feedback), saved = receipts.get(id) || vote(null,[]);
  const reason = controlReason(S.status), busy = pending.has(id);
  box.querySelectorAll('button,input').forEach(b => { b.disabled = !!reason || busy; });
  box.querySelectorAll('[data-vote]').forEach(b => b.setAttribute('aria-pressed',String(saved.value === Number(b.dataset.vote))));
  box.querySelectorAll('[data-reason]').forEach(b => { b.checked = (drafts.get(id) || saved.reasons).includes(b.dataset.reason); });
  // Why the controls are off goes on the controls, not under every line (200 copies a view)
  box.title = reason || '';
  box.querySelector('.feedback-status').textContent = busy ? 'Saving feedback…' :
    (!saved.value && drafts.has(id) ? 'Choose thumbs up or down to save these reasons.' : saved.message || '');
}
function update(id) {
  document.querySelectorAll('[data-feedback="'+id+'"]').forEach(draw);
}
function receive(id,incoming) {
  const previous=receipts.get(id);
  let saved=reconcileVote(previous,incoming);
  if (!pending.has(id) && previous && saved.revision > previous.revision) { drafts.delete(id); }
  // The same revision echoed by the event stream or a redraw keeps the save's message
  if (previous && previous.message && saved.revision === previous.revision) { saved={...saved,message:previous.message}; }
  receipts.set(id,saved);
  return saved;
}
async function send(id,value,reasons) {
  if (pending.has(id) || controlReason(S.status)) { return; }
  pending.add(id); drafts.set(id,reasons); update(id);
  const reply = await post('/api/feedback',{line_id:id,value,reasons});
  if (reply.ok) {
    // A newer SSE receipt may arrive before this older HTTP acknowledgement.
    const saved = receive(id,reply);
    receipts.set(id,{...saved,message:saved.revision > reply.revision ?
      'Newer feedback was saved in another console.' : 'Feedback saved.'});
  }
  else { receipts.set(id,{...receipts.get(id),message:reply.error || 'Feedback was not saved.'}); }
  pending.delete(id); drafts.delete(id); update(id);
}
export function feedbackButtons(line,author) {
  const id = line.feedback_id;
  if (!Number.isSafeInteger(id) || id<=0) { return null; }
  receive(id,line.feedback);
  const box = el('div','line-feedback'); box.dataset.feedback = String(id);
  box.setAttribute('role','group'); box.setAttribute('aria-label','Feedback on '+author+'’s line '+id);
  [1,-1].forEach(value => {
    const b = el('button','',value===1 ? '👍' : '👎'); b.type='button'; b.dataset.vote=String(value);
    b.dataset.focus='feedback-'+id+'-'+value;
    b.setAttribute('aria-label',(value===1 ? 'Thumbs up' : 'Thumbs down')+' on '+author+'’s line '+id);
    b.addEventListener('click',() => send(id,value,drafts.get(id) || (receipts.get(id) || vote(null,[])).reasons)); box.appendChild(b);
  });
  const detail = el('details','feedback-reasons'), summary=el('summary','','Reasons (optional)');
  detail.appendChild(summary); const chips = el('div','feedback-chips');
  REASONS.forEach(reason => {
    const label=el('label','feedback-chip'), input=el('input'); input.type='checkbox'; input.dataset.reason=reason;
    input.dataset.focus='feedback-'+id+'-'+reason;
    input.addEventListener('change',() => {
      const saved=receipts.get(id) || vote(null,[]), before=drafts.get(id) || saved.reasons;
      const reasons=REASONS.filter(r => r===reason ? input.checked : before.includes(r));
      if (saved.value) { send(id,saved.value,reasons); }
      else { drafts.set(id,reasons); update(id); }
    });
    label.append(input,el('span','',reason.replaceAll('-',' '))); chips.appendChild(label);
  }); detail.appendChild(chips); box.appendChild(detail);
  const status=el('small','feedback-status'); status.setAttribute('role','status'); box.appendChild(status);
  draw(box); return box;
}
on('status',() => document.querySelectorAll('[data-feedback]').forEach(draw));
on('snapshot',() => document.querySelectorAll('[data-feedback]').forEach(draw));
on('feedback',message => {
  if (!Number.isSafeInteger(message.line_id)) { return; }
  receive(message.line_id,message);
  update(message.line_id);
});
