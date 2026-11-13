// Audio conversion and recorder lifecycle, independent of the page and microphone hardware.
export const NOTE_SECONDS = 60, NOTE_RATE = 16000, NOTE_BYTES = 2 * 1024 * 1024;

export function mono(channels) {
  if (!Array.isArray(channels) || !channels.length || channels.length > 32 ||
      !channels[0]?.length || channels.some(c => !(c instanceof Float32Array) || c.length !== channels[0].length)) {
    throw new Error('The recording could not be decoded.');
  }
  const out = new Float32Array(channels[0].length);
  for (const channel of channels) {
    for (let i = 0; i < out.length; i++) {
      if (!Number.isFinite(channel[i])) { throw new Error('The recording contains invalid samples.'); }
      out[i] += channel[i] / channels.length;
    }
  }
  return out;
}

export function resample(samples, from, to = NOTE_RATE) {
  if (!(samples instanceof Float32Array) || !samples.length ||
      !Number.isInteger(from) || from < 8000 || from > 192000 ||
      !Number.isInteger(to) || to < 8000 || to > 192000) {
    throw new Error('The recording has an unsupported sample rate.');
  }
  if (from === to) { return samples.slice(); }
  const ratio = from / to, count = Math.max(1, Math.round(samples.length / ratio));
  const out = new Float32Array(count);
  for (let i = 0; i < count; i++) {
    if (ratio > 1) {
      // Average each downsampling window rather than dropping individual samples.
      const start = i * ratio, end = Math.min(samples.length, (i + 1) * ratio);
      let sum = 0;
      for (let j = Math.floor(start); j < Math.ceil(end); j++) {
        sum += samples[j] * (Math.min(j + 1, end) - Math.max(j, start));
      }
      out[i] = sum / (end - start);
    } else {
      const at = i * ratio, left = Math.floor(at), right = Math.min(left + 1, samples.length - 1);
      out[i] = samples[left] + (samples[right] - samples[left]) * (at - left);
    }
  }
  return out;
}

export function wav(samples, rate = NOTE_RATE) {
  if (!(samples instanceof Float32Array) || rate !== NOTE_RATE ||
      samples.length < rate / 5 || samples.length > rate * NOTE_SECONDS) {
    throw new Error('Record between a moment and 60 seconds.');
  }
  const buffer = new ArrayBuffer(44 + samples.length * 2), view = new DataView(buffer);
  function word(at, text) { for (let i = 0; i < text.length; i++) { view.setUint8(at + i, text.charCodeAt(i)); } }
  word(0, 'RIFF'); view.setUint32(4, buffer.byteLength - 8, true); word(8, 'WAVE');
  word(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); word(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  samples.forEach((sample, i) => {
    if (!Number.isFinite(sample)) { throw new Error('The recording contains invalid samples.'); }
    const bounded = Math.max(-1, Math.min(1, sample));
    view.setInt16(44 + i * 2, Math.round(bounded * (bounded < 0 ? 32768 : 32767)), true);
  });
  return buffer;
}

export function audioUrl(id) {
  if (!Number.isSafeInteger(id) || id <= 0) { throw new Error('Unknown voice note.'); }
  return '/api/discord/voice?id=' + id;
}
export function noteTime(seconds) {
  const n = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  return Math.floor(n / 60) + ':' + String(n % 60).padStart(2, '0');
}
export function playerState({ seconds = 0, duration = 0, playing = false, loading = false, error = '' } = {}) {
  const total = Number.isFinite(duration) && duration > 0 ? duration : 0;
  const at = Math.min(total, Math.max(0, Number.isFinite(seconds) ? seconds : 0));
  return { seconds:at, duration:total, label:loading ? 'Loading voice note…' : playing ? 'Pause voice note' : 'Play voice note',
    elapsed:noteTime(at), total:noteTime(total), disabled:loading, error };
}

export class VoiceRecorder {
  constructor({ capture, makeRecorder, decode, changed = () => {}, now = () => performance.now(),
    every = (fn, ms) => setInterval(fn, ms), clear = id => clearInterval(id) }) {
    Object.assign(this, { capture, makeRecorder, decode, changed, now, every, clear });
    this.generation = 0; this.phase = 'idle'; this.error = ''; this.blob = null;
    this.stream = null; this.recorder = null; this.timer = null; this.started = 0;
  }
  get seconds() { return this.phase === 'recording' ? Math.min(NOTE_SECONDS, (this.now() - this.started) / 1000) : 0; }
  get busy() { return ['asking', 'recording', 'encoding'].includes(this.phase); }
  notify() { this.changed(this); }
  tracks() { this.stream?.getTracks().forEach(track => track.stop()); this.stream = null; }
  untick() { if (this.timer !== null) { this.clear(this.timer); this.timer = null; } }
  cancel() {
    this.generation++; this.untick();
    try { if (this.recorder && this.recorder.state !== 'inactive') { this.recorder.stop(); } }
    catch { /* The device may have stopped first; its tracks still need releasing. */ }
    finally { this.recorder = null; this.tracks(); }
    this.phase = 'idle'; this.error = ''; this.blob = null; this.notify();
  }
  fail(error, generation) {
    if (generation !== this.generation) { return; }
    this.cancel();
    this.error = error?.name === 'NotAllowedError' ? 'Microphone permission was refused. Allow it in your browser to record a note.' :
      error?.name === 'NotFoundError' ? 'No microphone is available.' : error?.message || 'The voice note could not be recorded.';
    this.notify();
  }
  async start() {
    if (this.busy) { return; }
    this.cancel(); const generation = this.generation;
    this.phase = 'asking'; this.notify();
    if (generation !== this.generation) { return; }
    try {
      const stream = await this.capture();
      if (generation !== this.generation) { stream.getTracks().forEach(track => track.stop()); return; }
      this.stream = stream;
      const recorder = this.makeRecorder(stream), chunks = []; let captured = 0;
      this.recorder = recorder;
      recorder.ondataavailable = event => {
        if (generation !== this.generation || !event.data.size) { return; }
        captured += event.data.size;
        if (captured > 32 * 1024 * 1024) { this.fail(new Error('The recording is too large. Try a shorter note.'), generation); }
        else { chunks.push(event.data); }
      };
      recorder.onerror = event => this.fail(event.error || new Error('Recording stopped unexpectedly.'), generation);
      recorder.onstop = async () => {
        if (generation !== this.generation) { return; }
        this.untick(); this.tracks(); this.phase = 'encoding'; this.notify();
        try {
          const blob = await this.decode(new Blob(chunks, { type:recorder.mimeType }));
          if (generation !== this.generation) { return; }
          if (!blob || blob.size > NOTE_BYTES) { throw new Error('The voice note is too large. Try a shorter recording.'); }
          this.blob = blob; this.phase = 'preview'; this.notify();
        } catch (error) { this.fail(error, generation); }
      };
      recorder.start(); this.started = this.now(); this.phase = 'recording'; this.notify();
      this.timer = this.every(() => { if (this.seconds >= NOTE_SECONDS) { this.stop(); } else { this.notify(); } }, 250);
    } catch (error) { this.fail(error, generation); }
  }
  stop() {
    if (this.phase !== 'recording') { return; }
    this.untick(); this.phase = 'encoding'; this.notify();
    try { this.recorder.stop(); } catch (error) { this.fail(error, this.generation); }
    this.tracks();
  }
}
