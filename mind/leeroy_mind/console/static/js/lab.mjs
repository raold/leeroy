import { $, api, clear, el, on, post, refused } from "./core.mjs";
import { S } from "./state.mjs";
import { isoMinute } from "./dates.mjs";
import { pollTab } from "./tabkit.mjs";
import { controlReason, WEIGHTS, weightValues, score, percent, reportView, versionLabel, rollbackWords, turingView, conversationView, CHART,
         pendingView, ratedWords, familyTitle, armTitle } from "./labmodel.mjs";
import { setBadge } from "./tabs.mjs";
import { BoundedMap } from "./resilience.mjs";

let latest=null, busy=false, editVersion=null, returnTo=null, confirmed=null, withWeights=false;
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
// What waits for him, first on the tab: each row with its own buttons, so nothing has to be
// found inside the folded panels below.
function pending(value) {
  const box=$('lab-pending'); clear(box); const view=pendingView(value.pending);
  setBadge('lab',view.count);
  $('lab-pending-title').textContent=view.count ? 'Needs your decision ('+view.count+')' : 'Needs your decision';
  if (!view.arms.length && !view.lessons.length && !view.variants.length) { empty(box,'Nothing is waiting for you.'); return; }
  const row=(text,cls) => { const r=el('div','lab-pending-row'); r.appendChild(el('p',cls || '',text)); box.appendChild(r); return r; };
  const act=(r,label,words,args,key) => { const b=button(label,() => confirm(words,() => mutate(args))); b.dataset.labFocus=key+'-'+label; r.appendChild(b); };
  view.arms.forEach(a => { const r=row(a.words);
    act(r,'Accept','Accept arm '+a.id+'?',{action:'arm',id:a.id,verb:'accept'},'pending-arm-'+a.id);
    act(r,'Reject','Reject arm '+a.id+'?',{action:'arm',id:a.id,verb:'reject'},'pending-arm-'+a.id); });
  view.lessons.forEach(l => { const r=row(l.words);
    act(r,'Approve','approve this lesson for '+l.name+'?',{action:'lesson',id:l.id,verb:'approve'},'pending-lesson-'+l.id);
    act(r,'Veto','veto this lesson for '+l.name+'?',{action:'lesson',id:l.id,verb:'veto'},'pending-lesson-'+l.id); });
  if (view.more) { empty(box,view.more); }
  if (view.variants.length) { box.appendChild(el('h4','','Lab variants now in use')); }
  view.variants.forEach(v => { const r=row(v.words,'lab-stat');
    act(r,'Retire','Retire arm '+v.id+'?',{action:'arm',id:v.id,verb:'retire'},'pending-variant-'+v.id); });
}
function rewards(view) {
  const box=$('lab-rewards'); clear(box);
  if (!view.available) { empty(box); return; }
  box.appendChild(el('p','hint','Last '+view.period_days+' days. Silence scores 0; quality only deducts. Days follow the computer’s local clock.'));
  const trend=details('Guild reward trend by day ('+view.days.length+')','reward-trend');
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
    const d=details(familyTitle(f,view.arms),'family-'+f), frozen=latest.frozen_families.includes(f);
    const fixed=(latest.config_frozen_families || []).includes(f);
    const toggle=button(fixed ? 'Frozen in mind.toml' : frozen ? 'Unfreeze family' : 'Freeze family',() => mutate({action:'family',family:f,frozen:!frozen}));
    if (fixed) { toggle.disabled=true; }
    d.appendChild(toggle);
    if (!view.available) { empty(d); }
    view.arms.filter(a => a.family===f).forEach(a => {
      const arm=details(armTitle(a,view.contexts),'arm-'+a.id); arm.appendChild(el('p','mono',JSON.stringify(a.params)));
      const verbs=a.status==='proposed' ? ['accept','reject'] : a.status==='active' ? ['retire'] : a.status==='retired' ? ['accept'] : [];
      verbs.forEach(verb => { const label=a.status==='retired' ? 'Accept again' : verb[0].toUpperCase()+verb.slice(1);
        arm.appendChild(button(label,() => confirm(label+' arm '+a.id+'?',() => mutate({action:'arm',id:a.id,verb})))); });
      const contexts=view.contexts.filter(c => c.arm_id===a.id);
      contexts.forEach(c => arm.appendChild(el('p','lab-stat',c.context.join(' / ')+(c.promoted ? ' · promoted by the weekly cycle' : '')+' · posterior '+score(c.alpha)+' / '+score(c.beta)
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
    const d=details(l.name+' · '+l.scope+' · '+l.status+(l.promoted ? ' · promoted' : ''),'lesson-'+l.id); d.appendChild(el('p','',l.text));
    d.appendChild(el('p','hint','Reinforced '+l.reinforced+' times · expires '+isoMinute(l.expires)));
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
  view.history.forEach(w => box.appendChild(el('p','mono','Version '+w.version+' · '+isoMinute(w.created)+' · '+
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
  d.appendChild(el('p','hint','A rollback restores arm statuses, promotions and lessons. Later decisions of yours and the guards, your vetoes and freezes stay, and the current state is saved first. What it reverts waits for 30 new rewarded uses.'));
  const label=el('label','','Also restore that version’s reward weights '), check=el('input'); check.type='checkbox';
  check.id='lab-rollback-weights'; check.checked=withWeights; check.addEventListener('change',() => { withWeights=check.checked; });
  label.htmlFor=check.id; d.append(label,check);
  saved.forEach(v => {
    d.appendChild(el('p','lab-stat',versionLabel(v)+' · '+isoMinute(v.created)));
    d.appendChild(button('Roll back to version '+v.version,() => { const weights=withWeights;
      confirm(rollbackWords(v.version,weights),() => mutate({action:'rollback',version:v.version,weights})); }));
  });
  if (!saved.length) { empty(d,'No saved states yet.'); } box.appendChild(d);
}
// The trend chart is a clone of the page's own empty <svg>, so its parts share its namespace.
function chart(f) {
  const svg=$('lab-turing-chart').cloneNode(false), ns=svg.namespaceURI;
  svg.removeAttribute('id'); svg.setAttribute('aria-hidden','true');
  const part=(name,attrs) => { const n=document.createElementNS(ns,name);
    Object.entries(attrs).forEach(([k,v]) => n.setAttribute(k,String(v))); svg.appendChild(n); return n; };
  const mid=CHART.height/2;
  part('line',{x1:CHART.pad,x2:CHART.width-CHART.pad,y1:mid,y2:mid,class:'turing-chance'});
  [['auc','turing-auc'],['judge','turing-judge']].forEach(([key,cls]) => {
    const pts=f[key];
    if (pts.length>1) { part('polyline',{points:pts.map(p => p.join(',')).join(' '),class:cls}); }
    pts.forEach(([x,y]) => part('circle',{cx:x,cy:y,r:2.5,class:cls+' turing-dot'}));
  });
  return svg;
}
function turing(value) {
  const box=$('lab-turing'); clear(box); const view=turingView(value);
  if (!view.available) { empty(box,'Not set up yet. Turn on [turing] enabled in mind.toml; each night then stores a score.'); return; }
  box.appendChild(el('p','hint','Last '+view.days+' days. AUC 0.50: the members read like you; 1.00: always told apart. '+
    'The solid line is the style AUC, the dotted one the blind judge’s balanced accuracy; the dashed line is 0.50, chance for both. Intervals are approximate: a change under 0.10 is within noise.'));
  if (!view.families.length) { empty(box,'No nights stored in the last '+view.days+' days.'); }
  view.families.forEach(f => {
    const part=el('div','turing-family'); part.append(el('h4','',f.label),el('p','lab-stat',f.headline),chart(f),el('p','lab-stat',f.verdict));
    [['Style tells','features','None stand out.'],['What the judge caught','tells','Nothing caught yet.'],['By night','nights','No nights yet.']].forEach(([title,key,none]) => {
      const d=details(title+' ('+f[key].length+')','turing-'+key+'-'+f.family);
      f[key].forEach(t => d.appendChild(el('p','lab-stat',t)));
      if (!f[key].length) { empty(d,none); } part.appendChild(d);
    });
    box.appendChild(part);
  });
}
function conversation(value) {
  const box=$('lab-conversation'); clear(box); const view=conversationView(value);
  if (!view.available) { empty(box,'Not set up yet. Turn on [lab] enabled in mind.toml; each night then runs the battery in a sandbox copy.'); return; }
  box.appendChild(el('p','hint','Each night: invented lines of yours to sampled members in a sandbox copy, each answer scored 1-5 by the lab judge (5: reads as a real person texting) with rule checks and the blind judge; prompt variants tried against the wording in use. Variants tried: '+view.tried+', promoted into the learn bandit: '+view.promoted+'.'));
  if (view.note) { box.appendChild(el('p','hint',view.note)); }
  if (!view.runs.length) { empty(box,'No nights stored yet.'); }
  view.runs.forEach((r,i) => {
    const part=el('div','turing-family'); part.appendChild(el('p','lab-stat',r.headline));
    [['By kind','kinds','No grades yet.'],['Rule checks','flags','No flags.'],['Variants','variants','None tried.']].forEach(([title,key,none]) => {
      const d=details(title+' ('+r[key].length+')','lab-conv-'+key+'-'+i);
      r[key].forEach(t => d.appendChild(el('p','lab-stat',t)));
      if (!r[key].length) { empty(d,none); } part.appendChild(d);
    });
    box.appendChild(part);
  });
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
  pending(value); $('lab-rated').textContent=ratedWords(value.rated_week);
  $('lab-weight-history').parentNode.querySelector('summary').textContent='Version history ('+value.weights.history.length+')';
  rewards(value.rewards); arms(value.arms); lessons(value.lessons); weights(value.weights);
  weekly(value); turing(value.turing); conversation(value.conversation);
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
