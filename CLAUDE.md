# AzerothCore (WotLK 3.3.5a) — private server

A WotLK private server built from the **mod-playerbots fork** of AzerothCore,
with bots and several gameplay modules. Windows 11, MSVC, MySQL 8.4.

## Training ground

The optional training controller stores private fight records, tactics, trials,
setups and command journals in the configured mind data directory's `mind.db`
(`tg_` tables and scoped `meta` receipts). Schema support and code tests are
separate from measured acceptance. Adaptation requires at least 28 days of
eligible receipt history plus a passed overhead gate. Reports keep score bases
separate and state missing coverage; historical backlog imports cannot shorten
the observation period. A verified offline tactic win used later in a human raid
shows adoption, not causal improvement there. Dummy requested-damage proxies do
not prove actual boss DPS or a field rotation win. Voice send receipts do not
prove that audio was heard, facts were spoken, or every cast was covered.

Native keys `Leeroy.Telemetry.Enable` and `Leeroy.Training.Enable` are reloaded
with `.reload config`. The first stops new telemetry recording when zero. The
second refuses training additions and requests cleanup of owned bot changes,
DC run settings, dummy jobs and benchmarks. Cleanup retries with exact ownership
and completion checks; unavailable bodies or ambiguous receipts can remain
pending. Disabling a flag is not proof that everything was restored. Preserve
journals and pending receipts until verified cleanup completes. `[training]
enabled = false` disables the Python attachment on the next mind start; it does
not clear an outstanding native or Python journal.

Inspection commands use `python -m leeroy_mind training`: `status`, `report
--week YYYY-Www`, `gate`, `trials`, `cards`, `lessons --context MAP:BOSS:DIFFICULTY`,
`voicecheck --night YYYY-MM-DD`, `spells check` (loads the spell book and lists the
spell ids behind the boss-mechanic names) and `lab` (stored rotations, the last
nights' lab summaries and the dummy row count, labelled as a requested-damage
proxy). Read-only inspection does not migrate an old
database. With the mind stopped, `rollback --context MAP:BOSS:DIFFICULTY` restores
the prior stored champion and `stock --context ...` selects stock; the service OS
lock is held throughout. These card changes leave applied-command cleanup
journals intact. `gate --reset` discards gate evidence; `rescore` rechecks stored
pulls and restores matching scores.

The bridge exposes `telemetry`, `get_stats`, `get_vocab`, `custom_strategy`,
`dc_setting`, `dummy_trial`, `get_rivals`, `start_benchmark` and
`get_training_cleanup`. Recorder hooks are gated by the telemetry tap. Native
`bot_do restore` uses the training mutation journal; reset/clear operations retain
their cleanup role when new additions are disabled. Extra pins pass through
`LeeroySetExtraPins` into the single pin composer instead of calling
`SetPinnedBots` independently. Research background is in [references.bib](references.bib).

## Canonical paths

| What | Where |
|---|---|
| Install root | `F:\azerothcore` |
| Source | `F:\azerothcore\azerothcore-wotlk` |
| Build tree | `F:\azerothcore\build` |
| Binaries + runtime | `F:\azerothcore\build\bin\RelWithDebInfo` |
| Server configs | `...\bin\RelWithDebInfo\configs` |
| Module configs | `...\bin\RelWithDebInfo\configs\modules` |
| DB dumps | `F:\azerothcore\db_backup` |
| Maintenance scripts | `F:\azerothcore\scripts` |
| Client-side patches | `F:\azerothcore\client-patches` |

### A dead copy on Q: — always confirm the drive first

**`Q:\azerothcore` is DEAD.** It is a complete, convincing duplicate: same zips,
its own source tree, its own `build\`, its own `Wow355a` client with a real
character dir. A second one on `D:\azerothcore` (where the install lived until
it moved to F: on 2026-09-07) was deleted by the owner on 2026-10-30.

It is a genuine decoy, not obvious junk — a previous session lost about an
hour to the D: tree before noticing. **Confirm you are on `F:` before touching
any azerothcore path.** The giveaway is the build date: F: is current, Q:
stopped at 2026-09-06.

Do not build, read configs, or read logs from Q: (or anything left on D:).

## This directory is a public git repo: `leeroy`

`F:\azerothcore` is **github.com/raold/leeroy** (public, GPL-2.0, branch
`main`). A whitelist `.gitignore` tracks only what is ours: this file, the
README, `LICENSE`, `references.bib` (the bibliography), `scripts/`, `config/`,
`patches/`, `modules/` (mod-leeroy, mod-dojo), `mind/` (the mind service), `voice/`
(the voice service), `selfie/` (the selfie worker), `dojo/` (the dojo, source only) and
`.github/`. The core
repo, the build tree, the client, boost, DB dumps and the zips stay out, and so
do the local dev docs (`docs/`, `private/`). `dojo-runtime/` and `dojo-data/`
beside the install root hold the dojo's environments and recordings and are
never committed.

The checkout at `F:\azerothcore` itself runs the installed release: a local
branch (now `codex/live-release-20261102`) whose private history is never
pushed wholesale. Public changes land on `main` from worktrees under
`F:\azerothcore\worktrees\`. GitHub deletes a merged PR's head branch by
itself (repo setting, 2026-11-06), so do not delete remote branches by hand; the
local branch and worktree still need `git worktree remove` and `git branch -d`.

- `config/`, `patches/mod-playerbots/` and `patches/mod-ollama-chat/` are snapshots made by
  `scripts/ac-snapshot.sh`. Never edit them by hand; edit the live configs or
  the playerbots commits, then re-run it. It masks secret values (anything
  ending `DatabaseInfo`, `Secret`, `Token`, `ApiKey` or `Password`) and fails
  if a real secret survives. Nothing secret may ever be committed, since git
  history is permanent and public.
- Commits in this repo and the local playerbots commits use the GitHub no-reply
  identity (`raold <868138+raold@users.noreply.github.com>`, set in each
  repo's local git config). Never add attribution trailers.
- **Local dev docs, never pushed:** design and research Markdown in
  `docs/superpowers/` and `docs/research/`, plus `private/` (personas,
  brainstorms, the blind model test). They are for the owner and Claude only,
  git-ignored on purpose, and exist only on F:. Never commit them, and never
  delete any of them; update in place or add a new dated file.
- Rewriting playerbots commits changes their hashes; update the hashes cited
  below and re-run `ac-snapshot.sh`.
- CI (`.github/workflows`, see README): `ci` (shellcheck, gitleaks, the
  mod-leeroy and mod-dojo unit tests, the mind, voice and dojo suites, and a headless
  mind-voice rehearsal), `patches` (applies the playerbots patches, then the dojo's
  series on top, and the mod-ollama-chat patches to upstream; nightly and on patch
  changes; a nightly failure opens a `patch-drift` issue, which means rebase before
  the next `ac-update.sh`), `build` (Linux compile with clang, `-Werror`, of the
  patched fork with mod-leeroy and mod-dojo's disabled variant; weekly and on patch
  or module changes), `release` (on `v*` tags). No
  deploy job and never a self-hosted runner: the repo is public.

## Build traps that have each cost real time

### 1. Module folder names must be exact

A module's **directory name** drives two separate mechanisms:

- `modules/CMakeLists.txt` does
  `include(".../modules/<dir>/<dir>.cmake" OPTIONAL)` — a `.cmake` not named
  exactly after its folder is skipped **silently**, taking its compile flags and
  core-compat defines with it.
- The generated script loader calls `Add<dir, dashes as underscores>Scripts()`,
  which must match what the module's source defines, or the link dies with
  `LNK2019`.

GitHub zips extract as `mod-foo-master` / `mod-foo-main`. **Always strip that
suffix.** On 2026-10-06 this one mistake produced three different failures: a
silent skip (`mod-dungeon-clear-master` lost its `/D_USE_MATH_DEFINES`, which
surfaced as bogus `M_PI` errors inside *core* headers), plus two unresolved
externals (`mod-transmog-master`, `mod-individual-progression-master`).

`scripts/ac-build.sh` checks both rules before building.

### 2. The build tree stores absolute paths

`build/CMakeCache.txt` hard-codes absolute paths. When the install was copied
from D: to F:, the cache still said `D:/azerothcore/build` and CMake refused to
run at all. Fixing it meant rewriting 24 entries. **Do not move or copy
`build/`.** If you must relocate it, delete the cache and configure fresh.

### 3. Build parallelism is capped at 16

Not a CPU limit — a memory one. The PCH is large, and above ~16 concurrent
`cl.exe` the commit charge (63.8 GB RAM, 84.5 GB commit limit) runs out and
MSVC fails with `C3859: Failed to create virtual memory for PCH` and
`C1076: compiler limit: internal heap limit reached`. A full `-m` (32 jobs on
the 14900KS) fails 128 translation units this way. `AC_JOBS` defaults to 16.

`-m` alone does **not** cap `cl.exe`: it limits concurrent projects, and each
project still runs `/MP` with one compiler per core (33 were seen at `-m:16` on
2026-10-29, and it hit C3859). `ac-build.sh` now also passes
`UseMultiToolTask` + `EnforceProcessCountAcrossBuilds` + `CL_MPCount=$AC_JOBS`,
which makes it a true global cap (verified: peak 16).

If commit charge is high but no process owns it, check PrimoCache first: its
L1 cache reserved about 20 GB of RAM outside any process, which forced builds
down to `AC_JOBS=8` until the owner disabled it on 2026-10-30.

### 4. Never kill a build mid-compile

MSBuild's tracking logs record a file as built before `cl.exe` finishes. Kill
the build and the next run skips those files: on 2026-10-29 that left 5 missing
`.obj` (LNK1181) and 9 stale ones from the previous build that would have
linked silently. If you must kill one, delete `<project>.dir/RelWithDebInfo`
for every project that was compiling. Killing the bash wrapper does not stop
`cmake`/`MSBuild`/`cl` — `taskkill /T /F` the `cmake --build` PID.

### 5. Visual Studio updates invalidate the PCH

After a VS update, the next build fails at once with `C1853 ... precompiled
header file is from a different version of the compiler`. MSBuild does not
notice the compiler changed. Fix: `find build -name '*.pch' -not -path './bin/*' -delete`
(a near-full rebuild follows).

## Git remotes — pull from the fork, not upstream

The core repo has two remotes. **`origin` is upstream AzerothCore and is the
wrong one.**

```
core:       F:\azerothcore\azerothcore-wotlk
            branch  Playerbot
            remote  playerbot -> github.com/mod-playerbots/azerothcore-wotlk
            (origin -> azerothcore/azerothcore-wotlk — do NOT pull this)

