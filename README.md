# leeroy

[![ci](https://github.com/raold/leeroy/actions/workflows/ci.yml/badge.svg)](https://github.com/raold/leeroy/actions/workflows/ci.yml)
[![patches](https://github.com/raold/leeroy/actions/workflows/patches.yml/badge.svg)](https://github.com/raold/leeroy/actions/workflows/patches.yml)
[![build](https://github.com/raold/leeroy/actions/workflows/build.yml/badge.svg)](https://github.com/raold/leeroy/actions/workflows/build.yml)

A single-player World of Warcraft 3.3.5a (Wrath of the Lich King) server: one
human, a world full of [playerbots](https://github.com/mod-playerbots/mod-playerbots),
and raids filled with bots on demand. Bots talk through a local LLM
([mod-ollama-chat](https://github.com/DustinHendrickson/mod-ollama-chat)).

This repo is the customization layer on top of the
[mod-playerbots fork of AzerothCore](https://github.com/mod-playerbots/azerothcore-wotlk):
maintenance scripts, the tuned configs, and local patches. It contains no
Blizzard data and no core source.

The goal it is named for: a bot raid leader. A tank that knows the route,
calls the pulls, and runs the encounter, so a lone mage can follow it through
Icecrown the way you once followed a good raid lead. Not built yet.

## Layout

| Path | What |
|---|---|
| `scripts/` | Update, backup, build, start/stop, and snapshot scripts (Git Bash on Windows) |
| `config/` | Snapshots of the live server and module configs, secrets masked |
| `patches/mod-playerbots/` | Local playerbots commits, as `git format-patch` output |
| `patches/mod-ollama-chat/` | Local mod-ollama-chat commits, same format |
| `modules/mod-leeroy/` | The guild module: a local bridge (NDJSON over `127.0.0.1:7878`) that lets an outside "mind" see the guild and speak for its members, with unit tests and a smoke test in `tools/` |
| `mind/` | The mind: a Python service (`python -m leeroy_mind run`) that gives the guild's members personas, memories and their voice in guild chat, through the bridge and a local LLM (Ollama) |
| `voice/` | Local voice comms and push-to-talk (`python -m leeroy_voice`), in a separate Python 3.12 process; GPL-3.0-or-later, with its own [LICENSE](voice/LICENSE) |
| `CLAUDE.md` | Operating notes: paths, build traps, module quirks, what each patch does |

## Patches

- **Raid Browser listings**: idle randombots list themselves in the Raid
  Browser, so it is not empty.
- **`.playerbots bot add5|add10|add25|add40`**: fills your group to a 5-man
  party or a 10/25/40-man raid with a sensible tank/healer/damage mix at your
  level, with proper specs and gear.
- **Raid habits**: those bots follow in arrow formation, and damage dealers
  hold a few seconds into a pull so the tank gets threat.
- **Pinned random bots** (playerbots): an API other modules use to keep chosen
  random bots permanent and online, never re-rolled, teleported or pulled out
  of their group. mod-leeroy pins the guild with it.
- **Speaker filter** (mod-ollama-chat): lets another module keep chosen bots
  out of the LLM chat, so the guild speaks only through its own mind.

Apply them to a checkout of each module with
`git am /path/to/leeroy/patches/<module>/*.patch`.

## Scripts

```bash
./ac-update.sh [--dry-run]   # pull core + git modules, rebasing local patches
./ac-backup.sh [label]       # mysqldump all four databases
./ac-build.sh                # preflight checks, configure, build
./ac-server.sh start|stop|status
./ac-voice.sh start|stop|status # voice service; follows LEEROY_VOICE_DATA and voice.toml
./ac-snapshot.sh             # copy live configs and module patches into this repo
```

They assume this repo is the install root, with the core checked out in
`azerothcore-wotlk/` and the build in `build/`. See `CLAUDE.md`.

## CI

| Workflow | When | What |
|---|---|---|
| `ci` | main pushes and pull requests | shellcheck on `scripts/`, gitleaks over the full history, mod-leeroy unit tests, the mind's Python 3.14 suite, the `voice` job's Python 3.12 suite, and a headless cross-service rehearsal in separate environments |
| `patches` | patch changes, nightly | applies the patches to the newest upstream mod-playerbots (plus its codestyle check) and mod-ollama-chat; a nightly failure opens a `patch-drift` issue |
| `build` | patch or module changes, weekly | compiles the fork core with the patched playerbots and mod-leeroy (clang, `-Werror`, ccache) |
| `release` | `v*` tag | GitHub release with the patches, modules, configs and scripts |

There is no deploy job: the server runs on one PC and is updated with the
scripts above. A self-hosted runner on a public repo would let any pull request
run code on that PC.

## Voice

The voice service adds local room playback, per-member voices, mic effects,
push-to-talk recognition and a browser control panel. Its default loopback ports
are 7879 for the mind and 7880 for the panel. Runtime settings and models stay in
the ignored `voice-data/`; voice specs and synthetic references stay in
`mind-data/voices/`. See [the operating notes](CLAUDE.md#the-voice-service-leeroy-voice)
for commands, data-directory overrides and the rollout procedure.

Chatterbox Turbo can optionally use the CPU when GPU admission refuses it. In the
local `voice.toml`, set `[tts] cpu_fallback = true`; `cpu_threads` defaults to 12
and is capped at 24. Cold loading runs in the background. One direct reply or private
conversation can render on the CPU at a time, with a 15-second maximum shortened by
its expiry. Ambient chatter, urgent calls, overflow, and failed renders retain
stock/Kokoro delivery. This option
keeps the same synthetic references and does not reserve GPU memory; it trades
response speed for the expressive voice. It is disabled by default.

This branch's voice work is in development. A [headless regression rehearsal](voice/tools/rehearsal/README.md)
checks the two services together using synthetic data and fake audio devices.
Rehearsal with a copied database and real devices, deployment, and acceptance
during a normal game session are still pending.

Voice dependencies include pedalboard (GPL-3.0) and
[eSpeak NG](https://github.com/espeak-ng/espeak-ng/blob/master/COPYING)
(GPL-3.0, loaded through espeakng-loader). The model pins in
[models.py](voice/leeroy_voice/models.py) record revisions and licenses:
Kokoro-82M (Apache-2.0), NVIDIA Parakeet TDT 0.6B v2 (CC-BY-4.0),
Silero VAD (MIT), SpeechBrain ECAPA (Apache-2.0), and the optional
Whisper large-v3-turbo and Chatterbox Turbo models (MIT).

## License

GPL-2.0, matching AzerothCore and mod-playerbots, except the separate `voice/`
service, which is GPL-3.0-or-later under [voice/LICENSE](voice/LICENSE).
