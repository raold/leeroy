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
  guard:'guard trips worse', veto:'vetoed', diversity:'diversity floor', rolled_back:'rolled back before'};
const ROSE = {repetition:'repetition', stock:'stock phrases'};
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
  if (report.measured?.contradiction === 0) { rates.push('Contradiction: not measured (it needs a model).'); }
  if (report.measured?.unawareness === 0) { rates.push('Unawareness: not measured on this database.'); }
  const rose=Object.keys(ROSE).filter(k => report.diversity?.[k+'_rose']===1).map(k => ROSE[k]);
  const list=(items,line) => (Array.isArray(items) ? items : []).filter(x => x && typeof x==='object').map(line);
  return {
    heading:'Week '+report.week+(report.previous_week ? ', compared with '+report.previous_week : '')+'. '+
      (Number.isSafeInteger(report.state_version) ? 'The learning state before it is version '+report.state_version+'.' : ''),
    lines: (week.lines ?? 0)+' lines last week, '+(previous.lines ?? 0)+' the week before.',
    diversity: rose.length ? 'Guild '+rose.join(' and ')+' rose: nothing was promoted and the newest correlated promotions were rolled back.'
      : 'The diversity floor held.',
    rates,
    promoted: list(report.promoted, x => subject(x)+' · P '+score(x.probability)+' · '+(x.uses ?? 0)+' uses'),
    retired: list(report.retired, x => subject(x)+(x.reason==='share' ? ' · share '+percent(x.share)+', week before '+percent(x.share_previous)
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