playerbots: modules/mod-playerbots
            branch  master
            remote  origin -> github.com/mod-playerbots/mod-playerbots
```

The fork carries core patches playerbots depends on. Module code tracks the
fork's current API, so a module cloned today may not compile against a core
that is months behind — on 2026-10-06 `mod-ollama-chat` called
`WorldSession::IsHeadless()`, which the fork had renamed from `IsBot()` after
the local core was last pulled.

## Build toolchain

- Visual Studio 17 2022, `RelWithDebInfo`, `SCRIPTS=static`, `MODULES=static`, `TOOLS_BUILD=all`
- Boost 1.92 at `F:\azerothcore\boost_1_92_0`
- MySQL 8.4 at `C:\Program Files\MySQL\MySQL Server 8.4`
- OpenSSL at `C:\Program Files\OpenSSL-Win64` (required for mod-ollama-chat HTTPS)

## Databases

`acore_auth`, `acore_characters`, `acore_world`, `acore_playerbots` on
`127.0.0.1:3306`. Credentials live **only** in `configs/worldserver.conf`
(`WorldDatabaseInfo` and friends) — the scripts parse them from there rather
than keeping a second copy. Never echo the password.

The auto-updater applies new SQL on worldserver start:
`Updates.EnableDatabases = 15`, `Updates.AutoSetup = 1`. It needs
`SourceDirectory = "F:/azerothcore/azerothcore-wotlk"` and a valid
`MySQLExecutable`. **If `SourceDirectory` is wrong, module SQL silently loads
from the wrong tree or not at all.** Module SQL is only applied for modules that
were present in `modules/` when CMake last ran.

Live configs are `.conf`; the `.conf.dist` files are templates that a pull
updates. New `.dist` keys do not appear in your live `.conf` — missing keys fall
back to compiled-in defaults. `ac-update.sh` reports them.

## Scripts

Run from `F:\azerothcore\scripts` in Git Bash.

```bash
./ac-update.sh [--dry-run]   # pull core + git modules (ff-only, local commits rebased); refuses if dirty
./ac-backup.sh [label]       # mysqldump all 4 DBs, keeps newest 5
./ac-build.sh [--no-configure | --preflight]   # preflight + cmake configure + build; --preflight stops after the module checks
./ac-server.sh start|stop|status # also starts/stops the mind; stdout: mind-data/logs/mind-stdout.log
./ac-voice.sh start|stop|status # voice service; reads its data directory and ports from `where`
./ac-snapshot.sh             # live configs (masked) + playerbots and ollama-chat patches -> config/, patches/
```

`./ac-health.sh [--json] [--toast]` (`python -m leeroy_mind health`) checks the live install
read-only, one ok/FAIL/note line each, exit 1 on any FAIL: the servers, Server.log's cast line
since worldserver started (floor `[health] min_cast`, default 100; a note when the log begins
later, since `.reload config` reopens Server.log empty in its `w` mode) and no `so no places`, the
mind and mind.log, the control endpoint's `status` (it reports `bridge_connected`, `cast` and
`schema_version`), the console and voice (notes when down), Ollama's `/api/ps`, and stuck
activity (no `tg_pull` in 48 h with telemetry on and the overhead gate passed, a note while the
gate is in phase `ab`; a realm hold or `unknown` root older than 1 h; cast without personas). It
never dials the bridge (7878), loads no model, starts nothing and never probes `mind.lock` or
`voice.lock` (running comes from the process list, `control.json`, `voice.pid` and the TCP
table). `--toast` shows one toast per new failure and writes only
`mind-data/health-state.json`; a read-only open can leave SQLite's empty `mind.db-wal` and
`mind.db-shm` behind while the mind is stopped, as doctor and the console do.
`ac-health-schedule.ps1 -Install|-Remove|-Status` manages a per-user task that runs it every
15 minutes while he is logged on (not installed by default).

`db_backup/` holds two kinds of thing: sets `ac-backup.sh` made
(`<label>_<YYYYmmdd>_<HHMMSS>`) and sets from the older script described below
(`YYYY-MM-DD_HHMM`), plus a raw MySQL datadir copy. **The prune only ever
deletes the first kind.** Keep that invariant if you touch `ac-backup.sh` — on
2026-10-06 an unfiltered prune deleted one of the older sets before this was
fixed.

### There is an older, more thorough backup script

`Q:\azerothcore\backup_acore_dbs.sh` predates these scripts and does more:
gzipped dumps, table-count verification against `information_schema`,
`SHA256SUMS`, 14-set retention. Its drawback is that it defaults to **Q:** for
both its output dir and its credentials file (`Q:\azerothcore\.acore_backup.cnf`),
so it breaks if Q: goes away, and alternating targets means neither side prunes.
Run it against F: with:

```bash
ACORE_BACKUP_DIR=/f/azerothcore/db_backup bash /q/azerothcore/backup_acore_dbs.sh
```

`ac-backup.sh` is the simpler F:-local alternative with no Q: dependency and no
separate credentials file. Use whichever you prefer, but know both exist.

Normal update cycle:

```bash
./ac-update.sh && ./ac-backup.sh pre_update && ./ac-build.sh && ./ac-server.sh start
```

### Stopping the server cleanly

`ac-server.sh stop` **terminates** worldserver. It cannot do better: worldserver
is a console app that ignores `WM_CLOSE`, and this install has both remote
channels off (`Ra.Enable = 0`, `SOAP.Enabled = 0`), so nothing can ask it to
shut down from a script. Verified on 2026-10-06 — the polite request was
ignored by both servers.

If characters are online, type this in the worldserver console window first:

```
server shutdown 30
```

Setting `SOAP.Enabled = 1` would let the script do a real graceful shutdown.

## Modules

| Module | Source | Notes |
|---|---|---|
| `mod-playerbots` | mod-playerbots/mod-playerbots | Core feature. Tuned config; `.bak-500bots` / `.bak-1000bots` variants kept. |
| `mod-ollama-chat` | DustinHendrickson/mod-ollama-chat | LLM bot chat. git clone, updatable. |
| `mod-transmog` | zip (no git) | |
| `mod-individual-progression` | zip (no git) | Rewrites a lot of world/character data. Gates raids per character by tier (ICC needs 16, from killing Anub'arak in ToC; tier = highest rewarded quest `66000+N`). `.ip set <name> <tier>` **ignores the name** and applies to your target or yourself, and from the console it would dereference an empty player: use it in game with no target. Old PvP rank titles come from lifetime kills; live conf has `VanillaPvpEarnTitlesAfterVanilla = 1` and `VanillaPvpKillRequirement.Rank14 = 3000` (2026-11-01, so the owner holds High Warlord; bots do not earn ranks). A rank above what kills allow is stripped at each map change, even if set with `.titles add`. |
| `mod-dungeon-clear` | jrad7/mod-dungeon-clear | git clone since 2026-10-29 (was a zip of `60f3d98`). **The autonomous tank lead**: `.dc on` (or `dc on` in party chat) inside an instance makes the group's tank bot route boss to boss over the navmesh, pull (Dynamic/Leeroy/Advanced), run scripted events (all 16 WotLK 5-mans), loot, rest and rez. Raids too: trash flow with a readiness quorum, pre-boss muster, wipe recovery; boss fights are left to the playerbots raid strategies. Companion addon: jrad7/mod-dungeon-clear-addon. It hooks playerbots without patching it (appends to the shared context registries). Ships its own `.cmake`; folder name must stay exact. Reads `PlayerbotAIConfig` members directly, so it breaks when playerbots renames them. AGPL-3.0. |
| `mod-bg-slaveryvalley` | zip (no git) | Custom battleground, map 801 / BG id 31. Server side complete; **client patch still undistributed** — see below. |
| `mod-ah-bot-plus` | NathanHandley/mod-ah-bot-plus | Auction house bot (git clone since 2026-10-30). Sells under, and buys player auctions as, the ordinary account `ahbot` (auth id 352): characters Fatiggeritch (3502) and Bonqueefa (3503), set in `AuctionHouseBot.GUIDs` (comma list, no spaces). Never use a playerbot character for it, and never enter the world on those characters. Seller and buyer both on; everything else is the `.dist` default (about 150 listings a minute per house, up to 15000 each). GM commands: `.ahbot reload` (re-reads the conf, GUIDs included, no restart), `.ahbot update`, `.ahbot empty`. Loader `Addmod_ah_bot_plusScripts`, so the folder name must stay exact. No SQL. |
| `mod-leeroy` | this repo, `modules/mod-leeroy` | The guild bridge. `azerothcore-wotlk/modules/mod-leeroy` is a **junction** to `F:\azerothcore\modules\mod-leeroy`: remove it with `rmdir`, never `rm -rf` (that deletes the source through the link). Pins the cast (guild members at 80+, minus `Leeroy.HumanGuid`) via the playerbots pin API, mutes them in mod-ollama-chat, and serves NDJSON on `Leeroy.Bridge.Host`:`Leeroy.Bridge.Port` (`127.0.0.1:7878`, one mind at a time). Keys: `Leeroy.Enable`, `GuildId`, `HumanGuid`, `KeepCast`, `BringCastOnline`, `ExcludeFromOllamaChat`, `Bridge.*`, `CastGuard` (the kick guard), `Realm.*` (see the Realm) and `Telemetry.Enable`/`Training.Enable` (see Training ground). Start line in `Server.log`: `mod-leeroy: cast N members, pinned N ...`. Smoke test: `python modules/mod-leeroy/tools/bridge_smoke.py --expect-cast N` (0 pass, 1 fail, 2 no server or another mind connected); `--listen S` prints events, `--send` sends raw commands. `owner_say` (the console's replies, separate from `human_say`) queues his chat line on his session: guild, officer, party, raid or a whisper to an online cast member; text is sanitized and then refused, not cut, when empty, over 255 UTF-8 bytes or starting with `.` or `!`; at most one line per 1500 ms and 8 per 30 s; he must be in game. Item links: hello lists `features: ["item_links"]`, and only then does the mind put `{{item:<id>}}` placeholders in a cast line (`say`, `realm_say`; `mind/leeroy_mind/talk/items.py`: bracketed or exact multi-word item names from library.db, never a place, NPC or test item unbracketed, kept as `[Name]` in every record, every line folded and defused after the wire's cleaning). Native sanitizes first and expands only a placeholder written whole in the raw text into the client's link from the item template, at most two a line within 255 bytes (`src/Lib/LeeroyItemLinks.*`), so no other `|` survives; a Realm channel hook sees the `[Name]` form, and its rewrite goes out without links. For the owner it answers `.playerbots bot add5/10/25/40` from his guild (see that patch below). |

`mod-playerbots`, `mod-ollama-chat`, `mod-dungeon-clear` and `mod-ah-bot-plus` are
git repos and `mod-leeroy` lives in this one; the rest are zip extractions and
`ac-update.sh` skips them. Updating those means re-downloading.

### Playerbots config prefix is `Playerbots.*`

Playerbots #2854 (pulled 2026-10-29) renamed every key from `AiPlayerbot.*` to
`Playerbots.*` with no fallback: an unmigrated conf silently runs every option
at its default. The live `playerbots.conf` and the `.bak-500bots` /
`.bak-1000bots` variants were migrated; `.bak-prepolish` / `.bak-prepvp` still
use the old prefix. Pre-migration copy: `playerbots.conf.bak-preprefix-20261029`.
If `ac-update.sh` reports hundreds of "new keys" at once, suspect a rename.

### Local patch: randombots in the Raid Browser

Playerbots randombots only copy Dungeon Finder queues that real players are
in, so the Raid Browser was always empty. Local commit `bf33a531` on
`mod-playerbots` `master` ("List idle randombots in the Raid Browser") adds
`RandomPlayerbotMgr::CheckRaidBrowser()`: once a minute on the world thread it
keeps `Playerbots.RandomBotRaidBrowserListings` (20) idle bots per faction
listed for up to 3 raids of their level/expansion, each for 50-150% of
`RandomBotRaidBrowserListTime` (1800 s), and drops a listing once the bot is
invited. Listings log `lists in the raid browser` to `Playerbots.log`. It
needs the raid browser bit in `DungeonFinder.OptionsMask` (now 7; it was 5,
which disabled the raid browser entirely; `lfg options 7` sets it live).

### Local patch: `.playerbots bot add5|add10|add25|add40`

Local commits `c26c0d20`, `7decec27` and `538264f8` (add5) on `mod-playerbots`
`master`. Tops the player's group up to a 5-man party or a 10/25/40-man raid
with addclass bots: 1/1/3, 2/3/5, 3/6/16 and 4/10/26
tank/healer/damage, counting roles already in the group (so re-running it only
fills gaps). Classes come from fixed rotations in `PlayerbotMgr.cpp`
(`RAID_TANKS`, `RAID_HEALERS`, `RAID_DAMAGE`) matched to
`Playerbots.PremadeSpecName` by name; each bot gets that premade spec, glyphs
and fresh gear in `OnBotLogin`, plus `formation arrow`, and damage bots get
`co +wait for attack` with a 4 s hold so the tank opens pulls (`7decec27`).
Limited by `Playerbots.MaxAddedBots` (40).
Do not run `.playerbots bot init=auto` on such a raid: `Randomize()` re-rolls
the spec. Use the bots' `autogear` chat command instead.
Bots take the master's level, and the main character is 83 (GM-levelled past
`MaxPlayerLevel = 80`). Playerbots' talent code only knew spec links up to 80
and gave such bots **no talents at all**: no tank strategy, so dungeon-clear
said "No tank bot found". Local commit `8e8b4ae5` clamps to 80.
Do not pass party/raid leadership to a bot: every bot then runs `reset botAI`
(`ResetStrategies`), which drops `wait for attack` (formation survives).

**For the owner (`Leeroy.HumanGuid`) mod-leeroy answers the command itself and
fills from his guild only, never with addclass bots** (`src/Guild/GuildAdd.cpp`,
pure policy in `src/Lib/LeeroyGuildAddPolicy.*`). An `AllCommandScript`
resolves what he typed with the core's own parser
(`Acore::ChatCommands::GetAutoCompletionsFor`, so abbreviations such as
`.pl b add5` count) and, for `playerbots bot add5|add10|add25|add40` (the
subcommand compared case-sensitively, as playerbots does), stops it before
playerbots. Same composition and gap filling, from free cast members only:
online, alive, out of combat, his faction, in no group, not in a battleground,
arena or queue (a raid browser listing is fine), not teleporting or flying,
not in another instance or saved to another copy of his, and clear of
`RealmModule::MutationReason` (Realm leases, training, offline raids, pending
bot jobs). Within a role, the classes his group has fewest of come first,
lowest guid on ties. They join directly (`AddMember`; his group is made only
once someone can join, and becomes a raid only when a party is full), take the
raid night's `RaidHabits` (plus dungeon-clear's entry when he is inside an
instance) and are teleported beside him. He is told per role how
many were added and how many slots stayed empty, with a tally of why the
others were not free. He must lead his group and be alive, out of combat and
not travelling. Inside an instance, a size bigger than its copy's player cap
(add10/25/40 in a 5-man, add25/40 in a 10-man raid) is refused before anything
moves, with one line naming what fits there; refusals log `add<N> from the
guild refused: <line>`. Anyone else, `Leeroy.Enable = 0`, no guild or an empty cast:
the command reaches playerbots unchanged. Each fill publishes a `guild_group`
event (reason `built`). Taking that group onto an instance map grants
dungeon-clear's entry for it (`GrantRaidEntry`, which writes only what a member
lacks) to its guild members not yet in his copy, brings them in under the same
fences (his copy only), and publishes another (`entered`, with map, instance id
and difficulty). A group bigger than that copy's cap is not brought in, and he
is told so: the core checks the cap before a teleport but counts arrivals only
at the door, so it lets the first few in and sends the rest to their homebinds
(2026-11-05: 20 members of a 25 bounced to Dalaran from Old Hillsbrad). Such a
group raids for DKP and attendance like a called raid night (see the mind's
guild life).

### Local patch: pinned random bots

Local commit `ecbfeb6b` on `mod-playerbots` `master` ("Let other modules pin
random bots"). An API on `RandomPlayerbotMgr` that mod-leeroy uses to keep the
owner's guild of about 110 level-80 random bots permanent and always online. No
config key: nothing changes until a caller pins something.

- `uint32 SetPinnedBots(std::unordered_set<ObjectGuid::LowType> const& bots, bool keep, bool online)`,
  world thread. Replaces the pinned set; accepts only characters on RNDbot
  (type 1) accounts and returns how many it accepted. The account list is
  filled at playerbots init (`AssignAccountTypes`), so call it after that.
- `bool IsPinned(ObjectGuid::LowType bot) const`, any thread (mutex).
- `online`: every 30 s `UpdateAIInternal` logs in offline pins (at most
  `RandomBotsPerInterval` per pass) and adds them to `currentBots` regardless of
  `bot_count`; an expired `add` is re-armed instead of logging the bot out.
  Upstream's count trim (`RemoveRandomBots`, from #2774, pulled 2026-11-06)
  skips pins in `IsRemovableBot`, so a pin is never logged out to meet the
  target count. That rebase also moved the pin's `add` and `dead` timers to
  upstream's `YEAR` and `DAY`, since #2774 removed `PermanentlyInWorldTime`
  and `MaxRandomBotInWorldTime`.
- `keep`: a guard in `ProcessBot(Player*)` hands pins to `ProcessPinnedBot`,
  which reschedules `randomize` and `teleport` without running them, never
  leaves the group, leaves revival inside a dungeon or raid to the group, and
  elsewhere revives in place after the usual revive timer (no refresh, no
  teleport).

The guard must come before both the death block and "leave group if leader is
rndbot", or a raid led by a pinned bot dissolves on its next random-bot update.
Re-check after every `ac-update.sh` rebase (prints `pin guard OK`):

```bash
awk '/^bool RandomPlayerbotMgr::ProcessBot\(Player\* bot\)/{p=1}
     p && !g && /IsPinned\(botId\)/{g=NR}  p && !d && /bot->isDead\(\)/{d=NR}
     p && /leave group if leader is rndbot/{l=NR; exit}
     END{print (g && d && l && g < d && d < l) ? "pin guard OK" : "pin guard MISPLACED"}' \
    /f/azerothcore/azerothcore-wotlk/modules/mod-playerbots/src/Bot/RandomPlayerbotMgr.cpp
