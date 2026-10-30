# AzerothCore (WotLK 3.3.5a) — private server

A WotLK private server built from the **mod-playerbots fork** of AzerothCore,
with bots and several gameplay modules. Windows 11, MSVC, MySQL 8.4.

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
README, `scripts/`, `config/`, `patches/`, `modules/` (mod-leeroy) and `.github/`. The core repo, the
build tree, the client, boost, DB dumps and the zips stay out, and so do the
local dev docs (`docs/`, `private/`).

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
- CI (`.github/workflows`, see README): `ci` (shellcheck + gitleaks), `patches`
  (applies the patches to upstream playerbots nightly; failure opens a
  `patch-drift` issue, which means rebase before the next `ac-update.sh`),
  `build` (weekly Linux compile, `-Werror`), `release` (on `v*` tags). No
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
back to compiled-in defaults. `ac-update.sh` reports them, for the modules'
`conf/*.conf.dist` and for the core's `worldserver.conf.dist` / `authserver.conf.dist`
(under `src/server/apps/`).

## Scripts

Run from `F:\azerothcore\scripts` in Git Bash.

```bash
./ac-update.sh [--dry-run]   # ff-only pull of core + git modules; refuses if dirty
./ac-backup.sh [label]       # mysqldump all 4 DBs, keeps newest 5
./ac-build.sh                # preflight + cmake configure + build
./ac-server.sh start|stop|status
./ac-snapshot.sh             # live configs (masked) + playerbots and ollama-chat patches -> config/, patches/
```

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
| `mod-transmog` | azerothcore/mod-transmog | git since 2026-10-30 (history attached in place to the unpacked zip, which matched `0d85cbc` exactly; repo-local `core.autocrlf=false`, `core.filemode=false`). **Upstream `327753a` renames and moves its SQL files and deletes `data/sql/updates/`**: review before the next `ac-update.sh`, since the DB updater may re-run the renamed files. |
| `mod-individual-progression` | ZhengPeiRu21/mod-individual-progression | git since 2026-10-30 (attached in place at `60336b3`; same repo-local settings as transmog). Rewrites a lot of world/character data. Gates raids per character by tier (ICC needs 16, from killing Anub'arak in ToC; tier = highest rewarded quest `66000+N`). `.ip set <name> <tier>` **ignores the name** and applies to your target or yourself, and from the console it would dereference an empty player: use it in game with no target. |
| `mod-dungeon-clear` | jrad7/mod-dungeon-clear | git clone since 2026-10-29 (was a zip of `60f3d98`). **The autonomous tank lead**: `.dc on` (or `dc on` in party chat) inside an instance makes the group's tank bot route boss to boss over the navmesh, pull (Dynamic/Leeroy/Advanced), run scripted events (all 16 WotLK 5-mans), loot, rest and rez. Raids too: trash flow with a readiness quorum, pre-boss muster, wipe recovery; boss fights are left to the playerbots raid strategies. Companion addon: jrad7/mod-dungeon-clear-addon. It hooks playerbots without patching it (appends to the shared context registries). Ships its own `.cmake`; folder name must stay exact. Reads `PlayerbotAIConfig` members directly, so it breaks when playerbots renames them. AGPL-3.0. |
| `mod-bg-slaveryvalley` | zip (no git; upstream Helias/mod-bg-slaveryvalley is a monorepo with the module under `server-side/`, so it can't be cloned in place) | Custom battleground, map 801 / BG id 31. The local copy matches upstream `9be4de0` exactly. Server side complete; **client patch still undistributed** — see below. |
| `mod-ah-bot-plus` | NathanHandley/mod-ah-bot-plus | Auction house bot (git clone since 2026-10-30). Sells under, and buys player auctions as, the ordinary account `ahbot` (auth id 352): characters Fatiggeritch (3502) and Bonqueefa (3503), set in `AuctionHouseBot.GUIDs` (comma list, no spaces). Never use a playerbot character for it, and never enter the world on those characters. Seller and buyer both on; everything else is the `.dist` default (about 150 listings a minute per house, up to 15000 each). GM commands: `.ahbot reload` (re-reads the conf, GUIDs included, no restart), `.ahbot update`, `.ahbot empty`. Loader `Addmod_ah_bot_plusScripts`, so the folder name must stay exact. No SQL. |
| `mod-leeroy` | this repo, `modules/mod-leeroy` | The guild bridge. `azerothcore-wotlk/modules/mod-leeroy` is a **junction** to `F:\azerothcore\modules\mod-leeroy`: remove it with `rmdir`, never `rm -rf` (that deletes the source through the link). Pins the cast (guild members at 80+, minus `Leeroy.HumanGuid`) via the playerbots pin API, mutes them in mod-ollama-chat, and serves NDJSON on `Leeroy.Bridge.Host`:`Leeroy.Bridge.Port` (`127.0.0.1:7878`, one mind at a time). Keys: `Leeroy.Enable`, `GuildId`, `HumanGuid`, `KeepCast`, `BringCastOnline`, `ExcludeFromOllamaChat`, `Bridge.*`. Start line in `Server.log`: `mod-leeroy: cast N members, pinned N ...`. Smoke test: `python modules/mod-leeroy/tools/bridge_smoke.py --expect-cast N` (0 pass, 1 fail, 2 no server or another mind connected); `--listen S` prints events, `--send` sends raw commands. |

`mod-playerbots`, `mod-ollama-chat`, `mod-dungeon-clear`, `mod-ah-bot-plus`,
`mod-transmog` and `mod-individual-progression` are git repos, and `mod-leeroy`
lives in this one. `mod-bg-slaveryvalley` is the only zip extraction left, and
`ac-update.sh` skips it; updating it means re-copying `server-side/` from upstream.

### Playerbots config prefix is `Playerbots.*`

Playerbots #2854 (pulled 2026-10-29) renamed every key from `AiPlayerbot.*` to
`Playerbots.*` with no fallback: an unmigrated conf silently runs every option
at its default. The live `playerbots.conf` and the `.bak-500bots` /
`.bak-1000bots` variants were migrated; `.bak-prepolish` / `.bak-prepvp` still
use the old prefix. Pre-migration copy: `playerbots.conf.bak-preprefix-20261029`.
If `ac-update.sh` reports hundreds of "new keys" at once, suspect a rename.

### Local patch: randombots in the Raid Browser

Playerbots randombots only copy Dungeon Finder queues that real players are
in, so the Raid Browser was always empty. Local commit `1257c843` on
`mod-playerbots` `master` ("List idle randombots in the Raid Browser") adds
`RandomPlayerbotMgr::CheckRaidBrowser()`: once a minute on the world thread it
keeps `Playerbots.RandomBotRaidBrowserListings` (20) idle bots per faction
listed for up to 3 raids of their level/expansion, each for 50-150% of
`RandomBotRaidBrowserListTime` (1800 s), and drops a listing once the bot is
invited. Listings log `lists in the raid browser` to `Playerbots.log`. It
needs the raid browser bit in `DungeonFinder.OptionsMask` (now 7; it was 5,
which disabled the raid browser entirely; `lfg options 7` sets it live).

### Local patch: `.playerbots bot add5|add10|add25|add40`

Local commits `ed5a442f`, `e72145d4` and `1f1cb5cd` (add5) on `mod-playerbots`
`master`. Tops the player's group up to a 5-man party or a 10/25/40-man raid
with addclass bots: 1/1/3, 2/3/5, 3/6/16 and 4/10/26
tank/healer/damage, counting roles already in the group (so re-running it only
fills gaps). Classes come from fixed rotations in `PlayerbotMgr.cpp`
(`RAID_TANKS`, `RAID_HEALERS`, `RAID_DAMAGE`) matched to
`Playerbots.PremadeSpecName` by name; each bot gets that premade spec, glyphs
and fresh gear in `OnBotLogin`, plus `formation arrow`, and damage bots get
`co +wait for attack` with a 4 s hold so the tank opens pulls (`e72145d4`).
Limited by `Playerbots.MaxAddedBots` (40).
Do not run `.playerbots bot init=auto` on such a raid: `Randomize()` re-rolls
the spec. Use the bots' `autogear` chat command instead.
Bots take the master's level, and the main character is 83 (GM-levelled past
`MaxPlayerLevel = 80`). Playerbots' talent code only knew spec links up to 80
and gave such bots **no talents at all**: no tank strategy, so dungeon-clear
said "No tank bot found". Local commit `8040a53e` clamps to 80.
Do not pass party/raid leadership to a bot: every bot then runs `reset botAI`
(`ResetStrategies`), which drops `wait for attack` (formation survives).

### Local patch: pinned random bots

Local commit `e847415b` on `mod-playerbots` `master` ("Let other modules pin
random bots"). An API on `RandomPlayerbotMgr` that mod-leeroy uses to keep the
owner's guild of ~106 level-80 random bots permanent and always online. No
config key: nothing changes until a caller pins something.

- `uint32 SetPinnedBots(std::unordered_set<ObjectGuid::LowType> const& bots, bool keep, bool online)`,
  world thread. Replaces the pinned set; accepts only characters on RNDbot
  (type 1) accounts and returns how many it accepted. The account list is
  filled at playerbots init (`AssignAccountTypes`), so call it after that.
- `bool IsPinned(ObjectGuid::LowType bot) const`, any thread (mutex).
- `online`: every 30 s `UpdateAIInternal` logs in offline pins (at most
  `RandomBotsPerInterval` per pass) and adds them to `currentBots` regardless of
  `bot_count`; an expired `add` is re-armed instead of logging the bot out.
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

`Revive()` is guarded too (commit `ac039eba`): `FindCorpseAction` calls it
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
`maintenance` to bots in their group; a pin-safe top-up is planned for P1d.

Commits here never carry a `Co-Authored-By` or other attribution trailer.

Both local patches: `ac-update.sh` now rebases repos that are ahead of
upstream instead of refusing, so they are carried across updates; a conflict aborts the
rebase and stops the script. Upstreaming it (PR to `test-staging`) would drop
the need.

### Local patch: mod-ollama-chat speaker filter

Local commit `072d13d` on `mod-ollama-chat` `main` ("Let other modules keep
chosen bots silent"). Adds `OllamaDispatch_SetSpeakerFilter(std::function<bool(uint64_t botGuid)>)`
to `mod-ollama-chat_dispatch.h`; `OllamaDispatch_Submit` copies the filter under
a mutex, calls it outside the lock and drops the request (returns `false`) when
it says `false`. Inert until set. mod-leeroy uses it to keep guild cast members
silent so only their own mind service voices them. `ac-snapshot.sh` now exports
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
`playerbots.conf.bak-prequiet-20261029`). `RandomBotEmote = 1` made stacked
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

## Outstanding

- `mod-bg-slaveryvalley` server data + client patch (above).
- `mod-bg-slaveryvalley` has no update path (see the Modules table).
