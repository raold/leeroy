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
strategies; the [dojo](dojo/README.md) is where bot combat policies are to be
trained, on a separate server.

## Layout

| Path | What |
|---|---|
| `scripts/` | Update, backup, build, start/stop, voice and snapshot scripts (Git Bash on Windows), and `scripts/dojo/` (PowerShell, for the dojo's isolated build) |
| `config/` | Snapshots of the live server and module configs, secrets masked |
| `patches/mod-playerbots/` | Local playerbots commits, as `git format-patch` output |
| `patches/mod-ollama-chat/` | Local mod-ollama-chat commits, same format |
| `patches/dojo/mod-playerbots/` | The dojo's own playerbots commit (an external-controller hook), applied on top of the local commits in the dojo's isolated checkout only |
| `modules/mod-leeroy/` | The guild module: keeps the guild's bots pinned online and their consumables topped up, fills the player's `.playerbots bot addN` groups from the guild, and runs a local bridge (NDJSON over `127.0.0.1:7878`) through which an outside "mind" sees the guild and speaks and acts for its members. Unit tests, and a smoke test in `tools/` |
| `modules/mod-dojo/` | The dojo's native module, built only into the dojo's isolated core (the CI build compiles its disabled variant) |
| `mind/` | The mind (`python -m leeroy_mind run`), a Python service that voices the guild through the bridge and a local LLM (Ollama); see [The mind](#the-mind) |
| `selfie/` | The profile-photo worker (`python -m leeroy_selfie`): RealVisXL renders on the local GPU, started by the mind at night only; separate Python 3.12 process, GPL-3.0-or-later, with its own [LICENSE](selfie/LICENSE) and [README](selfie/README.md) |
| `voice/` | Local voice comms and push-to-talk (`python -m leeroy_voice`), in a separate Python 3.12 process; GPL-3.0-or-later, with its own [LICENSE](voice/LICENSE) and [README](voice/README.md) |
| `dojo/` | The dojo (`python -m leeroy_dojo`): an isolated environment for training combat policies against its own worldserver, never the live realm; see [its README](dojo/README.md) |
| `references.bib` | The research, software and models behind the project, each entry annotated with what it shaped |
| `CLAUDE.md` | Operating notes: paths, build traps, module quirks, what each patch does |

## Scripts

```bash
./ac-update.sh [--dry-run]   # pull core + git modules, rebasing local patches
./ac-backup.sh [label]       # mysqldump all four databases
./ac-build.sh                # preflight checks, configure, build
./ac-server.sh start|stop|status # the game servers and the mind
./ac-voice.sh start|stop|status # voice service; follows LEEROY_VOICE_DATA and voice.toml
./ac-snapshot.sh             # copy live configs and module patches into this repo
./ac-health.sh [--json]      # read-only health check of the live install; never dials the bridge
```

They assume this repo is the install root, with the core checked out in
`azerothcore-wotlk/` and the build in `build/`. See `CLAUDE.md`.

## CI

| Workflow | When | What |
|---|---|---|
| `ci` | main pushes and pull requests | shellcheck on `scripts/`, gitleaks over each push's and pull request's commits, the mod-leeroy and mod-dojo unit tests, the mind's Python 3.14 suite, the `voice` job's Python 3.12 suite, the dojo's suite and fake-worker protocol smoke, and a headless cross-service rehearsal in separate environments |
| `patches` | patch changes, nightly | applies the patches to the newest upstream mod-playerbots, then the dojo's series on top (plus playerbots' codestyle check), and to mod-ollama-chat; a nightly failure opens a `patch-drift` issue |
| `build` | patch or module changes, weekly | compiles the fork core with the patched playerbots, mod-leeroy and mod-dojo's disabled variant (clang, `-Werror`, ccache) |
| `release` | `v*` tag | GitHub release with the patches, modules, configs and scripts |

There is no deploy job: the server runs on one PC and is updated with the
scripts above. A self-hosted runner on a public repo would let any pull request
run code on that PC.

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
  of their group, and never logged out to meet the online bot count. A pinned
  bot revives in place, and skips playerbots' canned status and hello
  whispers. mod-leeroy pins the guild with it.
- **Speaker filter** (mod-ollama-chat): lets another module keep chosen bots
  out of the LLM chat, so the guild speaks only through its own mind. A queued
  reply asks the filter again before it is delivered.

Apply them to a checkout of each module with
`git am /path/to/leeroy/patches/<module>/*.patch`.

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
  guild can run a raid of its own, one a day by default. A group the player
  fills with `.playerbots bot add10|add25|add40` comes from the guild, and
  once the player leads it into a raid it counts for DKP and attendance like
  a called raid night. Between raids it keeps ranks, specs and gear current,
  and raiders keep flask and feast habits.
- **The Realm** (off by default): the world outside the guild. Three rival bot
  guilds (a progression rival, a drama farm and Alliance gankers) talk in Trade
  and General and race the guild for first kills. There are also canon
  achievements, the guild bank, a guild calendar with raid nights and
  birthdays, mail and gifts to the player, a guard against kicking the cast,
  and the realm's own night stage of forum posts, letters and write-ups,
  which answers the player's Board replies first.
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
  window on `127.0.0.1:7881` (dark by default, with a remembered light/dark
  switch; 13 tabs): Comms, the voice rooms and the guild's chat in one feed
  with the members' faces, channels, whispers, mute and deafen, and a reply
  box that speaks in WoW as the owner,
  the raid night (its call, comp, members, loot and DKP as they happen, recent
  raids, and the raid actions guild life performs: call, form, summon,
  start and stop the dungeon-clear run, end), the guild (roster, DKP ladder and
  stats: attendance, who is due an item, what each raid handed out; loot
  history, season awards, chronicle), the cast's personas as a wall of faces
  or a table (mood, stance, ties, memory counts, persona versions and drift)
  with their tuning dials at world, group and member level (a sample-line
  preview, and a change log with undo), the guild's drama (feuds, jealousy,
  romances, rumours, mood), the Board (the Realm's forum, the letters in the
  owner's mailbox and event write-ups, where he can reply to a post or a letter
  and the guild answers that night), the guild calendar, the guild bank (read
  for the console by the mind, so the database password stays with the mind),
  the Realm (rival guilds, the race for first kills, poaching, Trade lines), the
  training ground's fights, gate, trials and cards, the dojo (read from its own
  files), a search of the game library (text first, the mind's semantic search
  as a fallback), the game server and who is online, the mind's and the voice
  service's health, and their start, restart and stop. It runs apart from the
  mind, so it can start the mind again; it stops the mind cleanly, and sends
  raid actions, dial changes and Board replies, through the mind's loopback
  control endpoint. Away from home the owner opens it over his
  tailnet (`tailscale serve` to `https://<pc>.<tailnet>.ts.net`, never Funnel)
  with a bookmarked stable key, rotated with `console rotate-key` if a device
  is lost; a logon task (`scripts/ac-console-schedule.ps1`) keeps it running.

