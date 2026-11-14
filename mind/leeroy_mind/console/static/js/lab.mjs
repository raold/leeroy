import { $, api, clear, el, on, post, refused } from "./core.mjs";
import { S } from "./state.mjs";
import { pollTab } from "./tabkit.mjs";
import { controlReason, WEIGHTS, weightValues, score, percent, reportView, versionLabel } from "./labmodel.mjs";
import { BoundedMap } from "./resilience.mjs";

let latest=null, busy=false, editVersion=null, returnTo=null, confirmed=null;
const readLines=new BoundedMap(100), options={path:'/api/lab',ms:30000,render};
const dialog=$('lab-confirm');
function button(text,action) {
  const b=el('button','',text); b.type='button'; b.dataset.labAction='true';
  b.addEventListener('click',action); return b;
}
function gates() {
  const why=controlReason(S.status); $('lab-why').textContent=why;
  document.querySelectorAll('[data-lab-action]').forEach(b => { b.disabled=!!why || busy; });
}
async function mutate(args) {
  if (busy || controlReason(S.status)) { return; }
  busy=true; gates(); $('lab-result').textContent='Saving…';
  const reply=await post('/api/learn',args); busy=false;
  $('lab-result').textContent=reply.message || reply.error || 'The change was not confirmed.';
  if (reply.ok) { if (args.action==='weights') { editVersion=null; } refresh(); }
  gates();
}
function confirm(text,action) {
  returnTo=document.activeElement; confirmed=action; $('lab-confirm-words').textContent=text; dialog.showModal();
}
dialog.addEventListener('close',() => { if (returnTo?.isConnected) { returnTo.focus(); } });
$('lab-confirm-cancel').addEventListener('click',() => dialog.close());
$('lab-confirm-save').addEventListener('click',() => { const action=confirmed; confirmed=null; dialog.close(); if (action) { action(); } });
function empty(box,text='Not set up yet.') { box.appendChild(el('p','note',text)); }
function details(title,key) { const d=el('details','lab-detail'); d.dataset.detail=key;
  const summary=el('summary','',title); summary.dataset.labFocus=key; d.appendChild(summary); return d; }
