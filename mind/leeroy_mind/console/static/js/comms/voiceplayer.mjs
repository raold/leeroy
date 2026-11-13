import { api, el, on, refused } from "../core.mjs";
import { audioUrl, playerState } from "./voicenotes.mjs";

// Keep the playing element through ordinary feed redraws. A changed conversation or deleted
// message drops it immediately; no key, transcript or recording is stored in browser storage.
const players = new Map();
let active = null;
function drop(player) {
  player.generation++;
  player.request?.abort(); player.request = null; player.audio.pause();
  if (player.url) { URL.revokeObjectURL(player.url); player.url = null; }
  player.audio.removeAttribute('src'); player.audio.load();
  if (active === player) { active = null; }
}
export function retainVoicePlayers(ids) {
  const keep = new Set(ids);
  for (const [id, player] of players) {
    if (!keep.has(id)) { drop(player); players.delete(id); }
  }
}
function trimCache(current) {
  const loaded = [...players.values()].filter(p => p.url && p !== current).sort((a,b) => a.used - b.used);
  while (loaded.length >= 8) { drop(loaded.shift()); }
}
function draw(player) {
  const model = playerState({ seconds:player.audio.currentTime, duration:player.ms / 1000,
    playing:!player.audio.paused, loading:!!player.request, error:player.error });
  player.play.textContent = model.label;
  player.play.disabled = model.disabled;
  player.play.setAttribute('aria-label', model.label + ' from ' + player.poster);
  player.seek.max = model.duration; player.seek.value = model.seconds;
  player.seek.disabled = !player.url;
  player.seek.setAttribute('aria-valuetext', model.elapsed + ' of ' + model.total);
  player.time.textContent = model.elapsed + ' / ' + model.total;
  player.status.textContent = model.error;
}
async function play(player) {
  if (player.request) { return; }
  if (!player.audio.paused) { player.audio.pause(); return; }
  player.error = '';
  const generation = ++player.generation;
  if (active && active !== player) { active.audio.pause(); }
  active = player;
  try {
    if (!player.url) {
      const request = new AbortController(); player.request = request; draw(player);
      const response = await api(audioUrl(player.id), { signal:request.signal });
      if (refused(response)) { throw new Error('Open the console with its key to play this note.'); }
      if (!response.ok) { throw new Error(response.status === 404 ? 'This voice note is unavailable.' : 'The voice note could not be loaded.'); }
      const blob = await response.blob();
      if (request.signal.aborted || players.get(player.id) !== player) { return; }
      trimCache(player); player.url = URL.createObjectURL(blob); player.audio.src = player.url;
      player.request = null;
    }
    player.used = performance.now();
    // A second play click may have selected another note while this one's bytes arrived.
    if (active === player) { await player.audio.play(); }
  } catch (error) {
    if (generation === player.generation && error.name !== 'AbortError' && players.get(player.id) === player) {
      player.error = error.name === 'NotAllowedError' ? 'Your browser blocked playback. Press Play to try again.' :
        error.message || 'The voice note could not be played.';
    }
  } finally { if (generation === player.generation) { player.request = null; draw(player); } }
}
export function voiceBox(message, poster, mine) {
  let player = players.get(message.id);
  if (!player) {
    const box = el('div', 'dc-voice-note'), controls = el('div', 'dc-voice-controls');
    const audio = el('audio'); audio.preload = 'none';
    const button = el('button'); button.type = 'button'; button.dataset.focus = message.id + ':voice';
    const seek = el('input'); seek.type = 'range'; seek.min = 0; seek.step = .1;
    seek.setAttribute('aria-label', 'Seek voice note from ' + poster); seek.dataset.focus = message.id + ':voice-seek';
    const time = el('span', 'mono'), status = el('p', 'hint'); status.setAttribute('role','status');
    controls.append(button, time, seek); box.append(audio, controls, status);
    const details = el('details'), summary = el('summary', '', 'Show transcript'), text = el('p', 'body');
    details.open = mine; summary.dataset.focus = message.id + ':voice-transcript';
    details.append(summary, text); box.append(details);
    details.addEventListener('toggle', () => { summary.textContent = details.open ? 'Hide transcript' : 'Show transcript'; });
    player = { id:message.id, box, audio, play:button, seek, time, status, text, poster,
      ms:message.voice_note.ms, error:'', url:null, request:null, used:0, generation:0 };
    players.set(message.id, player);
    button.addEventListener('click', () => play(player));
    seek.addEventListener('input', () => {
      if (player.url && Number.isFinite(audio.duration)) {
        audio.currentTime = Math.max(0, Math.min(audio.duration, Number(seek.value))); draw(player);
      }
    });
    for (const event of ['timeupdate','play','pause','ended','loadedmetadata']) { audio.addEventListener(event, () => draw(player)); }
    audio.addEventListener('error', () => {
      if (player.url) { player.error = 'This browser could not play the voice note.'; draw(player); }
    });
  }
  player.ms = message.voice_note.ms;
  player.text.textContent = message.voice_note.transcript_ok ? message.text : 'The transcript is not available.';
  draw(player); return player.box;
}
on('key-lost', () => { for (const p of players.values()) { drop(p); draw(p); } });
on('comms-hide', () => { for (const p of players.values()) { drop(p); draw(p); } });
document.addEventListener('visibilitychange', () => { if (document.hidden) { for (const p of players.values()) { p.audio.pause(); } } });
window.addEventListener('pagehide', () => retainVoicePlayers([]));