```

`Playerbots.log` lines (`LOG_INFO`): `Pinned N random bots (keep=…, online=…);
M rejected: not on a random-bot account`, and per bot `Pinned bot #<guid>
<name>: kept, skipped randomize`, `…: kept, skipped refresh and teleport` and
`…: revived in place`.

`Revive()` is guarded too (commit `d464806f`): `FindCorpseAction` calls it
for a masterless ghost at death count >= 5, and for a pin it now only revives
in place instead of `Refresh` (group leave) + grind teleport. Unpinning logs
nobody out: the bots keep their armed `add` and carry on as ordinary random
bots. Not covered by the pin: auto-release in a group with no real-player master
puts the ghost at the graveyard outside the instance, so the pin revives it
there; `LeaveFarAwayAction` still makes non-leader members leave at
`death count > 4` with no client master (also on a level gap > 4 or distance
from the leader); random-bot BG, LFG and Raid Browser selection still apply to
pins. Pinned bots also skip playerbots' periodic `Refresh`, which clears inventory but
is the only routine top-up of ammo, reagents and potions, so over long uptimes
cast hunters run out of ammo and casters out of reagents. A master can whisper
`maintenance` to bots in their group. mod-leeroy's world-tick job (`TopUpJob`)
tops up pinned cast bots outside combat, groups and instances, at most once an
hour each and five bots per 15-minute pass. It preserves inventory: additive
ammo and shaman reagent paths replace factory helpers that destroy old stacks.
Food and drink are filled independently so existing food cannot crowd out missing
water; shifted druids retain drink coverage. Audited factory helpers add potions
and class consumables.

Local commits `41386b71` and `a8f5d378` (first built 2026-10-31) keep pins quiet: for a
pinned bot three `TellMaster` lines are skipped, "AI was reset to defaults"
(`ResetAiAction`, which playerbots runs on "group set leader") and the
`hello_follow`/`hello` lines after `FindNewMaster`. The reset and `+follow` still run.

Local commit `3826b2f4` (built 2026-11-07) keeps pins out of random bot arena teams:
`IsEligibleForBotArenaTeam` refuses a pinned bot. Playerbots assigns teams at every bot
login, filling teams below `Playerbots.RandomBotArenaTeam2v2Count`/`3v3Count`/`5v5Count`
(live 30/30/20, raised from 10/10/5 with `Arena.MaxRatingDifference = 0` so the owner meets
the strong bot teams), and without it the cast would join them for good. Teams he made
himself are untouched.

Commits here never carry a `Co-Authored-By` or other attribution trailer.

Both local patches: `ac-update.sh` now rebases repos that are ahead of
upstream instead of refusing, so they are carried across updates; a conflict aborts the
rebase and stops the script. Upstreaming it (PR to `test-staging`) would drop
the need.

### Local patch: mod-ollama-chat speaker filter

Local commits `072d13d` ("Let other modules keep chosen bots silent") and
`80bd8c9` ("Recheck speaker permission before delivering queued replies") on
`mod-ollama-chat` `main`. The first adds `OllamaDispatch_SetSpeakerFilter(std::function<bool(uint64_t botGuid)>)`
to `mod-ollama-chat_dispatch.h`; `OllamaDispatch_Submit` copies the filter under
a mutex, calls it outside the lock and drops the request (returns `false`) when
it says `false`. Inert until set. mod-leeroy uses it to keep guild cast members
silent so only their own mind service voices them. The second makes `Deliver`
ask the filter again before any routing, history or speech, so a reply queued
before a bot was muted is dropped; the callback now runs on the world delivery
thread as well as on submitting threads, and always outside the registry mutex.
`ac-snapshot.sh` now exports
the local commits of both `mod-playerbots` and `mod-ollama-chat`, and the
`patches` workflow checks each against its upstream nightly.

### mod-ollama-chat

Talks to Ollama at `http://localhost:11434/api/generate`, model
`hf.co/bartowski/TheDrummer_Orion-26B-A4B-v1.1-GGUF:IQ4_XS` (since 2026-10-29,
the owner's blind-test winner; fallback `...Cydonia-24B-v4.3-GGUF:IQ4_XS`;
`qwen3:14b`/`qwen3:8b` still pulled for rollback, backup
`mod_ollama_chat.conf.bak-preorion-20261029`), with `OllamaChat.NumCtx = 4096`.
Ollama runs `OLLAMA_NUM_PARALLEL=4` (user env var). Orion loads at 16 GB, 100%
GPU, ~0.3 s per line. **Orion's Gemma-4 template thinks by default without
advertising it**: a request without `think:false` returns empty content.
mod-ollama-chat always sends `think` explicitly, so it is safe; any new client
must do the same. Prompts are only ~200-330 tokens; leaving `NumCtx = 0` makes
Ollama reserve 40960 tokens per slot, which with 4 slots does not fit.
`OllamaChat.MaxConcurrentQueries = 6` — do not set this to 0 (unlimited) with
large bot counts; concurrent queries will saturate the GPU and stall the chat
path. In-game: `.ollama status`, `.ollama test <prompt>`. After changing the
model or restarting Ollama, run `ollama reload` in the worldserver console to
re-probe the model (no server restart needed).

