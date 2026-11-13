import { $, api, on, refused } from "../core.mjs";
import { NOTE_SECONDS, VoiceRecorder, mono, resample, wav, noteTime } from "./voicenotes.mjs";

const drafts = new Map();
let recorder, owner = '', options, visible = '';

async function decode(blob) {
  const Audio = window.AudioContext || window.webkitAudioContext;
  if (!Audio) { throw new Error('This browser cannot prepare a voice note.'); }
  const context = new Audio();
  try {
    const audio = await context.decodeAudioData(await blob.arrayBuffer());
    const frames = Math.min(audio.length, Math.floor(audio.sampleRate * NOTE_SECONDS));
    const channels = Array.from({ length:audio.numberOfChannels }, (_, i) => audio.getChannelData(i).subarray(0, frames));
    return new Blob([wav(resample(mono(channels), audio.sampleRate))], { type:'audio/wav' });
  } finally { await context.close(); }
}

export function voiceDraft(key) { return drafts.get(key) || null; }
export function voicePreparing(key) { return owner === key && !!recorder?.busy; }
export function clearVoice(key, expected) {
  const draft = drafts.get(key);
  if (draft && (!expected || expected === draft)) { URL.revokeObjectURL(draft.url); drafts.delete(key); }
}
export async function prepareVoice(draft) {
  if (!draft) { return null; }
  if (draft.token) { return draft.token; }
  const response = await api('/api/discord/voice', { method:'POST', body:draft.blob,
    headers:{ 'Content-Type':'audio/wav' }, timeoutMs:65000 });
  if (refused(response)) { throw new Error('Open the console with its key before uploading.'); }
  const answer = await response.json();
  if (!response.ok || !/^[a-f0-9]{32}$/.test(answer.token || '')) {
    throw new Error(answer.error || 'The voice note could not be uploaded. Your draft is still here.');
  }
  draft.token = answer.token; return draft.token;
}

export function renderVoiceDraft(key, enabled, busy) {
  if (visible !== key) {
    visible = key;
    if (recorder?.busy) { recorder.cancel(); }
    $('dc-voice-preview-audio').pause();
  }
  if (!enabled && recorder?.busy) { recorder.cancel(); }
  const own = owner === key, phase = own ? recorder?.phase : 'idle';
  const preparing = own && recorder?.busy, draft = enabled && voiceDraft(key);
  const button = $('dc-voice-record');
  button.hidden = !enabled;
  button.disabled = busy || phase === 'encoding' || !!draft;
  button.textContent = phase === 'recording' ? 'Stop recording' : phase === 'asking' ? 'Cancel microphone request' : 'Record voice note';
  button.setAttribute('aria-pressed', String(phase === 'recording'));
  $('dc-voice-timer').hidden = !preparing;
  $('dc-voice-timer').textContent = phase === 'asking' ? 'Waiting for microphone permission…' :
    phase === 'encoding' ? 'Preparing your voice note…' : noteTime(recorder.seconds) + ' / 1:00 · Escape cancels';
  const audio = $('dc-voice-preview-audio');
  $('dc-voice-preview').hidden = !draft;
  if (draft) {
    if (audio.getAttribute('src') !== draft.url) { audio.src = draft.url; }
  } else if (audio.hasAttribute('src')) { audio.pause(); audio.removeAttribute('src'); audio.load(); }
  $('dc-voice-discard').disabled = busy;
}

export function setupVoiceComposer(config) {
  options = config;
  recorder = new VoiceRecorder({
    capture:() => {
      if (!window.isSecureContext) { throw new Error('Microphone access needs the local console or its HTTPS address.'); }
      if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
        throw new Error('This browser cannot record voice notes.');
      }
      return navigator.mediaDevices.getUserMedia({ audio:true });
    },
    makeRecorder:stream => new MediaRecorder(stream), decode,
    changed:state => {
      if (state.phase === 'preview' && state.blob && drafts.get(owner)?.blob !== state.blob) {
        clearVoice(owner);
        drafts.set(owner, { blob:state.blob, url:URL.createObjectURL(state.blob), token:null });
      }
      if (state.error) { options.failed(new Error(state.error)); }
      options.changed();
    }
  });
  $('dc-voice-record').addEventListener('click', () => {
    if (!options.enabled()) { return; }
    if (recorder.phase === 'recording') { recorder.stop(); return; }
    if (recorder.phase === 'asking') { recorder.cancel(); return; }
    if (recorder.busy) { return; }
    if (drafts.size >= 8 && !drafts.has(options.key())) {
      options.failed(new Error('Discard or send a saved voice draft before recording another.')); return;
    }
    owner = options.key(); clearVoice(owner); recorder.start();
  });
  $('dc-voice-discard').addEventListener('click', () => {
    clearVoice(options.key());
    if (owner === options.key()) { recorder.cancel(); }
    options.changed(); $('dc-voice-record').focus();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && recorder.busy) { event.preventDefault(); recorder.cancel(); }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && recorder.busy) { recorder.cancel(); }
  });
  on('key-lost', () => { recorder.cancel(); for (const key of drafts.keys()) { clearVoice(key); } options.changed(); });
  on('comms-hide', () => { if (recorder.busy) { recorder.cancel(); } $('dc-voice-preview-audio').pause(); });
  window.addEventListener('pagehide', () => { recorder.cancel(); for (const key of drafts.keys()) { clearVoice(key); } });
}
