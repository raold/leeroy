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
export const RATES = [['repetition','Repetition'],['stock','Stock phrases'],['nonsequitur','Non sequiturs'],
  ['unawareness','Unawareness'],['guard','Guard trips']];
const REASONS_HELD = {uses:'under 30 rewarded uses', probability:'not yet 90% likely to beat control',
  no_week_lines:'no lines last week', no_control_lines:'nothing to compare with', detectors:'detector rates worse',
  guard:'guard trips worse', veto:'vetoed', diversity:'diversity floor',
  rolled_back:'rolled back by the diversity floor (waits 2 weeks and 30 new uses)',
  owner_rollback:'reverted by your rollback (waits for 30 new uses)'};
const MEASURED = {nonsequitur:'Non sequiturs: partial (filler after a question only; relevance needs a model).',
  unawareness:'Unawareness: partial (only plain claims against verified kills, raids and places).',
  contradiction:'Contradiction: not measured (it needs a model).'};
const ROSE = {repetition:'repetition', stock:'stock phrases'};
const MEDIUMS = {game:'the game', discord:'Discord rooms', dm:'DMs'};
function context(value) { return Array.isArray(value) && value.length ? ' in '+value.join(' / ') : ''; }
function subject(item) {
  return item.kind==='lesson' ? 'lesson '+item.id+(Number.isFinite(item.guid) ? ' (member #'+item.guid+')' : '')
    : 'arm '+item.id+context(item.context);
}
// The weekly cycle's numeric report as plain sentences. Nothing here is chat text.
export function reportView(report) {
  if (!report || typeof report !== 'object' || typeof report.week !== 'string') { return null; }
  const week=report.rates?.week || {}, previous=report.rates?.previous || {};
  const rates=RATES.map(([key,label]) => label+' '+percent(week[key])+' (week before '+percent(previous[key])+')');
  Object.keys(MEASURED).forEach(k => {
    if (report.measured?.[k] === 'not_measured') { rates.push(k==='contradiction' ? MEASURED[k] : k[0].toUpperCase()+k.slice(1)+': not measured.'); }
    else if (report.measured?.[k] === 'partial') { rates.push(MEASURED[k]); }
  });
  const rose=Array.isArray(report.diversity?.rose) && report.diversity.rose.length
    ? report.diversity.rose.map(r => { const [where,what]=String(r).split(':'); return (ROSE[what] || what)+' in '+(MEDIUMS[where] || where); })
    : Object.keys(ROSE).filter(k => report.diversity?.[k+'_rose']===1).map(k => ROSE[k]);
  const list=(items,line) => (Array.isArray(items) ? items : []).filter(x => x && typeof x==='object').map(line);
  return {
    heading:'Week '+report.week+(report.previous_week ? ', compared with '+report.previous_week : '')+'. '+
      (Number.isSafeInteger(report.state_version) ? 'The learning state before it is version '+report.state_version+'.' : ''),
    lines: (week.lines ?? 0)+' lines last week, '+(previous.lines ?? 0)+' the week before.',
    diversity: rose.length ? 'Guild '+rose.join(' and ')+' rose significantly: nothing was promoted and the newest correlated promotions were rolled back.'
      : 'The diversity floor held (no significant rise in repetition or stock phrases).',
    rates,
    promoted: list(report.promoted, x => subject(x)+' · P '+score(x.probability)+' · '+(x.uses ?? 0)+' uses'),
    retired: list(report.retired, x => subject(x)+(x.reason==='share' ? ' · share '+percent(x.share)+', week before '+percent(x.share_previous)+(Number.isFinite(x.probability) ? ' · P '+score(x.probability) : '')
      : ' · not reinforced in 14 days')),
    rolled_back: list(report.rolled_back, x => subject(x)+' · '+(Array.isArray(x.reasons) ? x.reasons.map(r => ROSE[r] || r).join(', ') : '')),
    held: {count: Number.isSafeInteger(report.held?.count) ? report.held.count : 0,
      items: list(report.held?.items, x => subject(x)+' · '+(Array.isArray(x.reasons) ? x.reasons.map(r => REASONS_HELD[r] || r).join(', ') : ''))},
  };
}
export function versionLabel(v) {
  return 'Version '+v.version+' · '+(v.reason==='cycle' ? 'before the weekly cycle'+(v.week ? ' for '+v.week : '')
    : 'saved before a rollback'+(Number.isSafeInteger(v.restored_from) ? ' to '+v.restored_from : ''));
}
export function rollbackWords(version, weights) {
  return 'Roll back learning to version '+version+'? Arm statuses, promotions and lessons return to that state; '+
    'later decisions of yours and the guards, your vetoes and freezes stay. Reward weights '+
    (weights ? 'return to that version too.' : 'stay as they are.')+' The current state is saved as a new version first.';
}

