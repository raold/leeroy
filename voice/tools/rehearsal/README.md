# Cross-service regression rehearsal

This headless test connects the real mind `serve()`, voice extension/client and
`VoiceService` over ephemeral loopback TCP. It also exercises the voice UI WebSocket.
It reuses the guarded test fixtures: an invented cast, FakeBridge, FakeLLM,
FakeEngine, FakeRecognizer and fake audio devices, volume sessions and keys.

Use existing environments with each package's `[test]` dependencies installed:
mind on Python 3.14 or later, voice on Python 3.12. No models or audio extras are needed.
From the checkout root, for example on Linux:

```bash
"$MIND_VENV/bin/python" -B voice/tools/rehearsal/run_rehearsal.py \
  --worktree "$PWD" \
  --mind-python "$MIND_VENV/bin/python" \
  --voice-python "$VOICE_VENV/bin/python" \
  --out /tmp/leeroy-voice-rehearsal
```

On Windows, pass the corresponding `Scripts/python.exe` paths. Interpreters can
come from another checkout: explicit `PYTHONPATH` and assertions verify that both
processes import this checkout's code and test fixtures.

Each run creates a fresh directory under `--out`, which must be outside the
checkout. It retains only synthetic databases, specs, caches, pytest logs and a
machine-readable `summary.json`. The runner returns nonzero on failed assertions,
child failures or timeouts, stops both children, and checks that all four temporary
ports have closed before reporting success. The `voice-rehearsal` CI job runs it
in two separate environments; the default unit suites remain unchanged.

The assertions cover:

- Online room/spec snapshots and actual fake audio playback, with `Played`
  acknowledgements persisted by the mind.
- Party room assignment, AFK return, disband, and member logout/relogin through
  real bridge events and both services.
- UI room changes through both real services and push-to-talk transcription
  from a fake microphone/recognizer into the mind's database.
- Logout during playback and PTT: cut acknowledgement, stale transcript
  suppression, closed fake streams, disarmed keys and restored fake volume.
- Typed bridge fallback after voice exits, zero Ollama connections, and clean
  process teardown.
- The attached nightly scheduler's human lease across login, logout and relogin,
  with fake model administration and no overnight run during the grace period.

This is **regression coverage, not live device acceptance**. It does not test
copied/live database migration, real model quality, physical audio, GPU admission,
WoW focus/elevation or Windows communications ducking. The mind leg includes the
nightly extension; the guild-life extension is not attached yet.

Random hot-mic and elsewhere conversation policies are disabled in this transport
rehearsal, and movement uses a fixed random seed. Their policy and send-time
validation are covered by the mind's unit suite.