function lineButton(item) {
  const box=el('div','lab-line');
  if (readLines.has(item.id)) { box.appendChild(el('blockquote','',readLines.get(item.id))); return box; }
  const b=button('Read line '+item.id+' · '+score(item.value),async () => {
    b.disabled=true;
    try {
      const response=await api('/api/lab/line?id='+item.id); if (refused(response)) { return; }
      const value=await response.json();
      const words=value.line ? value.line.name+': '+value.line.text :
        response.status===200 ? 'This line is not available in your Comms history.' : 'This line could not be read.';
      readLines.set(item.id,words); box.appendChild(el('blockquote','',words));
      b.remove();
    } catch { b.disabled=false; b.textContent='Read failed — try line '+item.id+' again'; }
  }); delete b.dataset.labAction; box.appendChild(b); return box;
}
function rewards(view) {
  const box=$('lab-rewards'); clear(box);
  if (!view.available) { empty(box); return; }
  box.appendChild(el('p','hint','Last '+view.period_days+' days. Silence scores 0; quality only deducts. Days follow the computer’s local clock.'));
  const trend=details('Guild reward trend by day','reward-trend');
  view.days.forEach(d => trend.appendChild(el('p','mono',d.day+' · '+score(d.average)+' average · '+d.lines+' lines')));
  if (!view.days.length) { empty(trend,'No rewarded lines yet.'); } box.appendChild(trend);
  view.members.forEach(m => {
    const d=details(m.name+' · '+score(m.average)+' average · 👍 '+m.up+' / 👎 '+m.down,'reward-member-'+m.guid);
    d.appendChild(el('p','hint',m.lines+' scored lines. Thumbs include unscored lines.'));
    ['top','bottom'].forEach(side => { d.appendChild(el('h4','',side==='top' ? 'Top lines' : 'Bottom lines')); m[side].forEach(x => d.appendChild(lineButton(x))); });
    box.appendChild(d);
  });
}
function arms(view) {
  const box=$('lab-arms'); clear(box);
  latest.families.forEach(f => {
    const d=details(f,'family-'+f), frozen=latest.frozen_families.includes(f);
    const fixed=(latest.config_frozen_families || []).includes(f);
    const toggle=button(fixed ? 'Frozen in mind.toml' : frozen ? 'Unfreeze family' : 'Freeze family',() => mutate({action:'family',family:f,frozen:!frozen}));
    if (fixed) { toggle.disabled=true; }
    d.appendChild(toggle);
    if (!view.available) { empty(d); }
    view.arms.filter(a => a.family===f).forEach(a => {
      const arm=details(a.id+' · '+a.status,'arm-'+a.id); arm.appendChild(el('p','mono',JSON.stringify(a.params)));
      const verbs=a.status==='proposed' ? ['accept','reject'] : a.status==='active' ? ['retire'] : [];
      verbs.forEach(verb => arm.appendChild(button(verb[0].toUpperCase()+verb.slice(1),() =>
        confirm(verb[0].toUpperCase()+verb.slice(1)+' arm '+a.id+'?',() => mutate({action:'arm',id:a.id,verb})))));
      const contexts=view.contexts.filter(c => c.arm_id===a.id);
      contexts.forEach(c => arm.appendChild(el('p','lab-stat',c.context.join(' / ')+' · posterior '+score(c.alpha)+' / '+score(c.beta)
        +' · '+c.uses+' uses · '+c.turns+' turns ('+percent(c.share)+') · '+c.bad+' guarded / '+c.attempts+' checked')));
      if (!contexts.length) { empty(arm,'No measured turns yet. Posterior starts at 1 / 1.'); }
      d.appendChild(arm);
    }); box.appendChild(d);
  });
  if (view.limited) { empty(box,'Showing a bounded selection of arms and contexts. More remain stored.'); }
}
function lessons(view) {
  const box=$('lab-lessons'); clear(box);
  if (!view.available) { empty(box); return; }
  const label=el('label','','Member '), select=el('select'); select.id='lab-lesson-member';
  label.htmlFor=select.id; view.members.forEach(m => { const option=el('option','',m.name+' ('+m.count+')'); option.value=m.guid; select.appendChild(option); });
  select.value=String(view.member); select.dataset.labFocus='lesson-member';
  function page(offset) { options.path='/api/lab?member='+select.value+'&offset='+offset; refresh(); }
  select.addEventListener('change',() => page(0)); box.append(label,select);
  const nav=el('div','acts');
  const previous=button('Previous lessons',() => page(Math.max(0,view.offset-50)));
  const next=button('Next lessons',() => page(view.offset+50));
  delete previous.dataset.labAction; delete next.dataset.labAction;
  previous.disabled=view.offset===0; next.disabled=!view.more;
  nav.append(previous,el('span','hint',view.total ? 'Lessons '+(view.offset+1)+'–'+(view.offset+view.items.length)+' of '+view.total : 'No current lessons'),next); box.appendChild(nav);
  if (!view.items.length) { empty(box,'No active or proposed lessons yet.'); }
  view.items.forEach(l => {
    const d=details(l.name+' · '+l.scope+' · '+l.status,'lesson-'+l.id); d.appendChild(el('p','',l.text));
    d.appendChild(el('p','hint','Reinforced '+l.reinforced+' times · expires '+new Date(l.expires*1000).toLocaleString()));
    (l.status==='proposed' ? ['approve','veto','expire'] : ['veto','expire']).forEach(verb =>
      d.appendChild(button(verb==='expire' ? 'Expire now' : verb[0].toUpperCase()+verb.slice(1),() =>
        confirm((verb==='expire' ? 'Expire' : verb)+' this lesson for '+l.name+'?',() => mutate({action:'lesson',id:l.id,verb})))));
    box.appendChild(d);
  });
}
function weights(view) {
  $('lab-weights-note').textContent=view.available ? 'Current version '+view.current.version+'. Edits apply to future scoring.' : 'Not set up yet. Defaults are shown.';
  if (editVersion===null) {
    editVersion=view.current.version; WEIGHTS.forEach(k => { $('lab-weight-'+k).value=view.current.values[k]; });
  }
  $('lab-weights-save').hidden=!view.available;
  const box=$('lab-weight-history'); clear(box);
  view.history.forEach(w => box.appendChild(el('p','mono','Version '+w.version+' · '+new Date(w.created*1000).toLocaleString()+' · '+
    WEIGHTS.map(k => k+' '+w.values[k]).join(', '))));
}
function weekly(value) {
  const box=$('lab-report'); clear(box); const view=reportView(value.report);
  if (!view) {
    empty(box,value.cycle ? 'Not set up yet. The first weekly cycle runs on the night after a Sunday.'
      : 'Not set up yet. The weekly cycle is off ([learn] cycle in mind.toml).');
  } else {
    box.append(el('p','hint',view.heading),el('p','',view.lines),el('p','',view.diversity));
    view.rates.forEach(r => box.appendChild(el('p','mono',r)));
    [['promoted','Promoted'],['retired','Retired'],['rolled_back','Rolled back']].forEach(([key,title]) => {
      const d=details(title+' ('+view[key].length+')','report-'+key);
      view[key].forEach(t => d.appendChild(el('p','lab-stat',t)));
      if (!view[key].length) { empty(d,'None this week.'); } box.appendChild(d);
    });
    const held=details('Held back ('+view.held.count+')','report-held');
    view.held.items.forEach(t => held.appendChild(el('p','lab-stat',t)));
    if (view.held.count>view.held.items.length) { empty(held,'Showing the first '+view.held.items.length+'.'); }
    if (!view.held.count) { empty(held,'Nothing was waiting.'); } box.appendChild(held);
  }
  const saved=Array.isArray(value.versions) ? value.versions : [];
  const d=details('Saved learning states ('+saved.length+')','report-versions');
  d.appendChild(el('p','hint','A rollback restores arm statuses, promotions, lessons and reward weights. Your vetoes and freezes stay, and the current state is saved first.'));
  saved.forEach(v => {
    d.appendChild(el('p','lab-stat',versionLabel(v)+' · '+new Date(v.created*1000).toLocaleString()));
    d.appendChild(button('Roll back to version '+v.version,() => confirm('Roll back learning to version '+v.version+
      '? Arm statuses, promotions, lessons and reward weights return to that state; your vetoes and freezes stay. The current state is saved as a new version first.',
      () => mutate({action:'rollback',version:v.version}))));
  });
  if (!saved.length) { empty(d,'No saved states yet.'); } box.appendChild(d);
}
$('lab-weights-form').addEventListener('submit',event => {
  event.preventDefault();
  const values=weightValues(Object.fromEntries(WEIGHTS.map(k => [k,$('lab-weight-'+k).value.trim()==='' ? NaN : Number($('lab-weight-'+k).value)])));
  if (!values) { $('lab-result').textContent='All four weights must be between 0 and 1 and add up to 1.'; return; }
  const version=editVersion;
  confirm('Save new reward weights from version '+version+'? '+WEIGHTS.map(k => k+': '+values[k]).join(', ')+'. Earlier scores keep their recorded version.',
    () => mutate({action:'weights',values,version}));
});
$('lab-weights-reset').addEventListener('click',() => { editVersion=null; if (latest) { weights(latest.weights); } });
$('lab-freeze').addEventListener('click',() => {
  if (latest) {
    const frozen = !latest.freeze;
    if (latest.config_freeze) { return; }
    confirm(!frozen ? 'Unfreeze learning?' :
    'Freeze learning? New scoring, arms, coach lessons, voice fingerprints and preference capture will pause. Your feedback is still saved.',
    () => mutate({action:'freeze',frozen}));
  }
});
function render(value) {
  // Keep open details and keyboard focus through a successful poll.
  const open=new Set([...$('tab-lab').querySelectorAll('details[open]')].map(d => d.dataset.detail || d.id));
  const focused=document.activeElement;
  const focusKey=$('tab-lab').contains(focused) ? focused.dataset.labFocus : null;
  latest=value; $('lab-freeze').textContent=value.config_freeze ? 'Frozen in mind.toml' : value.freeze ? 'Unfreeze learning' : 'Freeze learning';
  $('lab-freeze').setAttribute('aria-pressed',String(value.freeze));
  $('lab-freeze').disabled=!!value.config_freeze;
  $('lab-learning-state').textContent=value.config_freeze ? 'Learning is frozen in mind.toml ([learn] freeze). Change it there.' : value.freeze ? 'Learning is frozen.' : value.enabled ? 'Learning is enabled by its config.' : 'Learning is disabled in config. Controls do not enable it.';
  rewards(value.rewards); arms(value.arms); lessons(value.lessons); weights(value.weights);
  weekly(value);
  $('tab-lab').querySelectorAll('details').forEach(d => {
    d.open=open.has(d.dataset.detail || d.id);
    d.querySelectorAll(':scope > button').forEach(b => { b.dataset.labFocus=(d.dataset.detail || d.id)+'-'+b.textContent; });
  }); gates();
  if (focusKey && !focused.isConnected) {
    $('tab-lab').querySelector('[data-lab-focus="'+CSS.escape(focusKey)+'"]')?.focus({preventScroll:true});
  }
}
const refresh=pollTab('lab',options);
on('status',gates); on('snapshot',gates);
