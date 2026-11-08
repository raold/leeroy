import { $, clear, clock, el, emit, on, str } from "../core.mjs";
import { S } from "../state.mjs";
import { face, faceButton } from "../faces.mjs";
import { currentView, openView } from "./feedview.mjs";
import { channelOf, groupMessages, memberGroups, mentionsHim, presenceDot, quoteOf, reactionBar, viewOfChannel } from "./discord.mjs";
import { DISCORD, discordBusy, discordReason, mutateDiscord, refreshDiscord, saveDiscordRead, watchDiscord } from "./discordstate.mjs";
import { PICKER, isOneEmoji } from "./emoji.mjs";

var shown = "", thread = 0, panel = "", reply = null, entryMark = 0, drawing = "";
var drafts = new Map(), replyDrafts = new Map(), folded = new Set(), dialog = null, returnTo = null, failure = "";
var input = $("dc-text"), box = $("feed");
function button(text, cls) { var b = el("button", cls || "", text); b.type = "button"; return b; }
function active() { return !!channelOf(currentView()); }
function windowData() { return DISCORD.window(currentView(), thread); }
function name(guid) { return DISCORD.name(guid); }
function focusKey(root) {
  var a = document.activeElement;
  return a && root.contains(a) && a.dataset.focus ? a.dataset.focus : "";
}
function restore(root, key) {
  var e = key ? root.querySelector('[data-focus="' + CSS.escape(key) + '"]') : null;
  if (e) { e.focus({ preventScroll: true }); }
}
function routeKey() { return shown + ":" + thread; }
function keepDraft() {
  if (!shown) { return; }
  var key = routeKey(); drafts.delete(key); replyDrafts.delete(key);
  if (input.value || reply) { drafts.set(key, input.value); replyDrafts.set(key, reply); }
  while (drafts.size > 60) { var oldest = drafts.keys().next().value; drafts.delete(oldest); replyDrafts.delete(oldest); }
}
function loadDraft() { input.value = drafts.get(routeKey()) || ""; reply = replyDrafts.get(routeKey()) || null; failure = ""; }
function chooseThread(id) {
  keepDraft(); thread = id || 0; panel = ""; drawing = ""; loadDraft();
  watchDiscord(currentView(), thread); render(); input.focus();
}
function dot(state) {
  var d = el("span", "dc-presence dc-" + (state || "offline"), presenceDot(state));
  return d;
}
function renderLists() {
  var left = $("dc-channels"), key = focusKey(left); clear(left);
  if (!DISCORD.loaded) { left.appendChild(el("p", "none", DISCORD.error || "Loading the guild's Discord…")); }
  DISCORD.categories.forEach(function (category) {
    var details = el("details", "dc-category"); details.open = !folded.has(category.key);
    var summary = el("summary", "sec", category.name); summary.dataset.focus = "category-" + category.key;
    details.appendChild(summary);
    details.addEventListener("toggle", function () {
      if (!details.isConnected) { return; }
      if (details.open) { folded.delete(category.key); } else { folded.add(category.key); }
    });
    category.channels.forEach(function (channel) { details.appendChild(channelButton(channel.id, "# " + channel.name, channel.officers_only)); });
    left.appendChild(details);
  });
  restore(left, key);
  var dms = $("dc-dms"), dmkey = focusKey(dms); clear(dms);
  DISCORD.dms.forEach(function (d) { dms.appendChild(channelButton("dm:" + d.guid, d.name, false)); });
  if (DISCORD.loaded && !DISCORD.dms.length) { dms.appendChild(el("p", "none", "No members with personas yet.")); }
  restore(dms, dmkey);
}
function channelButton(channel, label, locked) {
  var view = viewOfChannel(channel), b = button("", "ch" + (currentView() === view ? " on" : ""));
  b.dataset.view = view; b.dataset.focus = view;
  b.appendChild(el("span", "nm", label));
  if (locked) { b.appendChild(el("span", "lock", "Officers")); }
  var unread = DISCORD.unread(view);
  if (unread) { b.appendChild(el("span", "badge", String(unread))); b.setAttribute("aria-label", label + ", " + unread + " unread"); }
  if (currentView() === view) { b.setAttribute("aria-current", "true"); }
  return b;
}
function renderMembers() {
  var members = $("dc-members"); members.hidden = !active();
  if (!active()) { return; }
  var key = focusKey(members); clear(members);
  members.appendChild(el("h3", "", "Guild members"));
  memberGroups(DISCORD.members).forEach(function (g) {
    members.appendChild(el("h4", "sec", g.role + " — " + g.members.length));
    g.members.forEach(function (m) {
      var row = el("div", "dc-member dc-class-" + m.class_key);
      row.appendChild(faceButton(m.guid, m.name, "list"));
      var words = el("div", "dc-member-words"); words.appendChild(el("span", "who", m.name));
      words.appendChild(dot(m.presence.state));
      if (m.presence.playing) { words.appendChild(el("small", "muted", "Playing WoW")); }
      row.appendChild(words);
      if (DISCORD.dms.some(function (d) { return d.guid === m.guid; })) {
        var dm = button("DM"); dm.dataset.dm = m.guid; dm.dataset.focus = "dm-" + m.guid;
        dm.setAttribute("aria-label", "Message " + m.name); row.appendChild(dm);
      }
      members.appendChild(row);
    });
  });
  restore(members, key);
}
function messageRow(item, data) {
  var m = item.message, mine = m.author === DISCORD.human.guid, deleted = m.deleted !== null;
  var row = el("article", "msg dc-message" + (item.grouped ? " dc-grouped" : "") + (mine ? " mine" : "")
    + (mentionsHim(m.text, DISCORD.human.name) ? " dc-mentioned" : ""));
  row.dataset.id = m.id;
  if (!item.grouped) { row.appendChild(mine ? face(m.author, name(m.author), "feed", { you: true }) : faceButton(m.author, name(m.author), "feed")); }
  else { row.appendChild(el("span", "dc-time-small", clock(m.ts * 1000).slice(0, 5))); }
  var content = el("div", "dc-content");
  if (!item.grouped) {
    var head = el("header", "h"), who = el("span", "who", mine ? "You" : name(m.author)); head.appendChild(who);
    var time = el("time", "t", clock(m.ts * 1000)); time.dateTime = new Date(m.ts * 1000).toISOString();
    time.title = new Date(m.ts * 1000).toLocaleString(); head.appendChild(time); content.appendChild(head);
  }
  var quote = quoteOf(m, (data.quotes || []).concat(data.messages), name);
  if (quote) { var q = el("blockquote", "dc-quote"); q.appendChild(el("b", "", quote.name)); q.appendChild(el("span", "", quote.text)); content.appendChild(q); }
  content.appendChild(el("div", "body", deleted ? "Message deleted" : str(m.text)));
  if (m.edited && !deleted) { content.appendChild(el("small", "muted", "edited")); }
  if (m.pinned && !deleted) { content.appendChild(el("small", "dc-pin", "Pinned")); }
  if (!deleted) {
    var reactions = el("div", "dc-reactions");
    reactionBar(m.reactions, DISCORD.human.guid, name).forEach(function (r) {
      var b = button(r.emoji + " " + r.count, "dc-reaction"); b.title = r.who;
      b.setAttribute("aria-pressed", String(r.mine)); b.setAttribute("aria-label", r.emoji + ": " + r.who);
      b.dataset.action = r.mine ? "unreact" : "react-emoji"; b.dataset.emoji = r.emoji;
      b.dataset.message = m.id; b.dataset.focus = m.id + ":" + r.emoji;
      b.disabled = !!discordReason() || discordBusy; reactions.appendChild(b);
    }); content.appendChild(reactions);
    var actions = el("div", "dc-message-actions");
    [["reply", "Reply"], ["react", "React"], [m.pinned ? "unpin" : "pin", m.pinned ? "Unpin" : "Pin"], ["thread", "Thread"]]
      .concat(mine ? [["edit", "Edit"], ["delete", "Delete"]] : []).forEach(function (a) {
        var b = button(a[1]); b.dataset.action = a[0]; b.dataset.message = m.id; b.dataset.focus = m.id + ":" + a[0];
        b.setAttribute("aria-label", a[1] + " message by " + name(m.author));
        b.disabled = !!discordReason() || discordBusy; actions.appendChild(b);
      }); content.appendChild(actions);
    (data.threads || []).filter(function (t) { return t.parent === m.id; }).forEach(function (t) {
      var b = button(t.name + " · " + t.count + " replies", "dc-thread-link"); b.dataset.thread = t.id;
      b.dataset.focus = "thread-" + t.id; content.appendChild(b);
    });
  }
  row.appendChild(content); return row;
}
function renderPanel(data) {
  var target = $("dc-panel"), key = focusKey(target); clear(target); target.hidden = !panel;
  $("dc-pins").setAttribute("aria-expanded", String(panel === "pins"));
  $("dc-threads").setAttribute("aria-expanded", String(panel === "threads"));
  if (!panel) { return; }
  target.appendChild(el("h4", "", panel === "pins" ? "Pinned messages" : "Threads"));
  var list = data ? data[panel] || [] : [];
  if (!list.length) { target.appendChild(el("p", "muted", panel === "pins" ? "No pinned messages here." : "No threads here yet. Start one from a message.")); }
  list.forEach(function (item) {
    if (panel === "threads") {
      var b = button(item.name + " · " + item.count + " replies", "dc-thread-link"); b.dataset.thread = item.id; b.dataset.focus = "thread-" + item.id; target.appendChild(b);
    } else {
      var p = el("blockquote", "dc-quote"); p.appendChild(el("b", "", name(item.author))); p.appendChild(el("span", "", item.text));
      target.appendChild(p);
    }
  }); restore(target, key);
}
function renderMessages(data) {
  var signature = JSON.stringify([shown, thread, data, entryMark, discordBusy, discordReason(), DISCORD.human]);
  if (signature === drawing) { return; }
  var key = focusKey(box), top = box.scrollTop, bottom = box.scrollHeight - top - box.clientHeight < 40;
  var newView = !drawing; drawing = signature; clear(box);
  if (!data) { box.appendChild(el("p", "empty muted", DISCORD.error || "Loading messages…")); return; }
  if (!data.messages.length) { box.appendChild(el("p", "empty muted", "Nothing said here yet. Say hello.")); }
  var divided = false;
  groupMessages(data.messages).forEach(function (item) {
    if (!divided && entryMark && item.message.id > entryMark && item.message.origin !== "human") {
      box.appendChild(el("div", "dc-new", "New messages")); divided = true; item.grouped = false;
    }
    box.appendChild(messageRow(item, data));
  });
  if (data.messages.length === 100) { box.prepend(el("p", "hint", "Showing the latest 100 messages.")); }
  box.scrollTop = newView || bottom ? box.scrollHeight : top; restore(box, key);
}
function renderComposer() {
  var reason = discordReason(), count = Array.from(input.value).length;
  input.disabled = !!reason; input.readOnly = discordBusy;
  $("dc-send").disabled = !!reason || discordBusy || !input.value.trim() || count > 2000;
  $("dc-emoji").disabled = !!reason || discordBusy;
  $("dc-count").textContent = count + " / 2000"; $("dc-count").classList.toggle("bad", count > 2000);
  $("dc-to").textContent = DISCORD.title(currentView()) + (thread ? " thread" : "");
  $("dc-why").textContent = failure || reason || (discordBusy ? "Saving…" : "Enter sends · Shift+Enter starts a new line");
  $("dc-reply").hidden = !reply;
  $("dc-reply-name").textContent = reply ? "Replying to " + name(reply.author) + ": " + str(reply.text).slice(0, 100) : "";
}
function render() {
  var yes = active(); $("dc-tools").hidden = !yes; $("dc-composer").hidden = !yes; $("composer").hidden = yes;
  if (!yes) { $("dc-panel").hidden = true; renderMembers(); return; }
  var data = windowData(), t = data && (data.threads || []).find(function (x) { return x.id === thread; });
  $("feed-title").textContent = DISCORD.title(currentView()) + (t ? " / " + t.name : "");
  var channel = DISCORD.channels().find(function (c) { return c.id === channelOf(currentView()); });
  $("feed-hint").textContent = DISCORD.error || (channel && channel.topic) || "A private conversation between you and this member.";
  $("dc-back").hidden = !thread;
  renderMessages(data); renderPanel(data); renderComposer(); renderMembers();
  if (data && !thread && document.visibilityState === "visible" && S.tab === "comms") { DISCORD.markRead(currentView()); saveDiscordRead(); }
  renderLists(); emit("discord-read");
}
function onView() {
  var next = currentView();
  if (shown !== next) { keepDraft(); shown = next; thread = 0; panel = ""; drawing = ""; entryMark = DISCORD.read[next] || 0; loadDraft(); }
  watchDiscord(active() ? next : "", thread); render();
}
function message(id) { var data = windowData(); return data && data.messages.find(function (m) { return m.id === id; }); }
function fail(error) { failure = error.message || "That change was not saved."; renderComposer(); emit("note", failure); }
function send() {
  if (!active() || $("dc-send").disabled) { return; }
  var text = input.value, key = routeKey(), body = { action: "post", channel: channelOf(shown), text: text };
  if (thread) { body.thread = thread; } if (reply) { body.reply_to = reply.id; }
  failure = "";
  mutateDiscord(body).then(function () {
    drafts.delete(key); replyDrafts.delete(key);
    if (key === routeKey() && input.value === text) { input.value = ""; reply = null; renderComposer(); input.focus(); }
  }).catch(fail);
}
function openDialog(mode, m, trigger) {
  dialog = { mode: mode, message: m }; returnTo = trigger || document.activeElement;
  $("dc-dialog-title").textContent = { emoji: "Add an emoji", react: "React to this message", edit: "Edit your message", thread: "Start a thread", delete: "Delete your message?" }[mode];
  $("dc-dialog-error").textContent = "";
  $("dc-dialog-value").value = mode === "edit" ? m.text : "";
  $("dc-dialog-value").hidden = mode === "delete" || mode === "emoji";
  $("dc-dialog-label").textContent = mode === "thread" ? "Thread name (up to 60 characters)" : mode === "react" ? "Or paste one emoji" : "Message (up to 2000 characters)";
  $("dc-dialog-label").hidden = mode === "delete" || mode === "emoji";
  $("dc-dialog-submit").hidden = mode === "emoji";
  $("dc-dialog-submit").textContent = mode === "delete" ? "Delete message" : "Save";
  $("dc-delete-hint").hidden = mode !== "delete";
  var emojis = $("dc-emoji-groups"); clear(emojis); emojis.hidden = mode !== "emoji" && mode !== "react";
  if (!emojis.hidden) { PICKER.forEach(function (g) {
    emojis.appendChild(el("h4", "", g.name)); var grid = el("div", "dc-emoji-grid");
    g.emoji.forEach(function (emoji) { var b = button(emoji); b.dataset.pickEmoji = emoji; b.setAttribute("aria-label", emoji); grid.appendChild(b); }); emojis.appendChild(grid);
  }); }
  $("dc-dialog").showModal();
  if (!$("dc-dialog-value").hidden) { $("dc-dialog-value").focus(); }
}
function insertEmoji(emoji) {
  var start = input.selectionStart, end = input.selectionEnd;
  input.setRangeText(emoji, start, end, "end"); $("dc-dialog").close(); renderComposer(); input.focus();
}
function submitDialog(emoji) {
  if (!dialog || discordBusy) { return; }
  var mode = dialog.mode, value = emoji || $("dc-dialog-value").value, m = dialog.message;
  if (mode === "emoji") { insertEmoji(value); return; }
  if (mode === "react" && (!isOneEmoji(value) || Array.from(value).length > 16)) { $("dc-dialog-error").textContent = "Choose or paste one emoji."; return; }
  if (mode === "thread" && (!value.trim() || Array.from(value.trim()).length > 60)) { $("dc-dialog-error").textContent = "Use a thread name of 1 to 60 characters."; return; }
  var body = { action: mode, message: m.id };
  if (mode === "react") { body.emoji = value; } if (mode === "edit") { body.text = value; } if (mode === "thread") { body.name = value; }
  $("dc-dialog-submit").disabled = true;
  mutateDiscord(body).then(function (j) { $("dc-dialog").close(); if (mode === "thread") { chooseThread(j.id); } })
    .catch(function (error) { $("dc-dialog-error").textContent = error.message; })
    .finally(function () { $("dc-dialog-submit").disabled = false; });
}
function action(ev) {
  var target = ev.target.closest("[data-action], [data-thread], [data-dm]"); if (!target) { return; }
  if (target.dataset.dm) { openView("dm:" + target.dataset.dm); input.focus(); return; }
  if (target.dataset.thread) { chooseThread(Number(target.dataset.thread)); return; }
  var m = message(Number(target.dataset.message)), act = target.dataset.action; if (!m) { return; }
  if (act === "reply") { reply = m; renderComposer(); input.focus(); }
  else if (act === "thread") {
    var existing = (windowData().threads || []).find(function (t) { return t.parent === m.id; });
    if (existing) { chooseThread(existing.id); } else { openDialog(act, m, target); }
  } else if (["react", "edit", "delete"].indexOf(act) >= 0) { openDialog(act, m, target); }
  else { var body = { action: act === "react-emoji" ? "react" : act, message: m.id }; if (target.dataset.emoji) { body.emoji = target.dataset.emoji; } mutateDiscord(body).catch(fail); }
}
$("tab-comms").addEventListener("click", action);
$("dc-send").addEventListener("click", send);
input.addEventListener("input", function () { failure = ""; renderComposer(); });
input.addEventListener("keydown", function (ev) { if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); if (!ev.repeat) { send(); } } });
$("dc-cancel-reply").addEventListener("click", function () { reply = null; renderComposer(); input.focus(); });
$("dc-emoji").addEventListener("click", function (ev) { openDialog("emoji", null, ev.currentTarget); });
$("dc-back").addEventListener("click", function () { chooseThread(0); });
$("dc-pins").addEventListener("click", function () { panel = panel === "pins" ? "" : "pins"; renderPanel(windowData()); });
$("dc-threads").addEventListener("click", function () { panel = panel === "threads" ? "" : "threads"; renderPanel(windowData()); });
$("dc-dialog-cancel").addEventListener("click", function () { $("dc-dialog").close(); });
$("dc-dialog-close").addEventListener("click", function () { $("dc-dialog").close(); });
$("dc-dialog-form").addEventListener("submit", function (ev) { ev.preventDefault(); submitDialog(); });
$("dc-emoji-groups").addEventListener("click", function (ev) { var b = ev.target.closest("[data-pick-emoji]"); if (b) { submitDialog(b.dataset.pickEmoji); } });
$("dc-dialog").addEventListener("close", function () {
  dialog = null; var key = returnTo && returnTo.dataset.focus;
  if (returnTo && returnTo.isConnected) { returnTo.focus({ preventScroll: true }); } else if (key) { restore(box, key); }
});
on("discord-open", onView);
on("feed", function () { if (!active()) { onView(); } });
on("discord-view", function () { render(); renderLists(); });
on("status", render);
on("comms-show", onView);
