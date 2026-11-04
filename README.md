# leeroy

[![ci](https://github.com/raold/leeroy/actions/workflows/ci.yml/badge.svg)](https://github.com/raold/leeroy/actions/workflows/ci.yml)
[![patches](https://github.com/raold/leeroy/actions/workflows/patches.yml/badge.svg)](https://github.com/raold/leeroy/actions/workflows/patches.yml)
[![build](https://github.com/raold/leeroy/actions/workflows/build.yml/badge.svg)](https://github.com/raold/leeroy/actions/workflows/build.yml)

A single-player World of Warcraft 3.3.5a (Wrath of the Lich King) server: one
human, a world full of [playerbots](https://github.com/mod-playerbots/mod-playerbots),
and raids filled with bots on demand. The world's bots talk through a local LLM
([mod-ollama-chat](https://github.com/DustinHendrickson/mod-ollama-chat)). The
player's own guild, about a hundred level-80 bots kept online for good, is
different: each member has a persona, memories and relationships, and speaks
through the mind in this repo, in guild chat and, with the voice service, aloud.

This repo is the customization layer on top of the
[mod-playerbots fork of AzerothCore](https://github.com/mod-playerbots/azerothcore-wotlk):
the guild module and its mind, the voice service, maintenance scripts, the tuned
configs, and local patches. It contains no Blizzard data and no core source.

The goal it is named for: a bot raid leader. A tank that knows the route,
calls the pulls, and runs the encounter, so a lone mage can follow it through
Icecrown the way you once followed a good raid lead. Much of it exists now:
[mod-dungeon-clear](https://github.com/jrad7/mod-dungeon-clear) has the group's
tank bot route boss to boss and pull, and the mind's raid nights assemble the
raid from the guild and give it a raid leader who calls the plan before each
boss. The boss fights themselves are still played by playerbots' raid
strategies.

## Layout

| Path | What |
|---|---|
| `scripts/` | Update, backup, build, start/stop, voice and snapshot scripts (Git Bash on Windows) |
| `config/` | Snapshots of the live server and module configs, secrets masked |
| `patches/mod-playerbots/` | Local playerbots commits, as `git format-patch` output |
| `patches/mod-ollama-chat/` | Local mod-ollama-chat commits, same format |
| `modules/mod-leeroy/` | The guild module: keeps the guild's bots pinned online and their consumables topped up, and runs a local bridge (NDJSON over `127.0.0.1:7878`) through which an outside "mind" sees the guild and speaks and acts for its members. Unit tests, and a smoke test in `tools/` |
| `mind/` | The mind (`python -m leeroy_mind run`), a Python service that voices the guild through the bridge and a local LLM (Ollama); see [The mind](#the-mind) |
| `voice/` | Local voice comms and push-to-talk (`python -m leeroy_voice`), in a separate Python 3.12 process; GPL-3.0-or-later, with its own [LICENSE](voice/LICENSE) and [README](voice/README.md) |
| `references.bib` | The research, software and models behind the project, each entry annotated with what it shaped |
| `CLAUDE.md` | Operating notes: paths, build traps, module quirks, what each patch does |

## The mind

Each guild member has a persona card, memories with privacy scopes, moods and
relationships. The mind decides who speaks and when, and writes each line on the
local model in that member's own voice and typing style, without letting a
member say what they could not know.

- **Talk**: guild chat, whispers and replies to the player, plus reactions to
  raids, loot and what happens in game.
- **Guild life**: ask for a raid and the guild answers. It picks a fair
  composition, rotates the raid lead, forms and summons the raid, and runs it
  with mod-dungeon-clear while the leader calls each boss. Loot goes through
  master loot and DKP, and the raid votes an MVP. While the player is away the
  guild can run a raid of its own, one a day by default. Between raids it keeps
  ranks, specs and gear current, and raiders keep flask and feast habits.
- **Nights**: once the player is offline and the GPU is idle, the mind runs the
  night. It consolidates and forgets old memories, spreads gossip and corrects
  rumours, moves relationships, feuds and mentorships along, lets jokes,
  nicknames and slang spread and fade, and writes stories, raid recaps, a guild
  chronicle and season awards. It stops the moment the player logs in and
  resumes where it left off.
- **Weekly evaluation**: persona consistency, social behaviour, memory and
  secret-keeping, measured on scratch databases, never by sending anything in
  game.
- **Scholar library**: the [game library](mind/leeroy_mind/library/README.md)
  the mind reads for facts in its answers: warcraft.wiki.gg's WotLK pages,
  AtlasLoot, the world database and client data, plus pinned AzerothCore
  documentation snapshots and the server's installed command and teleport
  references, with text and vector search.
- **Guild console**: `python -m leeroy_mind console` serves one local browser
  window on `127.0.0.1:7881`: the voice rooms and the guild's chat in one feed,
  the mind's and the voice service's health, and their start, restart and stop.
  It runs apart from the mind, so it can start the mind again.

Personas, memories, prompts and logs stay in the ignored `mind-data/`. See
[the operating notes](CLAUDE.md#the-mind-service-leeroy_mind) for commands.

## Patches

- **Raid Browser listings**: idle randombots list themselves in the Raid
  Browser, so it is not empty.
- **`.playerbots bot add5|add10|add25|add40`**: fills your group to a 5-man
  party or a 10/25/40-man raid with a sensible tank/healer/damage mix at your
  level, with proper specs and gear.
- **Raid habits**: those bots follow in arrow formation, and damage dealers
  hold a few seconds into a pull so the tank gets threat.
- **Bots past level 80**: bots that take the level of a master above 80 get the
  level-80 premade talents instead of none.
- **Pinned random bots** (playerbots): an API other modules use to keep chosen
  random bots permanent and online, never re-rolled, teleported or pulled out
  of their group. A pinned bot revives in place, and skips playerbots' canned
  status and hello whispers. mod-leeroy pins the guild with it.
- **Speaker filter** (mod-ollama-chat): lets another module keep chosen bots
  out of the LLM chat, so the guild speaks only through its own mind.

Apply them to a checkout of each module with
`git am /path/to/leeroy/patches/<module>/*.patch`.

## Scripts

```bash
./ac-update.sh [--dry-run]   # pull core + git modules, rebasing local patches
./ac-backup.sh [label]       # mysqldump all four databases
./ac-build.sh                # preflight checks, configure, build
./ac-server.sh start|stop|status # the game servers and the mind
./ac-voice.sh start|stop|status # voice service; follows LEEROY_VOICE_DATA and voice.toml
./ac-snapshot.sh             # copy live configs and module patches into this repo
```

They assume this repo is the install root, with the core checked out in
`azerothcore-wotlk/` and the build in `build/`. See `CLAUDE.md`.

## Training ground

The optional training ground records completed fights, checks observable mechanics,
and compares tactics on established farm encounters. Adaptation requires at least
28 days of eligible received records and a passed overhead gate. Reports separate
fixed score bases, incomplete coverage, desired setups and verified observations.
An offline tactic win followed by recorded use in a human raid proves adoption;
it does not establish a causal improvement in that raid. Requested damage counters
cannot establish actual boss DPS, and dummy results cannot establish a field
rotation win. Voice send records do not prove audible playback or complete callouts.

Training is disabled by default. Private records live in the mind data directory's
`mind.db` (`tg_` tables and scoped receipts). Use `python -m leeroy_mind training
status`, `training report --week YYYY-Www`, and `training trials` to inspect them;
the latter commands follow the same module invocation. With the mind stopped,
`training rollback --context MAP:BOSS:DIFFICULTY` or `training stock --context ...`
changes the stored card selection under the service lock, while preserving pending
command journals. Native `Leeroy.Training.Enable = 0` stops additions and requests
verified cleanup, which may remain pending; `Leeroy.Telemetry.Enable = 0` stops
recording. See [operating notes](CLAUDE.md#training-ground) and the
[research references](references.bib). Implementation and passing tests are separate
from live observation and acceptance.

## CI

| Workflow | When | What |
|---|---|---|
| `ci` | main pushes and pull requests | shellcheck on `scripts/`, gitleaks over each push's and pull request's commits, mod-leeroy unit tests, the mind's Python 3.14 suite, the `voice` job's Python 3.12 suite, and a headless cross-service rehearsal in separate environments |
| `patches` | patch changes, nightly | applies the patches to the newest upstream mod-playerbots (plus its codestyle check) and mod-ollama-chat; a nightly failure opens a `patch-drift` issue |
| `build` | patch or module changes, weekly | compiles the fork core with the patched playerbots and mod-leeroy (clang, `-Werror`, ccache) |
| `release` | `v*` tag | GitHub release with the patches, modules, configs and scripts |

There is no deploy job: the server runs on one PC and is updated with the
scripts above. A self-hosted runner on a public repo would let any pull request
run code on that PC.

## Voice

The voice service speaks the guild's lines aloud in voice rooms, each member in
a voice of their own, and turns the player's push-to-talk into text for the
mind. It runs on the live server beside the mind; if it is down, the guild types
in chat as before. Playback keeps each engine's native sound; the older
microphone and codec effects are off unless switched on. Every voice is a blend
of synthetic stock voices, none cloned from a real person.

Its default loopback ports are 7879 for the mind and 7880 for its browser
panel. Runtime settings and models stay in the ignored `voice-data/`; voice
specs and synthetic references stay in `mind-data/voices/`. The
[voice README](voice/README.md) covers setup, GPU warmup and the optional
Chatterbox Turbo CPU fallback, and [the operating notes](CLAUDE.md#the-voice-service-leeroy-voice)
cover commands and data-directory overrides. A
[headless regression rehearsal](voice/tools/rehearsal/README.md) checks the two
services together in CI, with synthetic data and fake audio devices.

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
