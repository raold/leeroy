# leeroy-voice

The guild's voice comms. The mind (`../mind`) decides who says what; this service speaks the
members' lines aloud in Ventrilo-style rooms, each member in a voice of their own, and turns the
player's push-to-talk into transcripts for the mind. It holds no persona logic and never talks to
the game: if it is down, the guild types in chat as before.

Everything runs on one PC and listens on loopback only: `127.0.0.1:7879` for the mind (one mind
at a time, newline-delimited JSON, voice protocol v1 in `leeroy_voice/wire.py`) and
`127.0.0.1:7880` for its own web page. Speech is synthesized on the CPU by default. The
push-to-talk audio is not written to disk unless the debug recording is switched on (it is off,
and keeps clips 24 hours), and the log holds ids, counts and timings, never what was said. Every
voice is a blend of synthetic stock voices: none is cloned from a real person.

## Running it

It needs Python 3.12 (misaki and kokoro-onnx do not support 3.13 or later), in its own venv:

```bash
cd voice
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e ".[audio,tts,stt,test]"
.venv/Scripts/python -m leeroy_voice doctor
.venv/Scripts/python -m pytest -q
```

Its settings and data live outside the repo, in `LEEROY_VOICE_DATA` (default: `voice-data/`
beside the repo): `voice.toml` (ports, devices, push-to-talk, sound), `decision.toml` and
`ui-state.json`. The defaults name no audio device: sound plays on the Windows default device,
and no microphone opens until `voice.toml` names one.

The default test run needs no audio device, GPU, model file, network or running service. Tests
that do are marked `engines`, `device`, `windows`, `gpu`, `ollama` or `live` and deselected
unless asked for (`pytest -m device`).

For a cast already configured for Chatterbox Turbo, `voice.toml` can opt into CPU rendering
when GPU admission refuses the model:

```toml
[tts]
cpu_fallback = true
cpu_threads = 12
cpu_timeout_s = 15.0
```

This fallback is limited to direct replies, private-room conversation and whispers (p3).
Ambient chatter and urgent lines continue through cached clips or Kokoro, preserving the warm
Turbo worker for the next direct reply. CPU fallback is disabled by default; its thread budget
is capped at 24. Loading and a discarded neutral warmup happen in the background within a
60-second startup budget. One CPU reply runs at a time; other lines use Kokoro while it is busy.
Synthesis has a maximum 15-second limit, shortened to leave seven seconds before the existing
line expiry for cleanup and fallback. CPU use is reported separately from GPU residency and
does not pin the chat models in VRAM.

## License

GPL-3.0-or-later (`LICENSE`), unlike the rest of this repository, which is GPL-2.0. This
program imports pedalboard and loads espeak-ng, both GPL-3.0, so it is GPL-3.0 as well. It is a
separate program from the mind, which talks to it over a local TCP socket.

## Credits

Libraries whose licence this one follows:

- [pedalboard](https://github.com/spotify/pedalboard) by Spotify, GPL-3.0: the microphone and
  codec effects.
- [espeak-ng](https://github.com/espeak-ng/espeak-ng), GPL-3.0, loaded through
  [espeakng-loader](https://github.com/thewh1teagle/espeakng-loader): phonemes for words the
  grapheme-to-phoneme model does not know.

Models, downloaded at pinned revisions and never stored in this repository:

- [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) by hexgrad, Apache-2.0, run through
  [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx): the speech.
- [Parakeet TDT 0.6B v2](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2) by NVIDIA,
  [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/), in the int8 ONNX export by
  [istupakov](https://huggingface.co/istupakov/parakeet-tdt-0.6b-v2-onnx), unmodified: the
  push-to-talk recognizer.
- [Silero VAD](https://github.com/snakers4/silero-vad) by Silero, MIT: the speech gate.
- [Whisper large-v3-turbo](https://huggingface.co/openai/whisper-large-v3-turbo) by OpenAI, MIT,
  in the CTranslate2 conversion for faster-whisper
  ([dropbox-dash](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo)): the
  optional recognizer on the GPU.

The other dependencies and their exact versions are pinned in `pyproject.toml`.
