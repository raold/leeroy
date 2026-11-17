// The Comms list of conversations with one member: Discord DMs and WoW whisper threads as one
// list, the unread ones first, then the newest. A DM with no message yet (every member with a
// persona has one) is left out until he looks for the member by name, has it open, or it gets a
// message, so the list is as long as his conversations, not as the guild.

function n(x) { return typeof x === "number" && Number.isFinite(x) ? x : 0; }
function s(x) { return typeof x === "string" ? x : ""; }
export function needle(find) { return s(find).trim().toLocaleLowerCase(); }

// dms: DISCORD.dms ({guid, name, last_id, last_ts, unread}); threads: FEED.threads() ({guid, name,
// unread, last}); writing: members typing him a whisper ({guid, name}); open: the open view;
// find: the filter box's text. Returns {rows, hidden}: hidden counts the DMs left out.
export function conversationRows(o) {
  var want = needle(o.find), open = s(o.open), rows = [], hidden = 0;
  var typing = new Set((o.writing || []).map(function (w) { return w.guid; }));
  function match(name) { return !want || s(name).toLocaleLowerCase().indexOf(want) >= 0; }
  (o.dms || []).forEach(function (d) {
    var view = "dm:" + d.guid, used = n(d.last_id) > 0 || n(d.unread) > 0 || view === open;
    if (want ? !match(d.name) : !used) { if (!want) { hidden += 1; } return; }
    rows.push({ view: view, guid: d.guid, name: s(d.name), kind: "dm", unread: Math.max(0, n(d.unread)),
                ts: n(d.last_ts), typing: false, fresh: false });
  });
  var known = new Set();
  (o.threads || []).forEach(function (t) {
    known.add(t.guid);
    if (!match(t.name)) { return; }
    rows.push({ view: "w:" + t.guid, guid: t.guid, name: s(t.name), kind: "whisper", unread: Math.max(0, n(t.unread)),
                ts: t.last ? n(t.last.ts) : 0, typing: typing.has(t.guid), fresh: false });
  });
  // a member writing him a first whisper: the thread is there before its first line
  (o.writing || []).forEach(function (w) {
    if (known.has(w.guid) || !match(w.name)) { return; }
    rows.push({ view: "w:" + w.guid, guid: w.guid, name: s(w.name), kind: "whisper", unread: 0, ts: 0,
                typing: true, fresh: true });
  });
  rows.sort(function (a, b) {
    return (b.fresh - a.fresh) || ((b.unread > 0) - (a.unread > 0)) || (b.ts - a.ts)
      || a.name.localeCompare(b.name) || (a.kind < b.kind ? -1 : a.kind > b.kind ? 1 : 0) || (a.guid - b.guid);
  });
  return { rows: rows, hidden: hidden };
}
export function hiddenWords(hidden) {
  return hidden > 0 ? hidden + (hidden === 1 ? " more member has" : " more members have") + " no messages yet: find one by name above." : "";
}
export var KIND_WORDS = { dm: "DM", whisper: "whisper" };
export function rowLabel(row) {
  return row.name + ", " + (row.kind === "dm" ? "Discord DM" : "WoW whispers") + (row.unread ? ", " + row.unread + " unread" : "")
    + (row.typing ? ", typing" : "");
}
// "/" opens the reply box and Ctrl+K (or Cmd+K) the member filter, unless he is typing somewhere
export function shortcut(ev, typing) {
  if (ev.key === "k" && (ev.ctrlKey || ev.metaKey) && !ev.altKey && !ev.shiftKey) { return "find"; }
  if (ev.key === "/" && !ev.ctrlKey && !ev.metaKey && !ev.altKey && !typing) { return "reply"; }
  return "";
}