Personas, memories, prompts and logs stay in the ignored `mind-data/`. See
[the operating notes](CLAUDE.md#the-mind-service-leeroy_mind) for commands.

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

## The dojo

The dojo is where the bots' combat is to be trained with deep reinforcement
learning, on a worldserver of its own. It has its own AzerothCore and
playerbots checkouts, build tree, MySQL instance, loopback ports and synthetic
fixture characters, and its preflight (`scripts/dojo/preflight.ps1`) refuses
any layout that overlaps the live install. Nothing in it touches the live
realm.

What exists is the foundation:

- **Protocol**: an environment protocol written twice, in pure C++
  (`modules/mod-dojo/src/Lib`, no AzerothCore includes) and in Python
  (`leeroy_dojo`), with a loopback transport.
- **Native side**: `mod-dojo`, a module built only into the dojo's core, and
  one playerbots commit (`patches/dojo/`) that hands chosen actors to an outside
  controller.
- **Training realm**: a disposable arena with two fixture actors. The
  controller has exclusive control of them through a narrow executor that acts
  like a client, and each actor sees only what is visible to it, after a
  perceptual delay.
- **Episodes**: each one resets by tearing the arena down and building it
  again. The dojo decides the outcomes and rewards, and a checksummed recorder
  writes episodes that replay without a game client.
- **Baselines**: `noop`, `random_legal` and `frost_duelist` (an authored Frost
  Mage script). The stock playerbots AI runs as a separate system benchmark.
- **The G1 evidence run**: it runs all of the above and checks each episode
  against the core's own scoreboard.

There is no learner yet, and no learned policy is claimed. PPO and its
multi-agent form are the planned first baselines (see [Research](#research)).
The Python package uses only the standard library, on Python 3.12. CI runs its
suite, the C++ protocol tests and a fake-worker smoke test. The
[dojo README](dojo/README.md) has the layout, the protocol and the commands.

## Research

[references.bib](references.bib) lists the 278 papers, books, software and
models behind the project. Each entry notes what it shaped, or, in the dojo's
section, what it is planned for. The main threads:

- **Generative agents.** Park et al.'s *Generative Agents* (2023) is the
  Smallville experiment, in which 25 agents lived in a simulated town. It gave
  the mind its memory stream, the recall score (recency, importance,
  relevance), nightly reflection and per-pair relationships. Anderson and
  Schooler (1991) and the FSRS forgetting curve shape how memories fade.
  MemGPT's self-editing memory was kept off the live path because of its
  latency.
- **Theory of mind.** Each member must know only what they could know. LLMs'
  theory of mind breaks under trivial changes to a task (Ullman 2023), so the
  mind tracks per member who knows what rather than trusting the model, after
  FANToM (Kim et al. 2023) and TimeChara (Ahn et al. 2024). The weekly
  evaluation probes secret-keeping, after Mireshghallah et al. (2024).
- **Relationships and social life.**
  - Disclosure is paced by social penetration theory (Altman and Taylor 1973)
    and Aron et al.'s closeness procedure (1997).
  - Moods follow ALMA (Gebhard 2005) and emotional contagion (Hatfield et al.
    1993).
  - Gossip, nicknames and slang follow the psychology of rumour (Allport and
    Postman 1947), naming-game dynamics (Baronchelli et al. 2006) and Dunbar et
    al. on conversation (1997).
  - Each member's drives are weighted by Yee's motivations for play (2006).
- **Evaluation.** SOTOPIA (Zhou et al. 2024), PersonaGym (Samuel et al. 2025)
  and LoCoMo (Maharana et al. 2024) shaped the weekly persona, social and
  memory checks.
- **Reinforcement learning for combat.**
  - PPO (Schulman et al. 2017) and its cooperative multi-agent form, MAPPO (Yu
    et al. 2022), are the dojo's planned first baselines.
  - AlphaStar's league (Vinyals et al. 2019) and OpenAI Five (OpenAI 2019) are
    the references for later self-play.
  - Reflexion (Shinn et al. 2023) and Voyager (Wang et al. 2024) are the models
    for the training ground's planned post-fight reflection and growing tactic
    library.

## Profile photos

With `[selfies] enabled = true` the mind picks a photo kind for each guild member who has looks
and, at night only, has the `selfie/` worker render four candidates each. The owner picks one,
undoes it or asks for four more in the console's Personas tab ("Choose selfies", through
`POST /api/selfie`). Rendering stays off by default, never runs while the owner is in game, and
every image passes a local NSFW check first. See [selfie/README.md](selfie/README.md).

## License

GPL-2.0, matching AzerothCore and mod-playerbots, except the separate `voice/`
service and `selfie/` worker, which are GPL-3.0-or-later under [voice/LICENSE](voice/LICENSE)
and [selfie/LICENSE](selfie/LICENSE).
