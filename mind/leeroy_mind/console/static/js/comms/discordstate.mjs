import { api, emit, on, post, recall, refused, remember, str } from "../core.mjs";
import { S } from "../state.mjs";
import { Discord, channelOf, mergeReadMarks } from "./discord.mjs";

export var DISCORD = new Discord(recall("guild-console-read", {}));
export var discordBusy = false;
var view = "", thread = 0, request = null, queued = false, timer = null, opened = false, keyLost = false;

export function saveDiscordRead() {
  remember("guild-console-read", mergeReadMarks(recall("guild-console-read", {}), DISCORD.read));
}
export function watchDiscord(next, tid) {
  var changed = view !== next || thread !== (tid || 0);
  view = next; thread = tid || 0;
  if (changed) { refreshDiscord(); }
}
export function discordReason() {
  if (keyLost) { return "Open the console's printed URL again to restore its key."; }
  if (!DISCORD.loaded) { return DISCORD.error || "Connecting to the guild's Discord…"; }
  if (DISCORD.missing.length) { return "The mind needs the Discord update before messages can be saved."; }
  if (S.status.control && S.status.control.enabled === false) { return "Discord is read-only in this console."; }
  if (!S.status.mind || S.status.mind.state !== "running") { return "The mind must be running to save a message."; }
  return "";
}
export function refreshDiscord() {
  if (keyLost || !opened || document.visibilityState !== "visible") { return Promise.resolve(); }
  if (request) { queued = true; return request; }
  var channel = channelOf(view), tid = thread;
  // Discover the current layout before sending persisted marks: removed channels or
  // corrupt browser convenience data must never prevent the server from loading.
  var first = !DISCORD.loaded;
  var query = "?read=" + encodeURIComponent(JSON.stringify(first ? {} : DISCORD.marks()));
  if (channel) { query += "&channel=" + encodeURIComponent(channel); }
  if (tid) { query += "&thread=" + tid; }
  request = api("/api/discord" + query).then(function (r) {
    if (refused(r)) { return null; }
    if (!r.ok) { throw new Error("The guild's Discord could not be read. Retrying shortly."); }
    return r.json();
  }).then(function (data) {
    if (data) { DISCORD.adopt(data, tid); saveDiscordRead(); if (first) { queued = true; } }
  }).catch(function (error) {
    DISCORD.error = str(error.message) || "The guild's Discord could not be read.";
  }).finally(function () {
    request = null; emit("discord-view");
    if (queued) { queued = false; refreshDiscord(); }
  });
  return request;
}
export function mutateDiscord(body) {
  if (discordBusy || discordReason()) { return Promise.reject(new Error(discordReason() || "One change is still being saved.")); }
  discordBusy = true; emit("discord-view");
  return post("/api/discord", body).then(function (j) {
    if (!j.ok) { throw new Error(str(j.error) || "That change was not saved."); }
    return j;
  }, function () { throw new Error("The console did not answer. Check the messages before trying again."); })
    .finally(function () { discordBusy = false; refreshDiscord(); emit("discord-view"); });
}
function start() {
  opened = true; clearInterval(timer); refreshDiscord();
  timer = setInterval(refreshDiscord, 30000); // keeps the console's 60-second typing interest alive
}
on("comms-show", start);
on("comms-hide", function () { opened = false; clearInterval(timer); timer = null; });
on("snapshot", function () { keyLost = false; if (S.tab === "comms") { start(); } });
on("key-lost", function () { keyLost = true; clearInterval(timer); emit("discord-view"); });
on("discord", refreshDiscord);
document.addEventListener("visibilitychange", function () {
  if (document.visibilityState === "visible" && opened) { refreshDiscord(); }
});