Low GPU use is expected: random chatter only fires within
`RandomChatterRealPlayerDistance` (200) of a real player, so with nobody logged
in the bots make ~2 requests/minute regardless of bot count.

Playerbots' own canned chatter is off (2026-10-29), so bots only talk through
Ollama: `RandomBotTalk`, `RandomBotEmote`, `RandomBotSuggestDungeons`,
`EnableGreet`, `EnableBroadcasts` all `0` (backup
`playerbots.conf.bak-prequiet-20261029`), and `AnnounceConsumableUse = 0`
(new upstream 2026-10-31: "Eating [item] (85%)" whispers to the master). `RandomBotEmote = 1` made stacked
raid bots answer each other's `/nod` forever ("At least SOMEONE agrees with
me!"). `RandomBotTalk = 0` also stops the canned chat replies; master commands
use a separate path and still work.

Models live in `F:\OllamaModels` (since 2026-10-29; the old `D:\OllamaModels`
was empty). The Ollama tray app's own "Model location" setting
(`%LOCALAPPDATA%\Ollama\db.sqlite`, `settings.models`) **overrides** the
`OLLAMA_MODELS` env var, so change both.

### mod-bg-slaveryvalley

Map 801, battleground id 31, levels 71-80, 5-10 per team.

**Server side is done** (2026-10-06). `server-data.zip` from the module's 1.0
release was unpacked over `Data/` — 12 DBCs, 6 maps, 7 mmaps, 33 vmaps. The DBC
overwrite is strictly additive; record counts went Spell 49839→49842,
Map 135→136, BattlemasterList 13→14, WorldSafeLocs 685→692, AreaTable unchanged.
The large file-size jump is localization strings (the release ships all 7
locales; the local extraction was enUS-only), not bulk content. The pre-install
DBC folder is backed up next to it as `Data/dbc.bak-20261006_142211`.