// The Turing panel (turing/report.py): numbers, fixed labels and the judge's short phrases.
export const CHART = {width: 240, height: 64, pad: 4};
function fixed(v) { return Number.isFinite(v) ? v.toFixed(2) : 'n/a'; }
function pct(v) { return Number.isFinite(v) ? Math.round(100*v)+'%' : 'n/a'; }
// Chart coordinates for values on a 0-1 axis, one per night; a night without one is left out.
export function chartPoints(values) {
  const {width, height, pad}=CHART, list=Array.isArray(values) ? values : [];
  const step=list.length>1 ? (width-2*pad)/(list.length-1) : 0;
  return list.map((v,i) => Number.isFinite(v) ? [
    +(list.length>1 ? pad+i*step : width/2).toFixed(1),
    +(pad+(1-Math.max(0,Math.min(1,v)))*(height-2*pad)).toFixed(1)] : null).filter(Boolean);
}
const STATUS = {too_few:'too few lines to compare', one_class:'only one side spoke'};
export function turingView(value) {
  const record=x => !!x && typeof x==='object' && !Array.isArray(x);
  if (!record(value) || !value.available || !Array.isArray(value.families)) {
    return {available:false, days: Number.isSafeInteger(value?.days) ? value.days : 14, families:[]};
  }
  const families=value.families.filter(record).map(f => {
    const points=(Array.isArray(f.points) ? f.points : []).filter(record);
    const latest=record(f.latest) ? f.latest : null, judge=record(f.judge) ? f.judge : {};
    const headline=!latest ? 'No style measurement stored yet.' : latest.status==='ok'
      ? 'AUC '+fixed(latest.auc)+(Number.isFinite(latest.low) && Number.isFinite(latest.high) ? ' (approx. '+fixed(latest.low)+'–'+fixed(latest.high)+')' : '')
        +' on '+latest.night+' · '+latest.sample+' lines each'
      : 'Not measured on '+latest.night+': '+(STATUS[latest.status] || latest.status)+' ('+latest.human_lines+' of yours, '+latest.member_lines+' of theirs)';
    // Balanced accuracy, never raw: members are most speakers, so an always-AI judge scores high raw.
    const sides=[Number.isSafeInteger(judge.members) && judge.members>0 ? 'members called AI '+pct(judge.members_ai)+' of '+judge.members+' times' : '',
      Number.isSafeInteger(judge.his) && judge.his>0 ? 'you called human '+pct(judge.his_human)+' of '+judge.his+' times' : ''].filter(Boolean);
    const verdict=!(Number.isSafeInteger(judge.speakers) && judge.speakers>0) ? 'Blind judge: no windows judged yet.'
      : Number.isFinite(judge.balanced) ? 'Blind judge: '+pct(judge.balanced)+' balanced accuracy (50% is chance): '+sides.join(', ')+'.'
      : 'Blind judge: '+sides.join(', ')+'; balanced accuracy needs both sides.';
    return {family:String(f.family || ''), label:String(f.label || f.family || ''), headline, verdict,
      auc:chartPoints(points.map(p => p.auc)), judge:chartPoints(points.map(p => p.judge)),
      nights:points.map(p => p.night+' · AUC '+fixed(p.auc)+(Number.isSafeInteger(p.speakers) && p.speakers ? ' · judge '+pct(p.judge)+' balanced ('+p.speakers+' speakers)' : '')),
      features:(latest && Array.isArray(latest.features) ? latest.features : []).map(x => x?.words).filter(w => typeof w==='string' && w),
      tells:(Array.isArray(f.tells) ? f.tells : []).filter(record).map(t => String(t.label)+' ('+t.count+')'+
        (Array.isArray(t.phrases) ? t.phrases.filter(p => typeof p==='string').map(p => ' · “'+p+'”').join('') : ''))};
  });
  return {available:true, days: Number.isSafeInteger(value.days) ? value.days : 14, families};
}

