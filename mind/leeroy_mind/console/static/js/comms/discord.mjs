import { arr, num, str } from "../core.mjs";

// Pure Discord presentation rules. Store channel ids never overlap the WoW views.
export function channelOf(view) {
  if (/^d:[a-z][a-z0-9-]{0,39}$/.test(str(view))) { return view.slice(2); }
  return /^(?:dm:[1-9]\d{0,9}|gdm:[1-9]\d{0,15})$/.test(str(view)) ? view : "";
}
export function viewOfChannel(channel) { return /^(?:gdm|dm):/.test(channel) ? channel : "d:" + channel; }
export function mergeReadMarks(saved, discord) {
  var out = Object.create(null);
  Object.keys(saved || {}).forEach(function (key) { if (!channelOf(key)) { out[key] = saved[key]; } });
  return Object.assign(out, discord);
}
export function groupMessages(messages) {
  var previous = null;
  return arr(messages).map(function (m) {
    var grouped = !!previous && m.author === previous.author && m.thread === previous.thread
      && m.ts >= previous.ts && m.ts - previous.ts < 7 * 60
      && new Date(m.ts * 1000).toDateString() === new Date(previous.ts * 1000).toDateString()
      && !m.reply_to && m.deleted === null && previous.deleted === null;
    previous = m;
    return { message: m, grouped: grouped };
  });
}
export function quoteOf(message, messages, nameOf) {
  if (!message.reply_to) { return null; }
  var parent = messages instanceof Map ? messages.get(message.reply_to) : arr(messages).find(function (m) { return m.id === message.reply_to; });
  if (!parent) { return { name: "Earlier message", text: "Message outside this window" }; }
  return { name: nameOf ? nameOf(parent.author) : "#" + parent.author,
    text: parent.deleted !== null ? "Original message was deleted" : str(parent.text), deleted: parent.deleted !== null };
}
export function reactionBar(reactions, human, nameOf) {
  return arr(reactions).map(function (r) {
    var members = Array.from(new Set(arr(r.members)));
    return { emoji: str(r.emoji), count: members.length, mine: members.indexOf(human) >= 0,
      who: members.map(function (g) { return nameOf ? nameOf(g) : "#" + g; }).join(", ") };
  });
}
export function unread(messages, lastRead, human) {
  return arr(messages).filter(function (m) {
    return m.id > num(lastRead) && m.deleted === null && m.author !== human;
  }).length;
}
export function presenceDot(state) {
  return { online: "Online", idle: "Idle", dnd: "Do not disturb", offline: "Offline" }[state] || "Offline";
}
export function memberGroups(members) {
  var roles = ["officer", "raider", "social", "trial"], groups = [];
  roles.forEach(function (role) {
    var rows = arr(members).filter(function (m) { return m.role === role && m.presence.state !== "offline"; });
    if (rows.length) { groups.push({ role: role, members: rows }); }
  });
  var offline = arr(members).filter(function (m) { return m.presence.state === "offline"; });
  if (offline.length) { groups.push({ role: "offline", members: offline }); }
  return groups;
}
export function mentionsHim(text, human) {
  var wanted = str(human).toLocaleLowerCase();
  return !!wanted && (str(text).match(/@[\p{L}\p{N}_]+/gu) || []).some(function (s) {
    return s.slice(1).toLocaleLowerCase() === wanted;
  });
}

// Read marks are independent of the shared, globally increasing message ids. The server
// counts unread rows after each mark, excluding his own messages and deleted messages.
export class Discord {
  constructor(read) {
    this.read = {};
    Object.keys(read || {}).forEach((key) => {
      if (channelOf(key) && Number.isSafeInteger(read[key]) && read[key] >= 0) { this.read[key] = read[key]; }
    });
    this.categories = []; this.dms = []; this.members = []; this.human = { guid: 0, name: "" };
    this.windows = new Map(); this.error = ""; this.loaded = false; this.missing = [];
      this.photos = false;
    this.voice_notes = false;
    this.groups = []; this.groups_enabled = false;
  }
  channels() {
    return this.categories.flatMap(function (c) { return c.channels; }).concat(this.dms.map(function (d) {
      return { id: "dm:" + d.guid, name: d.name, last_id: d.last_id, unread: d.unread };
    })).concat(this.groups);
  }
  adopt(data, thread) {
    this.photos = data.photos === true;
    this.voice_notes = data.voice_notes === true;
    this.groups_enabled = data.groups_enabled === true;
    this.groups = this.groups_enabled ? arr(data.groups) : [];
    this.categories = arr(data.categories); this.dms = arr(data.dms); this.members = arr(data.members);
    this.human = data.human || this.human; this.loaded = true; this.error = ""; this.missing = arr(data.missing);
    var current = new Set(this.channels().map(function (c) { return viewOfChannel(c.id); }));
    Object.keys(this.read).forEach((view) => { if (!current.has(view)) { delete this.read[view]; } });
    this.channels().forEach((c) => {
      var view = viewOfChannel(c.id);
      if (!(view in this.read)) { this.read[view] = num(c.last_id); }
      if (num(c.last_id) <= this.read[view]) {
        c.unread = 0;
        if (view.indexOf("dm:") === 0) { this.dms.find(function (d) { return "dm:" + d.guid === view; }).unread = 0; }
      }
    });
    if (data.channel) {
      var key = this.key(data.channel, thread); this.windows.delete(key); this.windows.set(key, data);
      while (this.windows.size > 60) { this.windows.delete(this.windows.keys().next().value); }
    }
  }
  key(channel, thread) { return channel + ":" + (thread || 0); }
  window(view, thread) {
    if (view.startsWith('gdm:') && !this.groups_enabled) { return undefined; }
    return this.windows.get(this.key(channelOf(view), thread));
  }
  title(view) {
    var channel = channelOf(view), c = this.channels().find(function (x) { return x.id === channel; });
    return c ? (/^(?:gdm|dm):/.test(channel) ? c.name : "# " + c.name) : channel;
  }
  name(guid) { return guid === this.human.guid ? this.human.name || "You" : (this.members.find(function (m) { return m.guid === guid; }) || {}).name || "#" + guid; }
  unread(view) {
    var c = this.channels().find(function (x) { return x.id === channelOf(view); });
    return c ? Math.max(0, num(c.unread)) : 0;
  }
  markRead(view) {
    var c = this.channels().find(function (x) { return x.id === channelOf(view); });
    if (c) {
      this.read[view] = Math.max(num(this.read[view]), num(c.last_id));
      if (view.indexOf("dm:") === 0) { this.dms.find(function (d) { return "dm:" + d.guid === view; }).unread = 0; }
      else { c.unread = 0; }
    }
  }
  marks() {
    var out = {};
    Object.keys(this.read).forEach((view) => { out[channelOf(view)] = this.read[view]; });
    return out;
  }
}
export function memberStatus(member) {
  const lines = [];
  if (typeof member.status === 'string' && member.status.length <= 80 && member.status) { lines.push(member.status); }
  if (member.presence?.playing) { lines.push('Playing WoW'); }
  return lines;
}
