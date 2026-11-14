// Counts only: never turn a topic, reference, or unexpected field into display text.
const KINDS = {life:'Life',plan:'Plans',followup:'Follow-ups',his_day:'Your day',
  birthday:'Birthdays',miss_you:'Missing you',dm_nudge:'DM reminders'};
const STATES = ['open','told','dropped'];
const count = v => Number.isInteger(v) && v >= 0 ? v : null;
const unavailable = p => p.status === 'not_set_up'
  ? 'Not set up yet in this mind.db.' : 'These counts are unavailable just now.';

export function memberOutreachModel(value) {
  if (!value) { return null; } // no field when the restart-only switch is off
  if (value.status !== 'ok') { return {rows:[],totals:null,why:unavailable(value)}; }
  const rows = [];
  for (const [kind,label] of Object.entries(KINDS)) {
    const counts = value.kinds?.[kind];
    if (!counts) { continue; }
    const values = STATES.map(state => count(counts[state]));
    if (values.some(n => n === null)) { return {rows:[],totals:null,why:unavailable({})}; }
    rows.push({label,values});
  }
  const totals = STATES.map(state => count(value.totals?.[state]));
  if (totals.some(n => n === null)) { return {rows:[],totals:null,why:unavailable({})}; }
  return {rows,totals,why:rows.length ? '' : 'Nothing on this list yet.'};
}

export function tonightOutreachLine(value) {
  if (!value) { return null; }
  const n = value.status === 'ok' ? count(value.count) : null;
  const words = n !== null ? String(n) : value.status === 'not_set_up' ? 'not set up yet' : 'unavailable';
  return 'Things members want to tell you: '+words;
}