// The Conversation lab panel (lab/report.py panel): the nights' control means by kind (the lab
// judge's 1-5 score, or the old 0-3 rubric), rule-flag rates, the blind judge, and the variants
// tried and promoted with their word diffs. A run that only measured says so: its deltas are
// indicative and nothing was proposed to the weekly gate.
export const MEASURE_ONLY = 'Measure only: the lab records and reports. Its judge does not yet agree with a strong grader, '
  +'so the changes shown are indicative and no variant is proposed to the weekly gate.';
const OUTCOMES = {won:'promoted', measure_only:'ahead of the control (indicative; not proposed)',
  beaten:'beat the control, not the best', lost:'lost',
  regressed:'a rule check got worse', untested:'never in a prompt', too_few:'too few graded answers',
  budget:'cut by the budget', frozen:'learning frozen'};
const FLAG_WORDS = {silent:'said nothing', unanswered:'question not answered first', wordy:'wordy',
  repeat:'same subject again', rewritten:'guard rewrote', echo:'echoed him', stock:'stock phrase'};
export function conversationView(value) {
  const record=x => !!x && typeof x==='object' && !Array.isArray(x);
  if (!record(value) || !value.available || !Array.isArray(value.runs)) { return {available:false, runs:[], tried:0, promoted:0}; }
  const runs=value.runs.filter(record).map(r => {
    const flags=record(r.flags) ? Object.entries(r.flags).filter(([,v]) => Number.isFinite(v) && v>0)
      .map(([k,v]) => (FLAG_WORDS[k] || k)+' '+pct(v)) : [];
    const kinds=record(r.kinds) ? Object.entries(r.kinds).filter(([,v]) => Number.isFinite(v)).map(([k,v]) => k+' '+fixed(v)) : [];
    const headline=String(r.night || '')+' · '+String(r.piece || '')+' · '+String(r.status || '')+(r.reason ? ' ('+r.reason+')' : '')
      +(Number.isFinite(r.score) ? ' · control '+fixed(r.score)+' of 5 over '+(r.scored|0)+' answers'
        : Number.isFinite(r.rubric) ? ' · control '+fixed(r.rubric)+' of 3 (old rubric) over '+(r.graded|0)+' answers' : '')
      +(r.promote===false ? ' · measure only' : '')
      +(Number.isFinite(r.human) ? ' · judge called her human '+pct(r.human) : '');
    const variants=(Array.isArray(r.variants) ? r.variants : []).filter(record).map(v =>
      'Variant '+v.id+': '+(OUTCOMES[v.outcome] || v.outcome || 'waiting')+(Number.isFinite(v.delta) ? ' ('+(v.delta>=0 ? '+' : '')+v.delta.toFixed(2)+')' : '')
      +(record(v.prefer) && Number.isFinite(v.prefer.share) ? ' · preferred to the control’s answer '+pct(v.prefer.share)+' of '+(v.prefer.n|0)+' lines' : '')
      +(v.arm ? ' · arm '+v.arm+(v.arm_status ? ' ('+v.arm_status+')' : '') : '')
      +(typeof v.diff==='string' && v.diff ? ' · '+v.diff : ''));
    return {headline, kinds, flags, variants};
  });
  const measuring=value.runs.filter(record).some(r => r.promote===false);
  return {available:true, runs, note: measuring ? MEASURE_ONLY : '', tried:Number.isSafeInteger(value.tried) ? value.tried : 0,
    promoted:Number.isSafeInteger(value.promoted) ? value.promoted : 0};
}