**Client side is NOT done.** Each player needs `patch-S.MPQ` (staged at
`F:\azerothcore\client-patches\patch-S.MPQ`) copied into their
`WoW/Data/<locale>/` as `patch-<locale>-4.MPQ`. Supported locales: enUS, enGB,
ruRU, frFR, deDE, esES, esMX.

The README also tells players to install a **modified `wow.exe`** from
third-party file hosts so the client will load an unsigned custom patch. That is
a cracked copy of Blizzard's binary; it was deliberately not fetched. That part
is yours to decide and carry out.

The repo (README, images, `client-side/` source assets) is kept at
`F:\azerothcore\client-patches\mod-bg-slaveryvalley-repo`.

The README targets an AzerothCore revision from 2020 while this core is current.
It compiles clean and the BG registers, but in-game behaviour is unverified.

## The mind service (`leeroy_mind`)

`mind/` is the Python service that speaks for the guild through mod-leeroy's
bridge, on the local Ollama. It has its own venv in `mind\.venv`; run it from
`F:\azerothcore\mind` in Git Bash inside Windows Terminal (in a standalone
mintty window Ctrl+C may not reach python):

```bash
D=F:/azerothcore/mind-data                                  # pass --data-dir "$D" to every command
.venv/Scripts/python -m leeroy_mind run --data-dir "$D"     # the service; Ctrl+C once stops it cleanly
.venv/Scripts/python -m leeroy_mind doctor --data-dir "$D"  # one ok/FAIL/note line per check
.venv/Scripts/python -m leeroy_mind db --data-dir "$D" migrate   # snapshot, then the code's schema
.venv/Scripts/python -m leeroy_mind personas --data-dir "$D" card <guid>   # also export, import, mix
.venv/Scripts/python -m leeroy_mind.library --data-dir "$D" stats   # the game library: build, search
.venv/Scripts/python -m leeroy_mind nightly --data-dir "$D" status  # also run, ledger, undo, enrich, veto
.venv/Scripts/python -m leeroy_mind eval --data-dir "$D"    # weekly evaluation, on a copied database by default
.venv/Scripts/python -m leeroy_mind selfies --data-dir "$D" status  # profile photos; also try (mind stopped)
.venv/Scripts/python -m leeroy_mind console --data-dir "$D" # the guild console; open the URL it prints
.venv/Scripts/python -m pytest -q                           # the unit tests
.venv/Scripts/python -m pytest -m ollama                    # optional: also -m live, -m mysql
```

