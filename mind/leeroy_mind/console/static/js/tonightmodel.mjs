export const SECTIONS = ['raid','waiting','loot','bank','kit','week','guild'];
export const INTRO = 'Your raid, replies and supplies at a glance. Read-only; nothing here sends a message or changes the game.';
const TITLES = {raid:'Raid tonight',waiting:'Waiting for you',loot:'Due an item',bank:'Bank shortages',kit:'Your kit',week:'This week',guild:'Guild now'};
const arr = v => Array.isArray(v) ? v : [];
const count = v => Number.isInteger(v) && v >= 0 ? String(v) : 'unknown';
const at = v => typeof v === 'number' && Number.isFinite(v) ? new Date(v*1000).toLocaleString() : 'not recorded';
const why = {mind_down:'The mind is not running.',mind_stopping:'The mind is stopping.',
  no_character:'Your character could not be found.',no_armory:'The mind cannot read the game database.',
  no_link:'The console has no link to the mind.',slow:'This source is taking too long. Try again shortly.'};
const row = (text,href,people=[]) => ({text,href,people});

export function cardModel(name, value) {
  const p = value || {}, lines = [], title = TITLES[name] || name;
  if (p.status === 'not_set_up') { return {title,lines:[row('Not set up yet.')]}; }
  if (p.status !== 'ok') { return {title,lines:[row(why[p.why] || 'This source is unavailable just now.')]}; }
  if (name === 'raid') {
    arr(p.events).forEach(e => {
      lines.push(row(String(e.title)+' · '+at(e.at),'#calendar'));
      ['signed_up','tentative','declined'].forEach((k,i) => lines.push(row(
        ['Signed up','Tentative','Declined'][i]+': '+arr(e[k]).length,'#calendar',arr(e[k]))));
    });
    if (!lines.length) { lines.push(row('No raid on the calendar tonight.','#calendar')); }
  } else if (name === 'waiting') {
    [['dms','Unanswered DMs','#comms'],['letters','Letters to answer','#board'],
     ['selfies','Selfie sets to choose','#personas'],['polls','Open polls','#guild']].forEach(([k,label,href]) =>
      lines.push(row(label+': '+(p[k]===null ? 'not set up yet' : count(p[k])),href)));
  } else if (name === 'loot') {
    arr(p.due).forEach(m => lines.push(row(count(m.raids)+' raids since an item','#guild',[m])));
    if (!lines.length) { lines.push(row('Nobody is due an item yet.','#guild')); }
  } else if (name === 'bank') {
    arr(p.items).forEach(i => lines.push(row(String(i.name)+': '+count(i.have)+' of '+count(i.target),'#bank')));
    if (!lines.length) { lines.push(row('No stock item is below half its target.','#bank')); }
  } else if (name === 'kit') {
    const labels = {flask:'Flasks',potion:'Potions',food:'Food'};
    arr(p.groups).forEach(g => lines.push(row((labels[g.kind] || 'Other')+': '+count(g.have)+' of '+count(g.target)
      +(g.short>0 ? ' · need '+count(g.short) : ' · ready'))));
    arr(p.items).forEach(i => lines.push(row(String(i.name)+' × '+count(i.count))));
    lines.push(row('Counts include your bags and bank. Targets cover a three-hour raid night.'));
  } else if (name === 'week') {
    arr(p.events).forEach(e => lines.push(row(String(e.title)+' · '+at(e.at),'#calendar')));
    if (!lines.length) { lines.push(row('No events on the calendar in the next seven days.','#calendar')); }
  } else if (name === 'guild') {
    lines.push(row(p.human_online===true ? 'You are in game.' : p.human_online===false ? 'You are out of game.' : 'Your game presence is unknown.'));
    lines.push(row('Cast online: '+count(p.cast_online)));
    lines.push(row(p.night ? 'Last finished night: '+String(p.night.date)+' · '+count(p.night.failed_units)+' failed units' : 'No finished night is recorded.','#ops'));
  }
  return {title,lines};
}