- `mind-data\` (next to `mind\`) holds all the personas, memories, prompts and
  logs: `mind.toml`, `mind.db`, `library.db` (the game library the mind reads,
  read-only, for facts in its answers), `personas\`, `prompts\`,
  `logs\mind.log` and `snapshots\`. It is never committed.
- Without `--data-dir` the data dir follows the code's folder, so a run from a
  worktree would not use the real one (`run` and `doctor` refuse an empty one).
- `run` takes a snapshot before it starts; each label keeps its newest 10
  (`mind-<label>-<stamp>.db`), so copy one you must keep to another name. After
  pulling new code, run `db migrate` before `doctor`.
- Explicit prompt wording lives only in `mind-data\prompts\` overlays; the
  built-in prompts in the repo stay non-explicit.
- `pytest -m ollama|live|mysql` runs the optional checks against Ollama, the
  running bridge and MySQL; the default run deselects them. `-m live` takes the
  events the bridge queued for the mind, so never run it while they matter.
- Do not run `personas generate`/`seed` or a library build while the mind is
  live: they load another large model next to the live one and evict it.
- Nothing writes a persona while the mind runs. A member who joins the cast
  later (invited, or levelled to 80) has none and stays silent: the mind voices
  only stored personas and native mutes every cast member in mod-ollama-chat.
  The mind logs `cast_without_persona` (their guids) when that set changes and
  `doctor` notes them. With the mind stopped, `personas generate --dry-run`
  shows their slots and `personas generate` writes them (the reflection model,
  a few minutes each); their voice specs are a separate casting step.
- The night: once the owner has been offline for 10 minutes and no offline
  raid holds the GPU, the mind unloads the live chat model, loads the
  reflection model (`[models] reflection` in `mind.toml`, a 27B that cannot sit
  on the 4090 beside it) and runs the night's stages. The owner's login stops
  it and preloads the live model; the next run resumes at the first unfinished
  unit. So a swapped model in `ollama ps` while nobody is online is expected.
- A live lease (his login, an offline raid, the voice service) pins the live
  model (`ollama ps` says Forever). When the last one ends, the pin drops to
  10 minutes (`RELEASED_KEEP_ALIVE` in `nightly/gpu.py`), so a relog finds it
  loaded and an idle evening frees its 16 GB. The embedding model (1 GB) stays
  pinned: the mind embeds through the night.
- Guild life (raid nights, DKP and loot, offline raids, rank, spec and gear
  upkeep) runs with built-in defaults; `[guild]` and `[dkp]` tables in
  `mind.toml` override them.
- Members start conversations with him on two clocks (2026-11-07), per hour of
  his play at chattiness 4 and scaled with it: `[talk] whispers_per_hour` (4;
  `whisper:start`, drawn and lust women most, fond rarely, men only casually,
  lifted by the romance dial) and `openers_per_hour` (6; `burst:to_him`, a
  casual line naming him in party when he is grouped with them, five or fewer,
  else guild, never romance), with the talkative and forward dials moving the
  rate; never in his raid's fight, wipe silence or debrief, while he is AFK or
  in his login's quiet, and a first `(nothing)` is asked again once.
- A group he built with `.playerbots bot addN` (native `guild_group` events)
  raids like a called raid night once he leads it into a raid instance in
  `guild/raids.py`: size and heroic from the instance difficulty, his group's
  guild members as the raid, he leads. Arrival is proved as a called raid
  proves it, by the raid night's inside summon: only members the server finds in
  his copy are the raid and earn attendance. Then tank and loot officer are
  chosen among them as for a called raid, except that a member the add
  command put in a tank slot counts as a tank (native judges bear form, Thick
  Hide and Frost Presence, which the mind cannot see; raid nights the mind
  forms keep its own rule), `SetLootMaster` is sent, and the
  called raid's own start runs (`RaidNight._begin`: record, attendance and its
  DKP, kills, DKP loot). It ends as a called raid does (10 minutes outside,
  logout, "calling it") or when the group breaks up. Never beside another raid,
  never twice for one instance within 7 days, never for 5-mans; nothing is
  formed or disbanded.
  The mind disbands only a group it formed itself (meta
  `guild:raid-night:formed-group`, set at FormRaid, cleared once the disband
  lands; a `guild_group` event outside a raid clears it too), so a group he
  built survives his relogs and mind restarts.
- The guild console (`leeroy_mind/console/`) is a process of its own on
  `127.0.0.1:7881` (`--port`). It prints `http://127.0.0.1:7881/#key=...`; the
  key (one per start, unless `--key-file`) stays in the page's memory (never a
  cookie, which every 127.0.0.1 port would be sent) and goes with each request
  as an `X-Console-Key` header, so a reload needs the URL again. Every request
  needs the console's Host, and its Origin when the browser sends one; a change
  (POST) always needs both the key and the Origin.
  Away from home the owner's devices reach it only over the tailnet, at
  `https://<pc>.<tailnet>.ts.net/` (this PC's MagicDNS name) through `tailscale
  serve` on this machine, never Funnel: `tailscale serve --bg --https=443
  http://127.0.0.1:7881` (undo with `tailscale serve --https=443 off`;
  `tailscale serve reset` clears all of serve's config). The console still
  listens on 127.0.0.1 only. `--allow-host NAME` (repeatable; also `[console]
  allow_hosts = [...]` in mind.toml) accepts a Host of NAME or NAME:443 with the
  Origin `https://NAME`: serve keeps the browser's Host (`r.Out.Host =
  r.In.Host` in tailscale's `ipn/ipnlocal/serve.go`), and X-Forwarded-Host is
  ignored. The two addresses' Origins never cross; every other Host is refused
  as before. `--key-file [PATH]` uses a stable key instead, by default in
  `%LOCALAPPDATA%\leeroy\console.key` (his profile, not mind-data): a missing
  file is made private (control.json's one-entry DACL) with
  `secrets.token_urlsafe(32)`; a file he does not own, that anyone else may
  read (owner and DACL read from the opened file), that is not a regular file
  or that holds fewer than 43 URL-safe characters stops the start. `python -m
  leeroy_mind console rotate-key [--key-file PATH] [--allow-host NAME]` writes a
  new key and prints the URLs to bookmark; a running console keeps the old one
  until it restarts. The start line adds `  tailnet: https://NAME/#key=...`;
  the key is printed there and by rotate-key, never logged. A bookmarked URL's
  fragment syncs with the browser's account like the rest of the bookmark, so
  if a device is lost, run rotate-key. `scripts/ac-console-schedule.ps1
  -Install -AllowHost <pc>.<tailnet>.ts.net` (also `-Show`, which prints the
  task without registering it, `-Remove` and `-Status`) manages the per-user
  logon task `leeroy-console`: the checkout's mind venv `pythonw.exe` runs the
  console hidden with `--key-file <KeyFile> --allow-host <AllowHost>`, only while
  he is logged on, Limited, priority 4 (normal; a task's default 7 is below
  normal), IgnoreNew, no time limit, and starts it again after a failure
  (RestartCount, plus a trigger every 5 minutes). Its output is not kept, so the
  bookmark is `https://<AllowHost>/#key=` plus the key in the key file. Under
  that pythonw.exe the console takes the mind's base interpreter from the mind
  venv's `pyvenv.cfg` (`<home>\python.exe`, `lifecycle.mind_base_python`, as
  `ac-mind.py` does), and the venv's pythonw.exe may control the mind as its
  python.exe may. The mind and the voice service start outside the starting
  process's job (`CREATE_BREAKAWAY_FROM_JOB`; a job that forbids it starts them
  inside, with one `<service>_breakaway_refused` log line) at normal priority, so
  ending the task does not end a mind its console started. A stop or restart asks
  first while the owner is in game or nobody can tell. It reads mind.db,
  mind.log and mind.toml read-only and talks to the voice service through the
  voice page's own socket. Start, restart and stop use
  `leeroy_mind/lifecycle.py`, the rules `scripts/ac-mind.py` uses, so only this
  checkout's own mind venv may control the mind (`--root` names another
  checkout); the voice service's pid must be the base interpreter its own venv
  launcher started. `--no-control` turns start, restart and stop off, and raid
  actions and replies too. The owner's game state is the running mind's open session; while
  the mind or its bridge is down, or the session cannot be read, it is shown as
  unknown, never "not in game". Its Raid and Guild tabs read mind.db read-only
  (raids, attendance, the DKP ledger, loot receipts, the roster, season awards,
  the chronicle); the Raid tab's live state and buttons come from the mind's
  control endpoint. The Personas tab is read-only too (`console/personaview.py`):
  each member's night mood, stance, row toward the owner, strongest ties, memory
  counts, persona version and who last wrote it (seed, owner, generator, the
  night's looks pass or photo-kind pass, a stance move or an undo), open drift
  flags and the night's trait sample, and per member the persona card summary
  (its looks with the photo kind), ties, memories by kind and ledger; a table
  an older mind.db lacks shows as unavailable. Its writes are the profile
  photos (see the selfie worker) and the tuning (`leeroy_mind/tuning/`,
  console `tuneview.py`): the
  owner's dials, stored in mind.db's `tuning_*` tables as an overlay that never
  rewrites a persona. Member dials: talkative, argumentative, forward/flirty
  (women only), humour amount, theorycrafter, lore nerd, drama, jealousy, party
  (drinking and smoking habits), opening up, reliability; world dials
  chattiness, romance and jealousy override mind.toml without writing it. A
  member's own value wins over group offsets, which add up. With nothing set,
  every prompt and weight is what it was (`tests/tuning/golden_prompts.json`).
  Writes and previews go through the mind's `tune` op; a preview runs only
  while the live model is already loaded, never loads one, and its text is never
  logged or stored. `python -m leeroy_mind tuning --data-dir "$D" status|history`
  reads it; `undo N|revert N` work with the mind stopped.
  The page is split into `console/static/` (CSS, a classic `theme.js`, ES
  modules, self-hosted OFL fonts with their licence texts) and is served at
  `/static/` without the key, like the page, since a browser cannot send a
  header for `<link>`, `<script src>` or `@font-face`; the CSP is `'self'`
  with no inline style. Dark is the default whatever the system setting; the
  light/dark switch is remembered in `localStorage` (`guild-console-theme`,
  with the folds and read marks, `guild-console-collapsed` and
  `guild-console-read`). It has 13 tabs: Comms, Raid, Guild, Personas and Mind
  are real; Social, Board, Calendar, Bank, Realm, Training, Dojo and Library
  are placeholders that wait for P3. `GET /api/face?guid=<int>&size=sm|md|lg` serves
  `mind-data/selfies/<guid>/chosen.jpg` (key and Host checked, `image/jpeg`,
  `private, max-age=60`, 404 when absent; the original for every size, the
  browser scales it) and never writes under `selfies/`; faces (initials when
  there is no photo) show in Comms and on the Personas roster. Comms is one
  feed of typed lines and the room lines he heard (from mind.db: a line
  appears once it has played to him, a dropped one does not), channels with
  unread counts (a first open or reload marks history read), the 32-room tree
  (empty rooms greyed, click to join), whispers and the member card. A voiced
  whisper is never typed in WoW: one the voice service dropped because he was
  deafened or had muted that member shows in its thread as text with a "not
  heard (deafened)" or "not heard (muted)" chip and counts as unread; a whisper
  dropped for any other reason does not appear. Push-to-talk the voice service
  recognised but the mind did not match to a line is noted as not sent in a
  Comms toast (8 s), not in the feed. Reply box: `POST /api/say` {channel,
  text[, to]} with channel `guild`, `officer`, `group` (the mind sends it to
  raid when his group is one, else party) or `whisper`, and `to` (a member's
  guid, online, in the cast, not him) for a whisper only; a line starting with
  `/` is refused ("Slash commands aren't sent from here.": WoW would post it as
  words). It needs the key and Origin and the mind's
  control endpoint, and is off with `--no-control`, with the mind or bridge
  down (said as such, not as "not in game") and while he is not in game. One
  line is in flight at a time; the refused text stays in the box. The server
  answers in words; an older worldserver without `owner_say` reads "The game
  server needs the rebuild that adds console replies." Mute and deafen are
  voice-service flags (the page's `self` message; deafen implies mute; both
  reset at each login): mute stops his push-to-talk, deafen silences the room.
  After a release they need the voice service restarted: an older voice
  service ignores `self`.
- The mind's control endpoint (`leeroy_mind/endpoint.py`) is on by default
  (`[control] enabled = false` turns it off; `[control] port`, default 0, picks
  a free port). It listens on 127.0.0.1 only and writes a new key each start,
  with its port and pid, to `mind-data/control.json`, readable by the owner's
  user only (a protected one-entry DACL) and removed when the mind stops
  cleanly. One JSON line per connection, exactly `v`, `key`, `op` (and `args`):
  `status`, `stop` (the mind's own shutdown, the one Ctrl+C runs), `selfie`
  (profile photos: pick, undo, more, status), `tune`
  (the owner's dials: status, set, undo, revert, group, retire, preview) and `raid`
  (`call`, `form` {raid, size, heroic, lead}, `summon`, `start`, `stop`,
  `end`), which enter guild life's raid night where the owner's chat or its
  timers would, with the same guards, and are refused while he is not in game,
  and `say` {channel, text, to} (`owner_say.py`: the shared text rule, one line
  at a time, sent to the bridge as `owner_say`; logs carry op, channel and
  outcome, never the text).
  `stop`/`start` of a raid switch dungeon-clear off and on again mid-raid. The
  console sends the key only to the listener the TCP table shows is the running
  mind's own pid; its Stop and Restart use the clean stop (asked up to five
  times while the endpoint is there but busy), wait up to 60 s for the process
  to end, and otherwise (or with no endpoint: a mind from before it, or one
  with it off) stop it by force and say why.

## The Realm (`leeroy_mind.realm`)

The mind's layer for the world outside the owner's guild: three rival bot guilds (a
progression rival he names, a drama farm, Alliance gankers), their officers' rationed lines in
Trade and General and the gankers' emotes, the race for first kills, canon achievement grants,
the guild bank, the guild calendar, mail to him, the kick guard and the realm's night stage.
Its rows are the `realm_*` tables of `mind.db` (migrations 0009-0016).

- **Off by default, two switches; this install runs it on.** `[realm] enabled = true` in `mind.toml` (read again at each
  hello and login) and native `Leeroy.Realm.Enable = 1` in `mod_leeroy.conf` (`.reload config`;
  while it is 0 native refuses every realm command `realm_off`). Other `[realm]` keys:
  `his_birthday`, `guild_anniversary` (MM-DD, local only), `trial_rank`, `member_cap`,
  `gank_human`. `mind-data/realm/` holds `blocklist.txt` (never committed), `cards/`, `plans/`
  and `approved/`. Without the blocklist, or before the guild's name is cached (meta
  `realm:guild_name`, read once per hello), the screen fails closed and no rival speaks.
- **Whitelists.** Native reads `Leeroy.Realm.PlacesFile` and `Leeroy.Realm.ItemsFile`
  (`leeroy_places.csv`, `leeroy_items.csv`, copied from mod-leeroy's `conf/*.csv.dist`) at each
  `.reload config`, places only once the world is up (so the boot line says `places 0`). A
  relative path resolves against the module config directory, `configs/modules` beside
  `mod_leeroy.conf`, where the mind's `realm/places.py` reads it too; an absolute path is used as
  written; an empty value names no file. A missing or invalid file leaves its list empty and logs
  `realm places file ... so no places` (or items); the `places N, items N` line shows the counts.
  Builds before this fix read a relative path from the worldserver's working directory
  (`RelWithDebInfo`), so the live conf names both by absolute path (2026-11-04); that keeps
  working.
- **Data steps**, from `mind/` with `--data-dir "$D"`: `python -m leeroy_mind realm status`
  and `titles` read `mind.db` read-only; `realm plan rivals --progression-name <name>`,
  `plan canon`, `plan bank`, `plan bank-stock` write only a plan file (rivals asks the live
  model and exits 2 while the night holds the GPU); `realm approve <plan file>` writes only
  an approval bound to the plan's hash; the running mind applies each approved plan once
  (`realm_step`), and a plan edited after its approval is refused for good. `realm canon
  history-ok <seed> <canon>` approves the history files; `realm cards [--show]`. A bank step
  that ends `needs_controller` needs a look at the bank in game, then a fresh plan
  (`plan bank --no-money` or `--first-tab N`). `realm release <root-id> --reason "<text>"`
  (the mind stopped; it holds `mind.lock` throughout) shows one body move stuck unknown in the
  action journal (kind, state, what native said, age, members) and asks for its exact id again
  (`--yes` skips that, never the exact id). It then marks it abandoned (completed, holding
  nobody) and writes meta `realm:action:abandoned:<id>` with the time, the reason and the row as
  it was. It refuses any other state (conflict too), a transport not yet settled, a WG, ack or
  result root and one with children. A released root that native evidence later contradicts goes
  to conflict and stays held. Native still fences a body it holds. The prune never deletes a
  grow's row or a released one, and a release receipt also stops a grow, so no grow with a stored
  outcome is ever sent again.
- **Exactly once.** A command that moves no body (speech, actor sets, renames, bank money, tabs
  and gift withdrawals, grants, calendar, mail) goes through the direct ledger `realm_direct`
  (prepared, dispatched, acked, refused, unknown, abandoned): an unknown one is never sent
  again, and a restart turns dispatched into unknown. Body moves go through the action queue
  and the body ledger. Native's `busy:<owner>` is stored as `busy_<owner>`. Acked calendar
  signups and speech lines are pruned 30 days after they settle; other acked rows stay. The
  actor set is resent from memory at each hello; if it ever moves into the ledger, its ref
  must name the process. One fenced body (`busy:raid_owner` for a member in his group, a
  training or Realm owner, `not_bot`) makes native refuse a whole body move at its ack before
  reserving anyone: the action journal settles that as refused, holding nobody, and each start
  reads again a stored unknown whose only evidence is such a refusal. The same goes for a
  rearm, `rpg_status`, `equip` or `set_profession` acked and then failed at one body's step for a
  fence or a `busy:` reason such as `busy:combat` (`STEP_REFUSALS`): native refused that body
  before changing it and released them all, so it settles `step_refused` and Recall goes on to
  the defaults. Any other failed job (`operation_transition_unknown`, a walk, leave, duel or
  grow) stays unknown. A root that settles unknown or conflict logs a warning in mind.log,
  `realm_action_unknown` (or `realm_action_conflict`) with its id, kind, reason, native's reason
  and member count, no names: from then on it holds its manifest.
- **Rulings.** His raid holds exactly his raid: waiting for the call, called and forming hold
  the roster and his group; assembling and running hold the raid's members and his group;
  everyone else, rival officers included, may move (an unreadable member list holds everyone).
  Rivals never kill a boss before the guild and never an Ulduar hard mode. Training keeps its
  own benchmark rival choice and asks the race (`app.realm.race.benchmark_allowed`: True or
  None, never False). A member he took back counts as a full member again. No model call
  while he is offline; no rival line in his raid's combat, wipe silence or debrief.
- **Night.** With the Realm on at the mind's start, stage `realm` runs after `stories` on the
  reflection model: 0-2 Trade beats per rival guild (said the next day, screened again), up to
  3 forum posts, 3 letters (written through the post office) and 2 write-ups. It counts 0 and
  writes nothing while `[realm] enabled` is false.
- **Calendar and mail.** The desk posts raid nights (from his logins), Thursday's officer
  meeting and the members' card birthdays a week ahead, signs members up and notes the raid
  nights he missed; native keeps 100 guild events. Mail: a letter once per absence, a week
  after his last logout; at most 2 gifts a product week (a spare BoE from the bank, else
  flasks); holiday and birthday gifts; at most 3 a day, sent only while he is online.

## The voice service (`leeroy-voice`)

`voice/` adds local room playback and push-to-talk to the mind. It is a separate
GPL-3.0-or-later process, with its own `voice\LICENSE` and `voice\.venv` on
**Python 3.12**: the pinned speech/audio dependencies are kept apart from the
mind's Python 3.14 environment. Expressive engines use another venv under
`voice-data\engines\<engine>\.venv`; do not install their torch stack into the
mind or voice-service venv.

**Installed on 2026-11-02**, together with the mind's nightly, guild-life and
library work. Changes are still developed and validated in a separate worktree.
Before deploying one, complete an isolated rehearsal using a copied database,
FakeBridge, FakeLLM and separate loopback ports. It must not contact the live
bridge or load/unload models on live Ollama. Resolve migration numbering
against the merge target, merge into the code the live mind runs, then use that
live code's own `db migrate` with the mind stopped. Do not run a development
migration on the live database. Acceptance with WoW focused is a check of the
next normal session after each deployment.

Playback keeps each engine's native sound (`[sound] effects_enabled = false`).
The owner wants native voices only: do not turn the old microphone, codec or
room effects back on, and do not add post-synthesis processing to hide
artefacts such as crackling; find and fix the cause.

- The service listens on loopback only: `voice.toml` defaults to 7879 for the
  mind connection and 7880 for the browser UI. The mind's `[voice]` endpoint
  must match the service's `[mind]` endpoint. The mind doctor reports spec
  coverage and optional service status; a stopped voice service is a note.
- `voice-data\` holds `voice.toml`, `decision.toml`, `lexicon.toml`,
  `attitudes.toml`, models, caches, UI state and logs. The local decision records
  the listening choices; never commit it or device settings. Voice specs,
  `brief.json` and synthetic `.ref.wav` clips live in `mind-data\voices\`.
- `LEEROY_VOICE_DATA` overrides the voice data directory. `LEEROY_MIND_DATA`
  selects the mind data directory used to locate voice specs. Defaults follow
  the code's checkout, including worktrees. For tests with a borrowed venv,
  set `PYTHONPATH` to the intended worktree's `voice` or `mind` directory and
  check the imported package's `__file__`; an editable install may point back
  at the live checkout. Use scratch data directories for development commands.
- `scripts/ac-voice.sh start|stop|status` uses `voice\.venv` under `AC_ROOT`
  (this checkout by default) and `python -m leeroy_voice where` to resolve its
  data directory and ports.
  It starts a hidden process. The mind can autostart it when configured; do not
  enable that in a development checkout against live data.
- On logout, playback fades, push-to-talk disarms, the microphone closes,
  WoW's volume is restored and GPU leases are released. A refused or failed
  expressive engine falls back to the member's Kokoro blend; cached expressive
  callouts can still play. GPU admission requires headroom beside the live chat
  and embedding models, and is measured before changing local settings.
- The browser page can send `{"type": "self", "mute", "deafen"}` (booleans):
  mute stops push-to-talk, deafen silences the room and implies mute; both
  reset at each login. The service reports them in its `self` state.
- Push-to-talk uses Raw Input and does not swallow the key. A tap under the
  configured hold threshold is ignored. WoW and the listener need compatible
  elevation: a non-elevated listener cannot hear an elevated foreground game.
  `selftest --ptt` checks injected key events; it does not prove capture with
  WoW focused. Check that and Windows communications ducking during acceptance.

From this checkout's `voice/`, with data-directory overrides set as appropriate:

```bash
.venv/Scripts/python -m leeroy_voice where
.venv/Scripts/python -m leeroy_voice doctor
.venv/Scripts/python -m leeroy_voice devices
.venv/Scripts/python -m leeroy_voice fetch-models --only kokoro,parakeet,silero
.venv/Scripts/python -m leeroy_voice selftest             # writes a neutral stock line
.venv/Scripts/python -m leeroy_voice selftest --guid 101 --cpu  # use an actual spec's guid
.venv/Scripts/python -m pytest -q                       # isolated suite; no live devices/models
```

The casting commands are `cast --brief <brief.json> --out <specs-dir>`,
`references --brief <brief.json> --specs <specs-dir> [--only <guids>]`,
`audition --specs <specs-dir>` and `lock <guids|all> --specs <specs-dir>`.
`references` runs on the CPU and prepares stock clips; locked specs are kept.
`audition` uses each saved spec's engine and reference, including locked voices
whose engine differs from the current decision. Expressive audition workers run
offline on the CPU, one engine at a time. An unavailable or failed engine fails
the command instead of substituting a fallback voice; check for a successful exit
before using the page, since an older page can remain after a failed run.
Models load offline after `fetch-models` has fetched and verified the pinned files. Default
tests deselect real engines, audio devices, Windows, GPU, Ollama and live-service
checks; those checks need a deliberate isolated setup.

## The selfie worker (`leeroy-selfie`)

`selfie/` renders the guild's profile photos: four candidates a member, RealVisXL V5.0 on the
local GPU, each checked by a local NSFW classifier before it is written. It is a separate
GPL-3.0-or-later process with its own `selfie\LICENSE` and `selfie\.venv` on **Python 3.12**
(never the mind's or the voice service's), and the mind never imports it: it starts the worker
as a subprocess at night and talks JSON lines over stdin and stdout.

- **Off by default.** `[selfies] enabled = true` in `mind.toml`, read once at the start. Other
  keys: `python` and `data` (absolute paths; default `selfie\.venv` and `LEEROY_SELFIE_DATA`,
  else `selfie-data` beside the repo), `nsfw_max` (0.2), `steps` (30), `guidance` (5.0), `size`
  (768, 896 or 1024), `min_free_gib` (11.5, the worker refuses to load with less VRAM free).
  An invalid table is logged and means off. With it off the pick service still works for sets
  an earlier night rendered, and "4 more" is refused.
- **Worker commands**, from `selfie/`: `.venv/Scripts/python -m leeroy_selfie fetch-models`
  (pinned revisions into `<data>/models`), `doctor` (`--quick` skips torch and hashing; the
  mind's doctor runs that), `where`, `smoke --out DIR` (one invented person; needs the GPU),
  `pytest -q` (no model, GPU or network).
- **Two night stages, only with `enabled = true`.** `photo` runs after `enrich` on the
  reflection model: it picks a photo kind, framing, setting, outfit and expression for each
  member who has looks and no kind yet (stored as a new persona version, screened for the
  owner's real name, member names, era and explicit wear). `selfies` runs after `regress` as an
  image stage: it queues a first set for each member with looks and a kind, plus any "4 more"
  asked for, and renders up to `selfie_per_night` (120) sets.
- **GPU rules.** The image stage unloads every chat model but the embedding model (the worker
  needs the VRAM) and never renders while a live lease is held (his login, an offline raid, the
  voice service): it ends without a failure and the sets wait for the next night. A night with
  nothing to render takes no GPU (`wants_gpu`). The worker's start checks the models without
  hashing them; a VRAM refusal keeps its pipeline on the CPU, and after making room the stage
  asks it once more (`selfies_worker_start` logs each start's `ms`, `check_ms`, `load_ms` and
  `free_gib`). An OOM makes room and retries once, then ends the stage. His login cancels the
  stage, whose last act is to end the worker's whole process tree; the arbiter's live-model
  preload starts only after that tree is gone.
- **Orion beside the worker.** mod-ollama-chat's night requests can load Orion into the VRAM
  the worker leaves, partly on the CPU, and Ollama reuses that runner for a preload at the same
  `num_ctx`. So the night makes room once more after the image stage (and `selfies try` once
  its worker is gone), and after any image stage the next hand-over (his login, voice, an
  offline raid) unloads a loaded live model before its preload: one cold load (about 10 s) at
  the first login after a render night. At
  release, `ollama ps` after a render night and his next login must show Orion at `100% GPU`.
- **Storage.** Sets and picks are rows in `mind.db` (the selfies migration,
  `migrations/*_selfies.sql`); the images are files
  under `mind-data\selfies\<guid>\`: `sets\<n>\c0.jpg` to `c3.jpg` with `.thumb.jpg`
  beside each (a set renders in `<n>.part` and is renamed when done), `chosen.jpg` (the photo
  in force) and `history\<pick>.jpg`. The database is the truth: the mind makes the files agree
  with it once at start, before any loop runs. `chosen.jpg` is replaced by a rename, so a
  reader sees the old photo or the new one. Generated images are never committed.
- **Picking.** The console reads the sets (`GET /api/selfies`, `/api/selfies/images`,
  `/api/selfies/member`) and sends `POST /api/selfie` (pick, undo, more) to the mind's `selfie`
  op, behind the same key, Origin and `--no-control` gates as raid actions and dial changes.
  On the page (`static/js/selfies.mjs`, words in `selfiemodel.mjs`) the Personas tab opens with
  "Choose selfies": the counts, five waiting members a page with their four thumbnails (a click
  picks one) and "4 more", the mind's answer with an Undo after a pick, and "Recent choices" from
  the running mind's own list (never the view's) with Undo where the mind says it would be taken.
  A member's detail has "Profile photo": the photo in force as their face, "4 more" and their
  history with Undo. With the mind down or control off the buttons are disabled and say why. A
  pick or undo (the `selfie` event) makes every page ask for that member's face again at once
  (`FaceCache.forgetKey`) instead of after its 60 s. `leeroy_mind selfies status`
  prints the queue and the latest night's stage records, and `selfies try [--guid N | --limit N]`
  renders a trial set into `scratch\selfies-try` of the data dir with the mind stopped.
- **Content limits.** Adults only, clothed and non-explicit, no real person's likeness, and no
  person's name ever reaches a prompt. Logs hold ids, counts, scores and timings, never a prompt
  or persona text.
- **Rehearsal.** `mind/tests/selfies/test_selfies_rehearsal.py` drives a real `MindApp`, its
  control endpoint and a real console over loopback with the FakeBridge, FakeLLM and a fake
  worker, through two nights and his login. A rehearsal against a copy of the live database
  must use the same fakes and its own free ports, and any console it starts takes
  `--voice-ui-port` of an unused port. The real worker and the GPU are checked at the release.

## The dojo

`dojo/` is an isolated environment for training the bots' combat policies with
reinforcement learning. It runs its own AzerothCore worldserver, MySQL instance,
ports and fixture characters, never the live realm, and everything in it stays
apart from this install. Read `dojo/README.md` before touching it.

- Source here: `dojo/` (Python, `leeroy_dojo`), `modules/mod-dojo` (the native
  module, built only into the dojo's isolated core; the CI build compiles its
  disabled variant), `patches/dojo/mod-playerbots/` (the dojo's own playerbots
  commit, applied on top of the local commits in the dojo's checkout only) and
  `scripts/dojo/` (PowerShell: build, configure, patches, preflight).
- Environments and recordings live in `dojo-runtime/` and `dojo-data/` beside the
  install root and are never committed.
- Never point the dojo at the live install, database or ports. `scripts/dojo/preflight.ps1`
  refuses a layout that overlaps them.

## Outstanding

- `mod-bg-slaveryvalley` server data + client patch (above).
- Zip-sourced modules (transmog, individual-progression, slaveryvalley) have no
  update path; converting them to git clones would let `ac-update.sh` cover them.
- `ac-update.sh` only checks `<repo>/conf/*.conf.dist` for new keys, which
  misses the core's `worldserver.conf.dist` / `authserver.conf.dist` (under
  `src/server/apps/`).
