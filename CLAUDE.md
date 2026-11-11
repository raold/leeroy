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
not clear an outstanding native or Python journal. The global training-cleanup
flag (`TrainingCleanupPending()`: Realm `busy:training_cleanup`, the queue fence,
play style) fences every body for at most ten minutes of an unproved cleanup;
after that only bodies a job still keeps or a journal names stay fenced, and new
training stays refused (`cleanup_pending`) until cleanup is verified. An offline
raid's or benchmark's unrestored training receipt (`OfflineTrainingCleanupPending()`)
is not bounded: it still fences every body until restored. Server.log names the
adapters keeping cleanup pending, and its all-clear says `released, not verified`
instead of `verified` when a job gave up on a member. Ten minutes after a dummy
trial's `cleanup_failed` terminal it abandons each blocked member it no longer
holds, journaled or not (`cleanup_abandoned guid=`; a journal fences its own body
and is restored by its adapter), and keeps fencing the members it still holds or
is still returning; with none left it ends and sends
`dummy_cleanup` (`released` with its unfinished members, or `restored` once every
return is proved). Journals stay.

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
pulls and restores matching scores. A lab blocked on an unproved trial holds its
members until, with the mind stopped, `lab-clear` clears it on native evidence
the mind stored: that trial's `dummy_cleanup` receipt, a terminal proving every
return, or a hello from a worldserver started after the trial (`started_ms`).
A lab blocked with no trial in flight needs no evidence (probes and priming touch
no body). It sends nothing, keeps every receipt (an attempt keeps its earlier
restoration state as `restoration_before`), frees the members, commits only a
record the body holds accept, and refuses in words without evidence or when a
member has a standing setup; `lab` shows the evidence, and health FAILs a block
older than a night. The first dummy trial after a worldserver start is refused
`driver_pending` while native logs the DC test driver in; the lab retries that
leg once, 60 s later (`training_lab_driver_retry`).

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
./ac-release.sh [--dry-run] <commit>... # deploy merged main commits onto the live release branch
./ac-release.sh --native [--dry-run] <commit>... # also stop servers and build with AC_JOBS=4
./ac-build.sh [--no-configure | --preflight]   # preflight + cmake configure + build; --preflight stops after the module checks
./ac-server.sh start|stop|status # also starts/stops the mind; stdout: mind-data/logs/mind-stdout.log
./ac-voice.sh start|stop|status # voice service; reads its data directory and ports from `where`
./ac-snapshot.sh             # live configs (masked) + playerbots and ollama-chat patches -> config/, patches/
```

`ac-update.sh` reports template keys the live file lacks (even as a comment) and live keys the
template no longer has (even as a comment), for core and module configs, plus suspected prefix
renames (at least 20 additions and 20 removals); values are never printed, and an unreadable
file is reported without stopping the update. `--dry-run`
reports the checked-out templates and still fetches metadata, but never merges or rebases.

`ac-release.sh` refuses an online/unknown owner, open guild or owned offline raids,
a dirty tree, a branch outside `release/*` or `codex/live-release-*`, a native build
lock, or a commit not present on the local `origin/main` (fetch before releasing).
`--allow-raid ID` explicitly excludes a known orphan; repeat it for multiple IDs.
Each exception is printed with the rollback instructions. Mind releases allow only
`mind/`, `selfie/`, `voice/`, `CLAUDE.md`, `README.md` and `references.bib`; native
releases additionally allow `modules/`, `config/` and `patches/`. Dry-run checks
and prints the plan without cherry-picking, stopping services or taking snapshots.
Voice-file releases require the separately managed voice service to be stopped
first (its OS lock is probed without loading models); this tool does not enable
or restart voice. Real releases hold the checkout lock before capturing the
rollback target and running preflight, so a delayed invocation cannot undo a
release that finished while it was checking the database.
After cherry-picking it stops the mind, takes an SQLite online backup in
`mind-data/snapshots/pre-release-*.db`, migrates with the new code, restarts services
and the optional `leeroy-console` task, then prints health. Native releases save
the old EXE/DLL files alongside the snapshot before building; a failed build
restores those files and the original checkout before restarting the old servers.
Native stop is confirmed separately from the best-effort server wrapper: both
server processes must be absent from a successful, well-formed process listing.
Unavailable status or a surviving process prevents build/restore writes; the
printed native rollback command applies the same check before resetting files.
Failures before the completed restart attempt rollback; a health failure leaves
the running release in place and prints recovery instructions. Every exit prints
the reset target, snapshot and native rebuild command.
The printed rollback also restarts the optional console task after restoring the
checkout and database, so it reloads the matching code.
Exit codes: 0 success, 1 refused before changes, 2 failure after changes. Test only with
`bash scripts/tests/ac-release.sh`; it uses temporary repositories and stub services.

`./ac-health.sh [--json] [--toast]` (`python -m leeroy_mind health`) checks the live install
read-only, one ok/FAIL/note line each, exit 1 on any FAIL: the servers, Server.log's cast line
since worldserver started (floor `[health] min_cast`, default 100; a note when the log begins
later, since `.reload config` reopens Server.log empty in its `w` mode) and no `so no places`, the
mind and mind.log, the control endpoint's `status` (it reports `bridge_connected`, `cast` and
`schema_version`), the console and voice (notes when down), Ollama's `/api/ps`, and stuck
activity (no `tg_pull` in 48 h with telemetry on and the overhead gate passed, a note while the
gate is in phase `ab`; a realm hold or `unknown` root older than 1 h; cast without personas). It
also fails an owned offline raid more than 30 minutes past its deadline or any guild raid
open longer than 6 hours. Before native progress creates a local raid, a valid owned receipt
is identified by its command ID and keeps the same deadline check. The latest finished night's
failed units are grouped by stage
(note for 1-9, FAIL at 10), using stored counts with a bounded log fallback and an exception
code when available. Log-only counts require historical coverage spanning the night and each
intervening day; newer, missing or truncated logs are an incomplete observation, never proof
of a clean night. Evidence cycles use the critic's shared persisted edges: the count is
memories whose ancestry reaches a cycle, not edges (note at 1, FAIL at 50). Its scan stops
at 100,000 rows/nodes or 0.8 seconds and labels incomplete results. Queued selfie sets older
than 30 hours are a note. A training lab blocked longer than a night (`tg:lab:owned` phase
`blocked`, from `halted_at`) FAILs with its halt, night, trial and the `training lab-clear`
command to run with the mind stopped; a younger block is a note. With `[realm] enabled = true`, a missing or entry-less
`realm/blocklist.txt` or no cached guild name (meta `realm:guild_name`) FAILs: the Realm's
screen cannot be ready, so rivals stay silent (the night also logs `realm_night_unready` as a
WARNING with `reason=` `blocklist_missing`, `guild_name_missing` or `blocklist_empty`). The
newest weekly evaluation incomplete, an empty `probe_run` while a secret memory or fact exists,
and offline raids blocked on each of the last 3 whole days are notes. Stale Realm roots include kind, member count and the exact
`realm release <id> --reason reviewed_stale_hold` command to inspect with the mind stopped;
the release command still enforces its own eligibility checks. Missing newer tables are
reported as not set up. These checks only report state and never repair it. Health
never dials the bridge (7878), loads no model, starts nothing and never probes `mind.lock` or
`voice.lock` (running comes from the process list, `control.json`, `voice.pid` and the TCP
table). `--toast` shows one toast per new failure and writes only
`mind-data/health-state.json`; a read-only open can leave SQLite's empty `mind.db-wal` and
`mind.db-shm` behind while the mind is stopped, as doctor and the console do.
`ac-health-schedule.ps1 -Install|-Remove|-Status` manages a per-user task that runs it every
15 minutes while he is logged on (not installed by default).

### Off-machine guild backups

`./ac-guild-backup.sh [--with-db]` backs up the guild to an already mounted destination.
Create `%LOCALAPPDATA%\leeroy\backup.toml` locally, outside git (TOML single quotes
keep Windows backslashes literal):

```toml
destination = '\\nas\private-backups\leeroy'
keep_daily = 7
keep_weekly = 8
# staging_dir = 'D:\leeroy-staging'   # optional, absolute, local
```

Use a destination on another machine; a second folder on F: does not protect against
drive failure. The script uses the logged-on user's existing share access and stores no
credentials. The backup contains private guild data: restrict the share to its owner.
It creates its own marked `leeroy-guild-backups-v1` directory under the destination,
which must already exist. It refuses source/destination overlap, symlinks and Windows
reparse points. It never mounts a share, starts a service, or contacts a model.

- **Contents.** Both `mind.db` and `library.db` are required. It includes the rest of
  `mind-data`: personas and versions, memories/history, prompts/overlays, voice specs
  and reference clips, Realm plans/blocklist, and selfie sets, picks and history. From
  `voice-data` it includes only `voice.toml`, `decision.toml`, `lexicon.toml`,
  `attitudes.toml` (when present). `--with-db` adds the newest timestamp-named
  `ac-backup.sh` set's four nonempty SQL dumps, rejecting incomplete/error sets.
  Run `ac-backup.sh` first if a fresh game snapshot is wanted; these older dumps are
  not synchronized with the mind snapshot.
- **Exclusions.** At any depth it omits `logs/`, `scratch/`, `snapshots/`, `models/`,
  `cache/`, `caches/`, `__pycache__/`, unfinished `.part` files/directories,
  `control.json`, `console.key`, runtime lock/PID files, files the services rewrite
  continuously (`health-state.json`, `wow-volume.json`, `ui-state.json`,
  `dojo-endpoint.json`, `worldserver.pid`; regenerated on start and they would fail the
  stability scan) and SQLite `-wal`, `-shm`, `-journal` files. Logs/scratch/snapshots are disposable local history; models and
  caches can be fetched again. Worker `selfie-data`, voice engines/cache/UI state,
  the console key in the owner profile and everything outside the listed data roots
  are never selected. Runtime control credentials must never be restored.
- **Consistency.** A local temporary capture uses SQLite's online `backup()` API in one
  step per database (`pages=-1`): a single read transaction, a consistent snapshot in WAL
  mode that never blocks the mind's writers. A stepped copy restarts whenever another
  connection writes between steps and, measured on a 20 MB database with a continuous
  writer, needed about 59,000 steps and 116 s; the single step has no deadline and is
  bounded by the database size. Then `PRAGMA integrity_check` for each copied database. The ordinary file inventory
  and SHA-256 contents must match on both sides of the database copies. File changes
  retry the capture up to three times; persistent changes fail without publication or
  pruning. SQLite can keep writing throughout (no service lock is taken). Each database
  has its own transaction; there is no global transaction across both databases and
  the filesystem. For a strictly simultaneous snapshot, stop the mind, console,
  voice and any other data writers first. Online backups establish stable file
  contents around the database snapshots; they cannot make an in-flight application
  operation atomic. Restore startup reconciles selfie files with the database.
- **Transfer and verification.** A dated set is a checksummed `manifest.json` plus
  an ownership marker. Every payload file has a size and SHA-256. Shared objects are
  addressed by that digest, so unchanged JPEGs are not uploaded again; there are no
  hard links, filesystem-specific deduplication calls, or external Python packages.
  This trades local hashing and destination verification reads for less upload space
  and traffic. A failed upload resumes its verified byte prefix on the next run; an
  existing object is trusted by its digest name and size (it was verified before it was
  renamed into place) and is not read back; every newly written object is verified. Every
  referenced object is re-read in full when the last full verification (recorded in
  `.verify-state.json` in the store) is over 7 days old, so a daily task verifies
  weekly, or on demand with `--verify-all`. A set becomes visible through a same-directory
  rename only after all destination objects verify. The destination must support normal
  file locks and same-directory renames (ordinary SMB shares do). Concurrent runs refuse
  the store lock; it is automatically released when a process exits.
- **Retention.** Keep the newest set from each of the last 7 represented UTC days and
  8 represented ISO weeks (their union). Settings can be zero individually, never both.
  Pruning runs only after a successful capture/publication. Only correctly named,
  checksum-valid sets with this store's marker and exactly its two metadata files are
  removed. No recursive deletion is used. Shared objects are deleted only when a
  pruned owned manifest named them and no retained set uses them. Foreign or corrupt
  set directories are preserved and disable object collection. A `sets/.pending-*`
  directory left by a crashed publish that holds only this script's own manifest and
  marker (checksums match) and is over 24 hours old is removed and is not foreign; a
  younger one, or one with any other content, counts as foreign. Object `.part` files
  over 7 days old and stale `.staging/leeroy-guild-capture-*` directories are removed.
  Unpublished complete orphan objects are retained. Never edit manifests or delete objects
  manually: multiple sets share them. Copy the whole store when relocating it.

`./ac-guild-backup.sh --restore-test` reconstructs the newest completed set into a
temporary directory, checks every file against its manifest, reruns both SQLite integrity
checks and removes only that temporary reconstruction. A corrupt newest set fails rather
than silently choosing an older one. `--restore-to F:/guild-restore` performs the same
checks but keeps the verified output in that **new, initially nonexistent** directory;
it refuses live data roots and existing output. A failure leaves the live data untouched.
The capture/restore requires temporary free space for the complete payload. Capture
stages in `staging_dir` from the config when set; otherwise in `.staging` inside the
store when the destination is on a local fixed disk, else the system temp directory
(normally C:). It must be outside the store and the source data directories.
`--config`, `--root`, `--mind-data`, `--voice-data` and `--db-backup-dir` allow explicit
isolated paths; normal source defaults follow this checkout and `LEEROY_MIND_DATA` /
`LEEROY_VOICE_DATA`. The Git Bash wrapper uses the mind venv (or an explicit
`AC_GUILD_BACKUP_PYTHON`); the engine needs Python 3.11+ and only its standard library.

`ac-guild-backup-schedule.ps1 -Show [-At '03:00'] [-WithDb]` previews the per-user
`leeroy-guild-backup` task. `-Install` creates/replaces it, `-Remove` removes it, and
`-Status` reports the last result (0 means verified). It runs daily at the selected
local time, only while the owner is logged on, with Limited privileges and no stored
password, so his share access is available. A missed run starts when available; concurrent
runs are ignored. `pythonw.exe` runs the same backup engine without opening a window.
It is **not installed by default**; configure the destination, run a manual backup and
restore test successfully, then have the operator install it. Check `-Status` regularly;
there is no automatic failure notification. An unavailable share fails without pruning.

**How to restore.** Run `--restore-test`, then `--restore-to` a new staging directory.
Stop the mind and all other writers (console, voice and selfie activity), and keep a
separate local copy of the current `mind-data`/voice configuration for rollback. Replace
the mind data with the verified staged `mind-data` as a directory; do not overlay it onto
old database WAL/SHM files or stale photos. Copy the four staged voice TOMLs that are
present into `voice-data`, preserving its refetchable engines/caches. Keep the owner's
existing console key outside these directories. If anything fails, leave services stopped
and restore the saved pre-restore directories/configs. With the installed mind's Python,
from `mind/`, run `db --data-dir F:/azerothcore/mind-data migrate`, then
`doctor --data-dir F:/azerothcore/mind-data`; only restart after checks pass. Importing the
optional game SQL dumps is a separate operator recovery step with MySQL stopped from
serving the game; this script never imports them or overwrites live data.

`bash scripts/tests/ac-guild-backup.sh` exercises synthetic data only, including a live
SQLite writer, verified restore, corrupt/interrupted copies, ownership-confined pruning,
foreign directories, stale pending/`.part` cleanup, a large database snapshot under
continuous writes, and junction/symlink refusal. The schedule self-test mocks task
cmdlets and never registers a task. Both run in CI; never use live data for these tests.

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

worldserver is a console app that ignores `WM_CLOSE` (verified 2026-10-06), so a
clean stop needs its remote console. Since 2026-11-10 the live `worldserver.conf`
has `SOAP.Enabled = 1` on `127.0.0.1:7890` (not the default 7878, which is the
mod-leeroy bridge) and `Ra.Enable = 1` on `127.0.0.1:3443` (its default `0.0.0.0`
changed to loopback). Both log in as the dedicated GM-3 account `LEEROYCONSOLE`,
whose credentials live only in `%LOCALAPPDATA%\leeroy\remote-console.cnf`
(`user=`, `password=`, `soap=127.0.0.1:7890`, `ra=127.0.0.1:3443`; readable by the
owner's user only; never in the repo or a log).

`scripts/ac-soap.py <command>` sends one console command over SOAP and prints the
answer (exit 0 answered, 1 refused/faulted/no answer, 2 not set up; loopback only;
`LEEROY_REMOTE_CONSOLE` names another credentials file). `ac-server.sh stop` uses
it: `server shutdown <delay>` (`AC_SHUTDOWN_DELAY`, default 30 s while a player is
connected, else 1 s), then waits up to `AC_SHUTDOWN_WAIT` (180 s) for it to save
every character and exit. Without SOAP, or if it does not exit in time, it is
terminated as before, keeping only what the server last saved; in that case type
`server shutdown 30` in its console window first if characters are online.
`bash scripts/tests/ac-soap.sh` tests the client against a fake loopback server.

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
| `mod-leeroy` | this repo, `modules/mod-leeroy` | The guild bridge. `azerothcore-wotlk/modules/mod-leeroy` is a **junction** to `F:\azerothcore\modules\mod-leeroy`: remove it with `rmdir`, never `rm -rf` (that deletes the source through the link). Pins the cast (guild members at 80+, minus `Leeroy.HumanGuid`) via the playerbots pin API, mutes them in mod-ollama-chat, and serves NDJSON on `Leeroy.Bridge.Host`:`Leeroy.Bridge.Port` (`127.0.0.1:7878`, one mind at a time). Keys: `Leeroy.Enable`, `GuildId`, `HumanGuid`, `KeepCast`, `BringCastOnline`, `ExcludeFromOllamaChat`, `Bridge.*`, `CastGuard` (the kick guard), `Realm.*`/`World.Enable` (see the Realm; `get_world_roster` and `world_say`) and `Telemetry.Enable`/`Training.Enable` (see Training ground). Start line in `Server.log`: `mod-leeroy: cast N members, pinned N ...`. Smoke test: `python modules/mod-leeroy/tools/bridge_smoke.py --expect-cast N` (0 pass, 1 fail, 2 no server or another mind connected); `--listen S` prints events, `--send` sends raw commands. `owner_say` (the console's replies, separate from `human_say`) queues his chat line on his session: guild, officer, party, raid or a whisper to an online cast member; text is sanitized and then refused, not cut, when empty, over 255 UTF-8 bytes or starting with `.` or `!`; at most one line per 1500 ms and 8 per 30 s; he must be in game. Item links: hello lists `features: ["item_links"]`, and only then does the mind put `{{item:<id>}}` placeholders in a cast line (`say`, `realm_say`; `mind/leeroy_mind/talk/items.py`: bracketed or exact multi-word item names from library.db, never a place, NPC or test item unbracketed, kept as `[Name]` in every record, every line folded and defused after the wire's cleaning). Native sanitizes first and expands only a placeholder written whole in the raw text into the client's link from the item template, at most two a line within 255 bytes (`src/Lib/LeeroyItemLinks.*`), so no other `|` survives; a Realm channel hook sees the `[Name]` form, and its rewrite goes out without links. For the owner it answers `.playerbots bot add5/10/25/40` from his guild (see that patch below). |

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
bot jobs). Within a role the pick weighs each member's spec (talent tree with
most points; `GuildAddSpecFacts` holds the WotLK raid buffs and debuffs it
brings, counting a debuff only where playerbots' default strategies for that
tree cast it, melee or ranged, healer kind) and average item level: tanks
prefer a tank class not yet tanking within 10 item levels of the best-geared
tank left, then gear; healers fill a size template (5: Holy paladin or
Discipline; 10: Holy paladin, Discipline, a raid healer; 25: two, one and
three raid healers mixing shaman, druid and Holy priest; 40: three, two and
five), a kind nobody free has going to the best gear left; damage goes
greedily to the most weight of buffs his group and the earlier picks lack
(Bloodlust and Replenishment 100, a second Replenishment in a 25 or 40, the %
buffs and debuffs 10, the rest 1), then gear, the melee/ranged split nearest
even, the class his group has fewest of and the lowest guid. When someone
joined, a last line names what his group still lacks: Bloodlust and
Replenishment for add5, those and the % buffs and debuffs for a raid. They
join directly (`AddMember`; his group is made only once someone can join, and
becomes a raid only when a party is full), take the
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

Optional persona play styles require both `[playstyle] enabled = true` in the
mind's `mind.toml` (restart to attach) and native `Leeroy.PlayStyle.Enable = 1`
(`.reload config`). Both default off. This first map adds only ordinary potion use
and a 2–6 s DPS wait-for-attack hold outdoors; tanks/healers get no added delay.
It never shortens an existing active hold and leaves one above 6 s untouched.
All dungeon/raid instances, raid groups, combat, PvP and conflicting controllers
refuse additions; RaidHabits remain authoritative. Native checks actual bodies,
roles and ownership after the ack. The independent style journal is restored by
`bot_do restore` (`param="playstyle"` or `"*"`), native off/raid/PvP cleanup,
and the mind's bounded shutdown drain. Unknown receipts remain in
`playstyle:owned` for reconciliation; foreign ownership can leave cleanup pending.
With an already attached mind, hello/login rereads the switch and queues undo when
off. No model, prompt, gear, loot or raid strategy change is involved. See
`mind/leeroy_mind/playstyle/README.md` for the deterministic map and limits.
Each hello restores, then reapplies, every owned member (reconciliation, by design).
`playstyle_result` logs `reason=` (`ok`, `no_job` with no terminal in 5 s, native's code,
`skipped:<code>`, `guids` or `kind`); an ack refusal logs `playstyle_refused` with its
`error=` code and member count.

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
himself are untouched. It does not remove a pin from a team it joined before, and a
captain's gather (`BGJoinAction::gatherArenaTeam`) takes any member out of its group
and teleports it unless its master is a game client (a self-mastered bot counts) or
it is in combat, a battleground or a queue; on 2026-11-07 it pulled masterless cast
member 251 out of an offline raid's group. So mod-leeroy (`Leeroy.CastArenaTeams.Leave`,
default 1, `.reload config`) takes a pinned cast member out of every rated team the owner
does not captain, on the queue fence's world tick, through the core's `ArenaTeam::DelMember`
under the leave handler's own conditions (not while the team fights or the member or a
group-mate is queued, nor while a Realm lease holds the body), once per member and team
per process: `cast_arena_team_left guid= team= type=`. A cast member who captains a bot
team is kept and logged once (`cast_arena_team_kept ... reason=captain`): it cannot leave
without disbanding or handing on the team, which is left to a playerbots patch.

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

- Talk awareness and guards are separately opt-in: `[talk] aware = true` adds a
  scoped observation card inside the live prompt budget; `[guards] enabled = true`
  reviews model drafts, allows one screened retry, and records delivery rations.
  Deterministic announcements bypass model guards. Answers owed to the owner use
  advisory verdicts; hard content/name/slash screens still apply. Discord cards
  and guards omit game-state evidence. `[claims] enabled = true` supplies claim
  evidence and enables its night stage in both runners.
  See `mind/leeroy_mind/guards/README.md` for bounds and fake rehearsal results.

`mind/` is the Python service that speaks for the guild through mod-leeroy's
bridge, on the local Ollama. It has its own venv in `mind\.venv`; run it from
`F:\azerothcore\mind` in Git Bash inside Windows Terminal (in a standalone
mintty window Ctrl+C may not reach python):

```bash
D=F:/azerothcore/mind-data                                  # pass --data-dir "$D" to every command
.venv/Scripts/python -m leeroy_mind run --data-dir "$D"     # the service; Ctrl+C once stops it cleanly
.venv/Scripts/python -m leeroy_mind doctor --data-dir "$D"  # one ok/FAIL/note line per check
.venv/Scripts/python -m leeroy_mind db --data-dir "$D" migrate   # snapshot, then the code's schema
.venv/Scripts/python -m leeroy_mind db repair-provenance --data-dir "$D"  # counts only; --go snapshots and repairs with the mind stopped
.venv/Scripts/python -m leeroy_mind personas --data-dir "$D" card <guid>   # also export, import, mix
.venv/Scripts/python -m leeroy_mind.library --data-dir "$D" stats   # the game library: build, search
.venv/Scripts/python -m leeroy_mind nightly --data-dir "$D" status  # also run, ledger, undo, enrich, veto
.venv/Scripts/python -m leeroy_mind eval --data-dir "$D"    # weekly evaluation, on a copied database by default
.venv/Scripts/python -m leeroy_mind eval talk --data-dir "$D" --since 14 --fake --json talk.json # stored talk candidates, scratch copy only
.venv/Scripts/python -m leeroy_mind selfies --data-dir "$D" status  # profile photos; also try (mind stopped)
.venv/Scripts/python -m leeroy_mind console --data-dir "$D" # the guild console; open the URL it prints
.venv/Scripts/python -m pytest -q                           # the unit tests
.venv/Scripts/python -m pytest -q -n auto tests             # fast run; up to 8 isolated workers
.venv/Scripts/python -m pip install -r tests/console/e2e/requirements.txt; .venv/Scripts/python -m playwright install chromium; npm ci --prefix tests/console/e2e; .venv/Scripts/python -m pytest tests/console/e2e -q  # isolated browser and axe rehearsal
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
- The weekly evaluation's secret probe asks a blank answer again (3 tries in all); a sample
  still blank counts as a leak (`empty_samples` in the report), so it can only close the
  secret gate, never open it, and no longer fails the whole week. Its live-prompt copy
  (`scratch\mind-eval-<stamp>.db`) is deleted when the evaluation closes it; one a crash leaves
  is pruned to the newest 10. Older `mind-eval-<night>-*` copies are the operator's to delete.
- Explicit prompt wording lives only in `mind-data\prompts\` overlays; the
  built-in prompts in the repo stay non-explicit.
- Optional `[callbacks] enabled = true` enables the memory callback selector API
  (default/invalid means off); it is not wired to talk yet. It checks complete
  supported provenance, pair/officer scopes, a 14-day reuse window and owner-play
  quotas. `python -m leeroy_mind callbacks eval --data-dir <data>` replays only on
  an in-memory backup, with counts on stdout; `--show-text` writes private seeds
  only to that data directory's scratch folder. Older schemas are not migrated.
  Caller transaction requirements and limitations: `mind/leeroy_mind/memory/CALLBACKS.md`.
- `pytest -m ollama|live|mysql` runs the optional checks against Ollama, the
  running bridge and MySQL; the default run deselects them. `-m live` takes the
  events the bridge queued for the mind, so never run it while they matter.
- Do not run `personas generate`/`seed` or a library build while the mind is
  live: they load another large model next to the live one and evict it.
- Optional `[talk] realism = true` adds bounded typing cadence, rare corrections and
  small reply bursts; default off, read at mind start and each login. Mind-only:
  no migration or native rebuild. It preserves authored/altered-state typing,
  excludes raid-event turns, and drops stale follow-ups after a newer line from
  that member. See `mind/leeroy_mind/talk/README.md` for budgets and busy evidence.
- Optional `[fingerprint] enabled = true` attaches a model-free typing-profile stage
  on restart (also in `nightly run`). Missing/invalid settings mean off. It measures
  the last 300 mind-origin lines, or seeds a documented persona prior below 50.
  Private/officer lines contribute numbers only; lexical phrases come from public
  guild/say/yell lines. Logs contain ids/counts and hints use fixed safe wording.
  No prompt or guard changes yet. `python -m leeroy_mind fingerprint show <guid>`
  reads without migration, including older databases. Enabled weekly evaluations
  add a current profile-distance snapshot, not a held-out-week score. See
  `mind/leeroy_mind/fingerprint/README.md` for the seed mapping and metric limits.
- Only the night's own writers write personas while the mind runs. Optional
  `[personas] auto_newcomers = true` (read at startup; false or invalid means off)
  gives members who join the cast later (invited, or levelled to 80) a persona at
  night. The `newcomers` reflection stage runs just after the snapshot, before
  threads and other persona readers. With no newcomer work it does not swap models.
  It reuses the manual generator, slot balance, age floor of 25, name/content screens,
  bounded overlay, birthdays and relationship seeds, with source `generated` (the
  existing source allowed by `persona_core`). `newcomers_per_night` defaults to 6 in
  NightlySettings; the budget may defer them. A saved nightly selection and unit
  receipts keep that cap across resumes. Generation refusals and recoverable save
  failures retry on at most three distinct nights, then log `newcomer_gave_up` with
  the guid. A save failure rolls back the persona/relationships before recording
  the failed night when the database remains writable; manual generation remains
  available. Login cancellation consumes no failed night. The owner and non-cast
  members are excluded. Later night stages see new personas immediately, and the
  existing login reload makes them available to live text talk without a restart.
  Existing enrichment, photo-kind and first-selfie stages pick them up as their
  eligibility and nightly budgets allow; their scheduling is unchanged. Without a
  separate voice spec a newcomer stays off voice comms (no fallback voice).
  Until generated, a newcomer stays silent: the mind voices only stored personas
  and native mutes every cast member in mod-ollama-chat.
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
- Situational photos (`[selfies] snaps`, the `selfies` world dial) are made and stocked at night
  from members' days and the raid nights (see the selfie worker); sending them waits for the
  guild Discord's photo phase, so the talk is unchanged.
- Guild life (raid nights, DKP and loot, offline raids, rank, spec and gear
  upkeep) runs with built-in defaults; `[guild]` and `[dkp]` tables in
  `mind.toml` override them.
- An offline raid start refused for one named roster member (`in_instance:<guid>`,
  `busy:realm_owner:<guid>`, `busy:realm_group:<guid>`, `busy:<guid>`, `not_cast:<guid>`,
  `leader_unavailable:*` for the leader, also inside a `setup:<stage>:` failure) leaves that
  member out of every plan for the rest of the day (day meta `excluded`): the bench fills the
  seat and the leader rules pick another tank. The retry keeps its delay (60 s for `not_cast`),
  `MAX_ATTEMPTS` and the GPU lease. Planning also leaves out members the last cast snapshot put
  in a raid or 5-man map who have not changed zone since. A roster that is then too small or has
  no tank waits 15 minutes (`offline_raid_waiting`) instead of sending or ending the day.
- Offline raids publish a running `job` heartbeat every 60 s with kills, wipes,
  `members_in_instance` (the exact map and instance), `last_progress_ts` (Unix ms;
  start, boss engage, kill or wipe), and `boss_engaged` when native can read the
  encounter state. The mind requests `offline_raid_stop {id, raid_id}` after
  `[guild] offline_stall_minutes` (30; 10-360) without progress. A heartbeat
  over 120 s old, an unknown encounter state or an engaged boss prevents a stop;
  boss evidence between heartbeats (also after a mind restart) defers it, and
  native rechecks engagement at dispatch. Members leaving the instance is native's
  own too_few stop; the mind has no scatter rule.
  Stops target the original start command's id (`raid_id`), use normal teardown,
  and finish only on its terminal `job` event (`stopped`, recorded by the mind as
  `stalled`). Cleanup failures retain their native reason. A stop whose send fails
  (bridge down, transport off) is uncertain and retried on a later tick with a fresh
  command id; stops register no job mailbox entry. An accepted stop releases the GPU
  after the cancel grace but never blocks the day, and a lost terminal is declared
  15 minutes after the accepted stop. Retries
  use fresh command ids for the same raid; stopping an already stopping run is a
  no-op, while unknown/finished runs are refused. Old native without heartbeats
  or the command keeps the existing timeout behaviour and logs unsupported once
  per run. A mind restart requires fresh progress evidence; lost terminals still
  use the deadline (or an accepted stop) plus 15-minute `terminal_lost` fallback. Native rebuild needed.
- A running offline raid can recall an absent follower after 30 s, at most three
  attempts per member per run and 60 s between attempts. The original body, group,
  controller and destination instance must still be proven; combat, PvP, another
  dungeon, dead bodies and pending transfers prevent it (pure policy
  `RecallQuietOk`/`RecallBodySafe`/`RecallPvpBusy`; the battlefield and arena lookups
  run last). Existing leader/minimum roster stops win. Arrival updates the heartbeat
  without resetting the boss progress clock. Benchmarks do not use this recall path.
- The job's driver is a self-mastered bot outside the raid group, and stock
  `ResetAiAction` (the empty group list Grouping's `RemoveFromGroup` sends, or a leader
  change) clears a master outside the bot's group; `FindNewMaster` finds none in an
  all-bot raid. On 2026-11-07 that left 3112 (the leader) and 251 masterless, so the
  teardown that began at 17:33:26 retained `restore_ownership` for both and never ended.
  Every tick, in every stage including teardown, native now puts the driver back for a
  member it still holds (same body, the job's group, no Realm or dummy lease) whose
  master is empty, re-strips follow, restores `wait for attack` and `avoid aoe` and
  reconciles DC's strategies (`master_reasserted guid= stage= repairs=`, first and every
  50th). A non-empty foreign master is still an ownership change.
- A restart no longer needs every member: after 30 s an absent member is dropped from
  that restart's gate (`restart_dropped guid=`) while the members present meet the
  minimum; fewer stop the run `too_few`. The leader is never dropped. A dropped member
  stays in the roster for cleanup and later recall.
- Native logs the stop reason immediately and includes it in teardown progress.
  Cleanup still incomplete after 60 s reports `cleanup_failed:<reason>` once,
  for ordinary offline raids as well as benchmarks; this ends mind accounting. From
  then on a member the job no longer holds (other group, master or body) that carries
  no training journal is abandoned (`cleanup_abandoned guid=`, never touched again) so
  the members it does hold finish their cleanup. Ten minutes after `cleanup_failed`, an
  ordinary offline raid releases its native slot (`cleanup_released after_ms=
  unfinished=`; then `training_run_cleared=`) unless a member is still provably held
  (connected, same body, the job's group, the driver as master), which logs
  `cleanup_held` once and keeps the slot as before. Release restores the WaitAtBoss
  override and the leader's DC training-run receipt where it can, and keeps training
  journals (their `training_journal_pending` fence stays) and the cleanup receipt
  (`get_training_cleanup` shows it unrestored). Benchmarks never release. A leftover
  group is disbanded by the 60 s leftover sweep. A native rebuild is required.
- Members start conversations with him on two clocks (2026-11-07), per hour of
  his play at chattiness 4 and scaled with it (0 stops every unprompted whisper):
  `[talk] whispers_per_hour` (4, at most 8 an hour and 2 minutes apart;
  `whisper:start`, romantic from drawn and lust women, casual from everyone else,
  a fond woman at a quarter of a lust one, lifted by the romance dial) and
  `openers_per_hour` (6, at most 12; `burst:to_him`, a casual line naming him,
  from a group-mate in party when his group is five or fewer, else guild), the
  talkative and forward dials moving the rate. Neither runs in his raid's fight,
  wipe silence or debrief, a 5-man boss fight, while he is AFK, in his login's
  quiet or within a minute of his own line, of his emote at a member or social
  emote (and theirs back) or of an answer to him (an emote's line too); a member
  in a whisper with him (10 minutes) starts none, openers never take the guard
  slot of an answer (an emote's line included), and a first `(nothing)` is
  asked again once.
- His text emotes (`talk/emotes.py`): every one at an online cast member but the utility and
  combat ones (`talk/emote_names.py`) gets her emote back after 2-6 s, by his emote and her
  stance (men, stance none and a forward dial at 0 never romantic), and sometimes a line (a
  romantic emote by whisper, half the time; others by /say, a quarter; none while she is
  answering him); a social one at nobody brings 0-2 of his group, or else his zone, joining
  in. 45 s per member, 6 a minute, never in a fight or the login quiet. The night counts the
  ones she answered (meta `emote_answered:<event id>`): romantic ones at a woman with an
  attitude as a flirt, two a night at most, rude ones as a hurt. Overlays
  `emote_at_you`/`emote_joined` in `mind-data/prompts` reword the line.
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
  HTTP accepts one request per connection, at most 64 connections and 8 event streams,
  with a 16 KiB head/body cap and 10 s request/drain deadlines. Malformed framing is refused;
  console and control JSON reject duplicate fields, nonfinite numbers and nesting beyond
  32 levels. Static package links are not served, and photo reads reject symlinks or junctions
  anywhere from the selfies root through the file, check the opened file's identity, and
  accept bounded JPEG bytes only (legacy thumbnails use the same checks).
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
  the chronicle, and the Guild tab's DKP stats from `raidview.dkp_stats`: each
  member's attendance over the finished raids since their first, who is due an
  item (4 such raids since their last, DKP above 0, at most 10) and what each of
  the last 20 raids awarded and spent); the Raid tab's live state and buttons come
  from the mind's control endpoint. The Personas tab is read-only too (`console/personaview.py`;
  it opens on a wall of the cast's faces below "Choose selfies", with a Wall / Table
  switch remembered in `guild-console-personas-view`):
  each member's night mood, stance, row toward the owner, strongest ties, memory
  counts, persona version and who last wrote it (seed, owner, generator, the
  night's looks pass or photo-kind pass, a stance move or an undo), open drift
  flags and the night's trait sample, and per member the persona card summary
  (its looks with the photo kind), ties, memories by kind and ledger; a table
  an older mind.db lacks shows as unavailable. Persona Insight (`console/insightview.py`,
  `insightreports.py`, `static/js/insight.mjs`; [guide](mind/leeroy_mind/console/INSIGHT.md))
  adds bounded read-only claims, guild-context awareness and private audit-rate projections.
  Its `claim_check` read op works with `--no-control` under key/Host/Origin gates, uses
  only an already-resident embedding model, and never logs or stores typed text.
  Guard verdict history is not persisted, so the panel explicitly says unavailable.
  Its writes are the profile
  photos (see the selfie worker), his Board replies (below) and the tuning (`leeroy_mind/tuning/`,
  console `tuneview.py`): the
  owner's dials, stored in mind.db's `tuning_*` tables as an overlay that never
  rewrites a persona. Member dials: talkative, argumentative, forward/flirty
  (women only), humour amount, theorycrafter, lore nerd, drama, jealousy, party
  (drinking and smoking habits), opening up, reliability; world dials
  chattiness, romance, jealousy and selfies override mind.toml without writing it. A
  member's own value wins over group offsets, which add up. With nothing set,
  every prompt and weight is what it was (`tests/tuning/golden_prompts.json`).
  Writes and previews go through the mind's `tune` op; a preview runs only
  while the live model is already loaded, never loads one, and its text is never
  logged or stored. `python -m leeroy_mind tuning --data-dir "$D" status|history`
  reads it; `undo N|revert N` work with the mind stopped.
  Temporary nudges share that history and its undo/revert. They are private
  owner overlays, never persona edits or log text; 1–200 cleaned characters,
  1 hour to 14 days, with channel/recipient scope and a 200-token prompt budget.
  The `tune` op accepts `nudge` set/retire/list/preview; preview keeps the loaded
  model gate. `tuning nudges` reads active entries and history. Optional
  `[tuning] nudge_memories` (default true) offers ended everywhere intentions to
  reflection as past moods, never factual evidence; scoped ones are excluded.
  Details and storage keys: `mind/leeroy_mind/tuning/README.md`. No migration.
  Optional `[signals] enabled=true` records numeric owner-response observations;
  default/invalid is off. `signals status|member <guid>` is read-only; keyed
  `feedback` replaces a line's vote. Values are normalized (`reply_latency_s`
  is seconds/120, `reply_len` is codepoints/500 capped at 1). `judge_guard_*`
  means historical screening, not a live action. General combat/rest telemetry
  is absent, so ignored capture stays conservative. See
  `mind/leeroy_mind/signals/README.md` for coverage, privacy and retention.
  The page is split into `console/static/` (CSS, a classic `theme.js`, ES
  modules, self-hosted OFL fonts with their licence texts) and is served at
  `/static/` without the key, like the page, since a browser cannot send a
  header for `<link>`, `<script src>` or `@font-face`; the CSP is `'self'`
  with no inline style. Dark is the default whatever the system setting; the
  light/dark switch is remembered in `localStorage` (`guild-console-theme`,
  with the folds and read marks, `guild-console-collapsed` and
  `guild-console-read`). Beside it, Hide photos (`guild-console-photos`, applied before the first
  paint) draws every member's face as initials without asking for the photo, and blanks any
  other photo (the gallery, the selfie picker) until it is switched back. It has 14 tabs (Comms, Raid, Guild, Personas, Social,
  Board, Calendar, Bank, Realm, Training, Dojo, Library, Ops, Mind); each polls only
  while it is open and the page is visible, one request at a time. Ordinary reads have a
  30 s browser deadline, actions 120 s (long enough for a clean stop); an unanswered action
  says it may have completed and is never retried automatically. Failed or malformed reads
  keep the last good view with a stale age on interval polls. One-shot reads report their
  own errors without promising retries. Hidden pages close the event stream: they receive
  no background events or toasts, and resync from a fresh snapshot when shown. The event
  stream and voice socket reconnect with capped exponential backoff and jitter; their
  heartbeat deadline is 45 s. Voice retries cap at five seconds, and a console voice
  start/restart wakes the relay immediately. The face
  cache keeps at most 256 entries and four active loads. The Personas roster is paged in
  groups of 50; its member details stay available. Arrow keys, Home and End move through
  the tablist; Tab reaches the main scroll region and controls; dialogs trap focus and
  Escape returns it. Both themes and reduced motion are covered by the browser rehearsal.
  WoW feed history retains 64 active/recent views, at most 200 lines each, 64 explicitly
  opened whisper threads and 128 read marks. Inactive WoW reply/refusal and nudge drafts
  keep their most recent 64 entries; these conveniences are not durable message storage.
  Discord's read-only message/quote/pin/thread window fits a 180 KB ASCII-JSON budget:
  whole records are kept, with visible words when older items are omitted. Stored history
  is untouched. Its existing watched fingerprint suppresses unchanged SSE updates; the
  server now retains fixed-size digests instead of full copies of those windows.
  A source that is not there yet answers 200 and reads "Not set up yet: <why>"; only a file that
  exists but cannot be read is a 503. Ops (`GET /api/ops`, `console/opsview.py`) keeps
  those states per panel in one 200 response: health, night, Realm holds, raids,
  selfies/snaps, backups, cached Git release drift and scheduled-task status. Each
  source has its own deadline (health 6 s, Git/tasks 3 s, others 1 s) and one worker;
  a slow source is explicitly skipped while the others answer. Health reuses the
  installed checks at most once a minute; the tab polls every 15 s. It shows only
  counts, codes, identifiers and times; private health diagnostics stay in the CLI.
  Saved running nights may be paused, unretained progress stays unknown, desk dates
  mean recorded operations, and backup metadata is not a new restore test. Release
  commands remain selectable text. Ops actions additionally require `[control] ops_actions = true`
  (default false; invalid control settings keep the actions off) and restarting both mind and
  console. `--no-control`, key/Host/Origin checks and a verified running mind still apply.
  Every action uses a fresh preview and a one-use confirmation; the owner must be known offline.
  A refused confirmation restores the review step and keeps the entered draft fields.
  `POST /api/ops/action` starts one background operation, `GET /api/ops/action` reports its outcome;
  the reservation also blocks other console writes and lifecycle actions until it finishes.
  Realm release requires the exact root id typed back and a 1–200 character reason kept only
  in its private CLI receipt. It refuses open guild/offline raids and the CLI's nonreleasable
  roots, stops cleanly (never force), runs this checkout's `realm release --yes`, then restarts
  even after a CLI refusal. Release and restart outcomes are reported separately; an unproved
  clean stop changes no root. Offline stop targets the original owned command id, keeps fresh
  progress/boss fences, and finishes only after the terminal and cleanup; an ACK is pending.
  Render now keeps the booth's offline wait, request age, daily cap, cooldown and lease/night
  priority rules; completion comes from that requested task, never an older shared receipt.
  Snapshots use the same SQLite backup helper as `run`, in a fresh contained
  staging directory, and publish without overwriting. Labels are 1–40 ASCII letters/digits,
  underscore/hyphen; links/junctions are refused. Manual snapshots carry an operation suffix
  and are retained until the operator removes them. Their local volume must support hardlinks;
  unsupported publication fails rather than falling back to an overwrite. Console shutdown
  waits for a release's restart; other work remains mind-owned. Unknown outcomes require a
  status check before retrying. Logs contain action/id/outcome, never reasons or labels.
  `GET /api/face?guid=<int>&size=sm|md|lg` serves
  `mind-data/selfies/<guid>/chosen.jpg` (key and Host checked, `image/jpeg`,
  `private, max-age=60`, 404 when absent; the original for every size, the
  browser scales it) and never writes under `selfies/`; faces (initials when
  there is no photo) show in Comms, Raid, Guild, Social, Board, Calendar, Bank and
  Realm and on the Personas wall and roster. Comms is one
  feed of typed lines and the room lines he heard (from mind.db: a line
  appears once it has played to him, a dropped one does not), channels with
  unread counts (a first open or reload marks history read), the 32-room tree
  (empty rooms greyed, click to join), whispers and the member card. While a
  member's line is being composed or typed, "<Name> is typing" with three dots
  shows under the open view's feed (two names, then "Several people"; the dots hold
  still under reduced motion), a member writing him a whisper gets the dots beside their
  name in the whisper list, and the dots go when the line lands or after 30 s
  (`comms/typing.mjs`, `typingview.mjs`; voice-room turns show none). A voiced
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
  The other tabs read, each through one `GET /api/<tab>` and one view module
  (`console/<tab>view.py`; a table an older mind.db lacks is listed in `missing`
  and reads as empty). Social: the guild's drama as the night wrote it (feuds,
  jealousy, romances, how members feel about him, live rumours, the guild mood).
  Calendar: what the Realm's calendar desk posted (raid nights, Thursday's officer
  meeting, birthdays) with who signed up, and the raid nights he missed, by the
  desk's own rule (`realm.calendar.night_text`). Realm: the rival guilds with their
  people and progress, the race for first kills, the week's poaching offer, the
  rivals' Trade lines and the canon grants. Bank: the guild bank's gold, six tabs
  as grids and money log from the mind's `bank` op (the mind runs the SELECTs, so
  the database password never reaches the console; item names from library.db),
  with the officers' stock and withdrawal ledger (`realm_bank`) from mind.db, and in
  words why there is no snapshot while the mind is down. Training: what `training
  status`, `gate`, `trials` and `cards` print (`training/inspect.py`, shared with
  the CLI): fights, the 28-day count, the gate, trials and cards. Dojo: only the
  dojo's own files, through `--dojo-layout PATH` (default
  `<root>/dojo-runtime/instances/dojo-01/layout.json`; from the layout only
  `format`, `marker`, `instance`, `roots.data`, `roots.runtime`, `provenance` and
  `ports.world`, never its database, secrets, configs or logs; files capped in
  size and only under those roots; a read-only `git` for whether the checkouts
  moved): whether its worldserver runs (pid file and the TCP table), the newest
  build, baselines, G0-G5 with their evidence, runs, episodes and replays, the
  scenario and its hash, "not yet" for later work, and "No dojo set up" without
  a layout. Library: a BM25 search of library.db, opened read-only per search
  (`?q=&kind=`, no Ollama, so it works with the mind down); when it finds nothing,
  or on "Search deeper", it asks the mind's `library` op (`deeper=1`), which works
  with `--no-control`. Mind adds the game server (`/api/health`'s `server`):
  worldserver, authserver and the bridge are up when the console's TCP table shows
  a listener on 8085, 3724 and 7878 (it never reads worldserver.conf), and
  `status`'s `world` says whether he is in game and how many of the cast are
  online, unknown while the mind or its bridge is down. Board: the Realm's forum
  threads, the letters delivered to his mailbox (`sent` or `taken`; one not yet
  delivered stays hidden) and the event write-ups, with his replies under the post
  or letter each answers. His one write there is `POST /api/board`
  {`action`: `reply`, `kind`: `post`|`letter`, `to`, `text`} or {`action`:
  `withdraw`, `id`}, checked and cleaned as the mind checks it
  (`endpoint.board_args`, the rule of `board.py` and `boardmodel.mjs`: `|` and
  control characters dropped, whitespace collapsed, then an empty text or one over
  200 characters for a forum post or 400 for a letter refused, never cut; 400 with
  its words), 403 with `--no-control` and 409 while the mind is not running or
  there is no link. The mind's `board` op then stores it in mind.db's
  `board_reply` (migration 0020) `waiting`, only while the Realm is on, one
  waiting reply per post or letter (a second is refused in words) and only to a
  post still on the forum or a letter in his mailbox from a member with a persona;
  a waiting reply can be withdrawn. The page says "waiting for tonight" until the
  Realm night takes it (see the Realm's Night). Logs carry the kind and outcome,
  never the text.
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
  outcome, never the text), `board` {action: reply {kind, to, text} | withdraw
  {id}} (his Board replies, `board.py`, one at a time), `discord` (local channel/DM
  posts, edits, deletion, reactions, pins and threads; `discord/desk.py`), and two reads: `bank`
  (`guild/bankread.py`: money, the six tabs' slots and the money log by four
  read-only SELECTs through guild life's armory, one read at a time within 15 s,
  an answer younger than 30 s given again; errors are words, never the database's
  text) and `library` {query[, k]} (the mind's semantic search over library.db:
  one embedding call on the embedding model Ollama already holds, never a load and
  never a chat model, refused in words otherwise; up to 12 hits; the query is never
  logged). `status` also carries `world`, the last hello's server and version with
  the cast count, how many are online and whether he is, or null before a hello and
  while the bridge is down. It also carries `typing` (`Pipeline.typing_now`: guid, name,
  channel, to, since, age of each model turn being composed or typed where Comms shows
  the line, never text), which the console asks for on its 2 s strip pass only while a
  page listens and he is in game, or a page has asked for `/api/discord` within 60 seconds.
  `stop`/`start` of a raid switch dungeon-clear off and on again mid-raid. The
  console sends the key only to the listener the TCP table shows is the running
  mind's own pid; its Stop and Restart use the clean stop (asked up to five
  times while the endpoint is there but busy), wait up to 60 s for the process
  to end, and otherwise (or with no endpoint: a mind from before it, or one
  with it off) stop it by force and say why.

## The guild's Discord (`leeroy_mind.discord`)

- Comms includes a local guild server above the separate **From WoW** and voice
  sections. It uses no external Discord API. `GET /api/discord` returns its channel
  layout, member roles/presence, DMs and an optional message window with quotes,
  reactions, threads and pins. Browser read marks request actual unread row counts;
  global message-id gaps are never counts. The page polls every 30 seconds while
  Comms is visible and refreshes on the `discord` SSE event. Loaded channel/thread
  windows are compared again to catch removals, including unreact and unpin.
- `POST /api/discord` forwards validated actions to the control endpoint's `discord`
  op: `post`, `edit`, `delete`, `react`, `unreact`, `pin`, `unpin` and `thread`.
  The mind alone writes; the console reads the database read-only. The normal
  key, Host, Origin, fetch-site and `--no-control` gates apply. Posts hold at most
  2000 Unicode code points; Unicode emoji are kept. Only the owner edits/deletes
  their own messages. Deletion leaves a tombstone so quotes do not expose old text.
- Migration **0027_discord.sql** adds `discord_message`, `discord_thread` and
  `discord_reaction`; pins live on message rows. `layout.py` owns the fixed categories,
  class channels and officer rooms. Store ids are channel names or `dm:<guid>`;
  talk ids are `discord:<channel>` or `dm:<guid>`. Scheduled presence uses regional
  time zones, including the weeks when UK and US daylight-saving changes differ.
  The console cannot see the owner's current group, so its presence display lacks
  the mind's in-group override.
- `DiscordTalk` answers a DM with its member, a mention or reply with the addressed
  members, otherwise a presence/affinity/role-weighted choice. It uses the existing
  talk pipeline with its own transport and 120-second deadline. Game AFK/offline
  flags do not block a Discord answer; an away record still does. A direct answer's
  first silence is retried once. `DiscordTarget` keeps its reply and thread ids.
  Nothing reaches WoW, and these messages do not advance the game's talk clocks.
- Before the first bridge cast, Discord uses the saved guild roster for channel
  audiences, addressed members, presence and name protection. This never marks a
  game body online. The saved roster is read once and cached; failed reads remain
  retryable. The first bridge cast drops this cache and takes over, even when empty;
  removed members cannot return from the saved roster. A roster read failure leaves
  no fallback members and logs only the exception class.
- DMs have whisper privacy. The desk also stores each owner post as his chat line
  (origin human, no session, the talk's channel id, a DM's member as `to_guid`), threaded
  with the members' answers, so a DM where he asks for secrecy is secret exactly as a
  whisper; it moves no game clock and never reaches WoW. His edit edits that line, but
  never loosens secrecy (an edit dropping his ask leaves it as read); his delete keeps it,
  since a member may already have read it. Officer-only and unknown channels are restricted;
  restricted evidence stays restricted through derived memories, notes and recall.
  Unknown audiences fail closed, and the storage transport checks its speaker's
  audience again. Logs contain action/channel/message ids, counts and outcome codes,
  never message text or emoji.
- An owner post holds the arbiter's `discord` live lease; subsequent activity keeps
  it for ten minutes. This uses the existing night hand-over and keeps the owner's
  game-login lease independent. Shutdown drains the final release. The default
  rehearsal (`tests/discord/test_discord_rehearsal.py`) runs a real mind, control
  endpoint and console on ephemeral ports with the invented cast and fake bridge,
  LLM and model admin: channel answer, named and officer-only replies, reaction and
  DM, both after logout and before the first bridge cast; no game `say`, clean shutdown.
- D1 is owner-triggered conversation, with optional pre-send guard review and
  scoped awareness cards. Autonomous chatter, broader member reactions, topics,
  photos, decisions and culture remain later phases.

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
- **World personas (optional).** `[world] enabled = true` admits native-verified
  random-bot guilds outside ours. A native rebuild is required for `get_world_roster`
  and `world_say`; both require `Leeroy.Realm.Enable = 1` and `Leeroy.World.Enable = 1`
  (World defaults to 0; `.reload config` applies it). The mind's `realm` and `selfies`
  opt-ins default false; world selfies also require `[selfies] enabled = true`.
  Restart the mind after enabling. Caps are `world_per_night = 12` and
  `world_selfies_per_night = 10`, with cast images first. Complete acknowledged
  rosters expire after an hour. Refused roster ACKs log one code per process.
  Existing Realm actors retain their moves, whispers and night beats; extra world
  speakers use Trade/General only. A world bot joining the cast seeds a screened
  newcomer persona at night only with `[personas] auto_newcomers = true`; otherwise
  use `personas generate` with the mind stopped (doctor lists these missing cast
  personas). Both paths retire old world selfie choices, retaining their files.
  Retired outsiders retain their stored personas.
  `personas world status|plan --data-dir PATH` reads counts without models or writes;
  the console's World cards are read-only. See `leeroy_mind/world/README.md`.
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
  fence or a `busy:` reason such as `busy:combat` (`STEP_REFUSALS`), or a rearm for
  `weapon_record_unavailable` (`KIND_STEP_REFUSALS`): native refused that body
  before changing it and released them all, so it settles `step_refused` and Recall goes on to
  the defaults. A rearm no longer fails whole for one member (2026-11-10/11: all 15 overnight
  rearms of the 143 refused for one member in combat or dead, others at the ack for his group's
  members): native skips a member its step refuses before any swap, or Tick's fence before its
  step, and leaves out at the ack a member admission fences (refusing whole only when every member
  is, or for a job-wide fence such as `busy:training_cleanup`: member reasons are an allow-list,
  `MemberSkipReason`), so the done `realm_job` lists them in `skipped`; the mind settles the rearm done with no
  backoff (the next pass rearms them) and the defaults go to everyone. A defaults batch native ran
  with some members skipped (dead, busy) settles `defaults_failed` and no longer stops the chain's
  later batches; an aborted one still does. Native keeps the weapon records in memory, so a worldserver start empties them
  until the 60 s poll finds each body online and free; a rearm now records an armed body that
  has none, as that poll would. Any other failed job (`operation_transition_unknown`, a walk,
  leave, duel or grow) stays unknown unless its event carries `changed: false` (below; any ordinary
  kind can carry it, and a grow refused that way goes on to its next attempt): an ordinary leave's
  `wg_private_state_unavailable` (a member in Wintergrasp), `cleanup_member_not_admitted`
  (a member's cleanup roster reaches outside the manifest), or a group departure reason
  (`shared_group_leader`, `shared_group_cleanup`, `shared_lfg_queue`, `shared_lfg_cleanup`: a
  member who leads its battleground or battlefield group, or whose leaving would disband it), is
  refused whole at its ack, before any body is reserved or moved. Wintergrasp takes precedence, then
  cleanup, then the group reasons, one reason per error. `LeavePvpAckError` names as many
  manifest members as fit in 256 characters, at least one, as `<reason>:<guid,...>`; cleanup names
  the members whose rosters reach outside, not the outsiders, using the step's roster readers.
  The mind accepts any of them only for an ordinary leave when the names are distinct manifest members,
  settles it refused and holds nobody. Recall backs off those members for 600 s and recalls the rest
  as a new command; every such retry strictly shrinks its manifest. A member its own step cannot
  take out for a reason of its own (a group departure, its original raid group, its entry point, a
  changed battleground group, an LFG proposal or closure, Wintergrasp, or a lease Tick's fence finds
  broken before its step, as when its battleground ends mid-job), while nothing in the job has
  touched its body, is skipped instead (2026-11-10: one battleground raid leader made a 143-member
  recall fail after others had moved, and native kept all 143): native ends that member's lease
  alone, goes on with the others (whose steps no longer recheck a later member the job has not
  touched), and the terminal `realm_job` lists it in `skipped`; the mind settles a done leave as
  usual and backs the skipped members off for 600 s (evidence `skipped_at`), and Recall rearms and
  defaults only the rest. Any other reason from the job
  is a Refuse only until that leave has changed a body. A failed `realm_job` may now carry
  `changed: false`: native proved the entire job changed no body or cleanup state and released
  it, including all earlier members and pending steps. A cleanup-closure refusal before any
  mutation carries that proof; earlier changed or unproved steps still prevent it.
  With a positive ack, an ordinary root
  without children settles `step_refused` and holds nobody; startup rechecks an uncontested
  stored unknown with that proof. Missing/true flags keep the old rules; WG, ack and result
  roots do too, and contradictory evidence still becomes conflict. The settlement logs
  `realm_action_step_refused` (id, kind, reason code, member count). See
  `modules/mod-leeroy/README.md`; it needs a native rebuild and an updated mind.
  A root that settles unknown or conflict logs a warning in mind.log,
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
  writes nothing while `[realm] enabled` is false. His Board replies (`board_reply`, migration
  0020) go first, oldest first, inside those caps of 3 posts and 3 letters. A forum reply that
  passes the forum screen and the forum fact's own (checked before any model call) becomes his
  public `forum_post_v1` fact, known by every realm guild by `forum`, and the post's author
  answers it in the same thread while they can still write (else the night's author pool;
  nobody leaves it `posted`, unanswered), with memories as other posts get them (the way back in
  for a thread about an event fact, none for a rumour's). A letter reply that passes the letter
  screen becomes that member's private memory with him (key `board-reply:<id>`) and an answer
  letter (`realm_mail` kind `reply`, `ready`) the post office delivers under its usual limits.
  His text refused is settled `refused` with its reason and never retried; an answer refused or
  not given (the model's error) leaves his reply waiting for another night, at most 3 such nights
  (meta `board:reply:<id>:failed`), then it is settled `refused`, `no_answer`; one he withdrew
  before the commit is left alone.
  While the screen is not ready his forum replies wait; letter replies do not.
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
  While the service is down the mind retries after 5, 10, 20 and 40 s, then once a
  minute, and logs `voice_connect_failed` once, `voice_still_down` (a count) at most
  hourly and `voice_reconnected` when it is back; a dropped session still reconnects in 5 s.
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
- **Photo style variety.** With the mind stopped, `selfies restyle --dry-run|--go`
  previews or changes choices; `--go` snapshots before migration and records old kinds.
  Lust choices are composed; plain kinds are cleared for the night's model to re-choose.
  Fresh sets preserve the gallery and cover. `--lust-only` narrows the selection;
  hand-set kinds require `--include-owner`. `[selfies] variety = true`, read once at
  startup, balances Lust choices and adds screened plain-only hints to the plain pass.
  Lighting matches the setting and drops before identity or clothing at prompt fit.
  See `mind/leeroy_mind/selfies/style_variety.md`.
- **Appearance variety.** `[looks] diversity = true`, read at startup (or once per
  generator command), gives new members screened deterministic looks. It does not
  re-enrich existing owner/seeded members with looks but no disclosures. With the
  mind stopped, `selfies variety --dry-run|--go --top 30` diagnoses/re-rolls similar
  chosen photos. First fetch the pinned IP-Adapter: `fetch-models --only ipadapter`
  in the selfie worker environment; the command never downloads it. `--go` snapshots
  before migration and records the old looks/kind for `personas undo LEDGER_ID`.
  Owner field-change evidence protects looks; old rows without such history fall
  back to current owner source. `--release GUID[,GUID...]` permits named protected
  members listed inside the current dry-run clusters. Hand-set photo kinds stay.
  Plain kinds are re-chosen by the night; scene planning waits for a new cover pick.
  Galleries and the current cover remain intact. See
  `mind/leeroy_mind/personas/looks_diversity.md`.
- **Two night stages, only with `enabled = true`.** `photo` runs after `enrich` on the
  reflection model: it picks a photo kind, framing, setting, outfit and expression for each
  member who has looks and no kind yet (stored as a new persona version, screened for the
  owner's real name, member names, era and explicit wear). `selfies` runs after `regress` as an
  image stage: it queues a first set for each member with looks and a kind, plus any "4 more"
  asked for, and renders up to `selfie_per_night` (120) sets.
  A woman whose stance toward the owner is lust, aged 25 or more, whose looks (hair colour and
  style, skin, eyes, build, face details) use no word of the age guard (`selfies/style.py`
  `suggestive`), gets the lust style, and her kind is picked, not written: the request's schema
  (`LUST_SCHEMA` in `selfies/kind.py`) allows only values from fixed lists (21 settings, 28
  outfits, 12 colours, 8 expressions; kind and framing keep their enums), so a reply off the
  lists fails the schema, and the code builds the stored kind from the picks (`compose_lust`,
  the colour put into the outfit). Pairings (`screen_lust`): swimwear only with the six water
  settings, only framed upper body and never as a headshot; the hot tub only in swimwear; the
  sports top and leggings only at the gym. A composed part that holds her character or real
  name (a colour or a season can be one) is refused too, so the prompt's name filter never drops
  the joined phrase below. A reply that breaks only these is asked once more, then refused. Her
  template (`KIND_PROMPT_LUST`; the owner's overlay
  `prompts/nightly_photo_lust.txt` replaces it) only flavours the pick. At render time
  (`prompt.build_prompt`, given the kind's recorded style) the lust prompt is used only for a
  kind the lust rules composed while she is still of the lust style: her outfit and setting go
  in as one priority-0 phrase, `wearing <outfit>, <setting>`, so the worker's 75-token fit keeps
  both, and the negative drops `cleavage` and `swimwear` and adds `topless`, `underwear`,
  `see-through` and `sheer`. A plain or hand-set kind on a lust woman renders with the plain
  phrases and `NEGATIVE`. A kind the lust rules composed is never rendered for a woman who no
  longer has the lust style (she left lust, or her looks now code young): `renderable` holds
  her set (log `selfies_set_held`, guid only) until the pass has chosen her kind again under
  the plain rules, so its text is never rendered plainly; one whose joined phrase her name filter
  would drop (renamed since it was composed) is held the same way (`reason=name`) and chosen
  again under the lust rules. A held set takes no GPU (`wants_gpu`), and "4 more" for her says
  it waits. Everyone else
  writes a free-text kind under the plain rules (`KIND_PROMPT`, `EXPLICIT_RE`, plus `WEAR_RE`
  for underwear, sleepwear and see-through words such as bralette, camisole, pajamas, mesh,
  fishnet and translucent), and every
  free-text kind, whoever it is for, also meets the age guard (`selfies/style.py` `AGE_RE`:
  school, schoolgirl, schoolboy, schoolkid, high-schooler, school or student uniform, teen,
  teenage, teenaged, teenager, teeny, teenybopper, preteen, tween, youthful, childlike,
  childish, child, children, kid, kiddie, young girl, young-looking, barely legal, lolita, loli,
  underage, underaged, adolescent, jailbait or jail bait, baby face, baby-faced, babyface,
  brace-face, little girl, cheerleader, pigtails or pig tails, braces, and fresh-faced, a
  young face, looks young, younger-looking, babyish, schoolgirlish, teenie, barely 18, just
  turned 18, in bunches; a multi-word one matches with spaces, hyphens or nothing between its
  words, and looks are read with their whitespace folded). When her style changes
  the pass chooses her kind once more (meta
  `selfies:photo-style:<guid>` records which rules chose each kind). A kind set by hand is never
  redone, with one exception: a kind from before the records counts as the pass's plain kind
  when the pass once had a unit for her, so one the owner set by hand before the first photo
  night after this release cannot be told apart and may be redone once; from that night on
  (meta `selfies:photo-style:adopted`) every hand change is detected. A re-choice into the lust
  style queues a fresh set for her (a first set if she never had one, else a "4 more" one; sets
  waiting for his choice stay waiting). The NSFW gate (`nsfw_max`) is unchanged for every image
  of both styles.
- **Offline photo booth.** `[selfies] booth = true` additionally renders profile sets between
  nights. It defaults to false and is read only at startup; an invalid selfies table means
  rendering off. Its background loop checks once a minute, outside chat/control handling.
  Both `enabled` and `booth` must be true, the owner's nightly Presence must have counted at
  least 10 minutes offline, no live lease may be held, and no night may be running or due.
  Requests must be at least `booth_wait_minutes` old (default 10, range 1-60); a newly available
  first set is queued and timestamped at the loop's first eligible observation. Every request
  still passes the shared renderer in `selfies/render.py`, including held-kind and NSFW rules.
  `booth_sets_per_day` (default 6, range 1-120) caps completed attempts per local calendar day:
  ready, refused and failed sets consume that allowance, survive restart and are not retried
  by the booth that day. Held sets take no allowance. The night's `selfie_per_night` cap and
  its ability to render remaining/refused booth sets are unchanged. A set crossing midnight
  remains queued and is rendered under the new day's allowance at a later eligible check.
  A due night cancels the booth and waits for the whole worker tree to exit; one shared lock
  covers each run and its cleanup. Every live lease preempts both through the same arbiter.
  Normal completion makes room before rendering and again after worker exit; cancellation
  makes no arbiter call before hand-over. `selfies status` includes separate `booth` daily
  stage totals and the last pass (sets, attempts, seconds, outcome); `selfies_booth_done`
  logs sets, seconds and outcome. Receipt/provenance keys use `booth:<local-date>` so booth
  work cannot reset a night's stall counter or appear as nightly work in reports.
  No night schedule, photo choice, or content rule changes.
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
  `/api/selfies/member`) and, for the gallery, `GET /api/selfies/gallery?guid=` (a member's
  retained candidates of every ready set and rendered situations, newest first, with cover flags
  and keyed URLs; read-only) and `GET /api/selfies/photo?guid=&kind=candidate&set=&index=` (or
  `kind=snap&id=`, optional `thumb=1`; one rendered JPEG of that member's, else 404, never a disk
  path) and sends `POST /api/selfie` (pick, undo, more) to the mind's `selfie`
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
  Any rendered candidate of any ready set can be picked as the cover, not only one from a set
  waiting for his choice (a decided set keeps its prior state for undo). Exception: a set
  rendered under the lust style (recorded at `mark_ready` as meta
  `selfies:set-lust:<guid>:<set>`; a set with no record counts as plain) cannot be picked while
  the member is not lust-styled (`style.suggestive`: she left lust, or her looks now code young);
  the mind's pick refuses in words. The gallery neither lists nor serves such a set's photos
  unless one is already her cover.
- **Situational photos and the guild photo (P4, night side only).** Off by default: they need
  `[selfies] enabled = true` and `snaps = true` (also `identity_scale`, 0.6, and
  `identity_min_free_gib`, 13.5, the identity worker's VRAM floor), else no stage is added
  (`selfies_snaps_on|off|need_enabled` logs). **Photos are made and stocked; SENDING them
  arrives with the guild Discord's photo phase (D3).** Nothing in WoW, the console or the
  members' talk changes, and the romance "picture" stage stays off until a sender exists.
  - *Identity adapter.* The worker keeps a member's face in a new scene with IP-Adapter Plus Face
    for SDXL (`h94/IP-Adapter` at revision `018e402774aeeddd60609b4ecdb7e298259dc729`, Apache-2.0,
    with the OpenCLIP ViT-H/14 laion2B image encoder, MIT; about 3.4 GB), pinned as
    `MODEL_PINS["ipadapter"]`. The reference is a square crop of her chosen photo
    (`history/<pick>.jpg`, never `chosen.jpg`) cut by her photo kind's framing. The worker runs
    with `--identity` (the mind passes it for the snaps stage and `selfies try --snaps`); it is
    fetched only by `fetch-models` (`--only ipadapter`), and `smoke --identity --ref FILE`
    measures its VRAM before `identity_min_free_gib` is trusted. Doctor notes it as not
    fetched with snaps off and FAILs with snaps on. Situational photos are 832 x 1216 portraits
    (thumbnail 384 px); the NSFW gate and three seeds per slot are P2's.
  - *Stages.* `scenes` (reflection model, after `life`) plans: the reflection model chooses
    enums for a member from tonight's life beat, her card birthday tomorrow or an ordinary day,
    and may decline. `scene_choices.py` composes 30 activities, 20 places, time, framing,
    expression, clothed outfit and a prop (including `none`). Activity/place and prop/activity
    pairings refuse incoherent picks. Off-list replies get one schema retry, then no scene.
    Lust settings/outfits/colours/expressions reuse the profile lists and their pairings;
    situational settings exclude the hot tub, crowd and hallway mirror. New stored scenes carry
    `recipe: 1`. Pre-enum pending scenes (or an unknown recipe) stay planned with the held code
    `legacy_scene_schema`, use no render/GPU slot, and expire normally; their rows and files are
    preserved. No free-prose fallback approves them. Already rendered photos are unchanged.
    The screen (`selfies/scene.py`) remains a second defence against
    looks changes, other people,
    names, likeness, era, minors, explicit wear and undressing or bathing contexts, and a lust
    woman's scene reuses the profile photo's enum picks. `snaps` (image model, after `selfies`,
    `wants_gpu`) renders what is planned, each with the identity reference, under P2's GPU
    rules (live lease ends it with no failure; his login ends the worker's whole process tree
    before the live model's preload). Caps per night: `scenes_per_night` (24) and `snaps_per_night`
    (40), both counting tonight's units on a resumed night.
  - *Stock and ration.* The `selfies` world dial (0-10, `[talk] selfies`, default 3; console
    Tuning) sets photos a week across the guild at most: 0, 1, 2, 3, 5, 7, 10, 14, 20, 28, 40.
    The stock the night keeps is twice that (at most 40), at most 2 a member; a member without a
    chosen profile photo gets none; birthdays first, then tonight's beats, then a draw by send
    weight. Dial 0 plans nothing.
  - *Expiry.* A photo is wanted from 06:00 after its night for 3 days (life, guild), 5
    (everyday) or 18 hours (birthday), in its time-of-day phases. It also expires when her chosen
    photo or looks change (`photo_changed`, `looks_changed`) or its reference is missing. Expiry
    only changes the row (`ended`): **no photo file is ever deleted** (owner, 2026-11-07), so old
    photos stay on disk, unserved.
  - *Guild photo.* The night after the newest online raid with a kill (it ended within 12 hours
    before the night began), at most one a week, up to 16 of that raid's members with a chosen
    photo (at least 3) are each rendered alone at their desk in a candid tile (1024 px),
    with a monitor displaying a landscape game map. Own looks and monitor content stay at
    priority 0, with required negatives for duplicate faces and faces on screens. Each tile
    already has its own GUID/slot/attempt seed and immutable reference; crop and adapter scale
    are unchanged pending GPU evidence. Overlong required identity text refuses the render.
    then composited at 512 px with 8 px gutters in a grid by tile count: 1 is 1x1, 2 is 2x1, up to 4 is
    2x2, up to 6 is 3x2, up to 9 is 3x3, up to 12 is 4x3, up to 16 is 4x4. He is never in it.
  - *Storage.* Rows in `selfie_snap` (migration 0022). Files: `selfies/<guid>/snaps/<id>.jpg`
    with `.thumb.jpg`; `selfies/guild/<id>.jpg` and `.thumb.jpg`, tiles in `selfies/guild/<id>.part/`
    while it renders. Nothing here writes `chosen.jpg`, `history/` or `sets/`.
  - *Commands.* `selfies status` also prints the snap counts (planned, ready, sent_week,
    expired_week) and the `scenes`/`snaps` stage records. `selfies try --snaps [--guid N |
    --limit N] [--guild]` (mind stopped) renders one situational photo each with the member's
    face, and with `--guild` one grid, into `scratch\selfies-try\snaps\`; members with no
    chosen photo (`no_photo`) or no photo kind (`no_kind`) are skipped and counted. Readiness
    prints per-GUID declined/refused/rendered status, refusal codes, and NSFW score (unavailable
    before rendering; the last score after all seeds are flagged), also in JSON `results`.
    `guild_similarity` reports an uncalibrated head-crop embedding cosine maximum, mean and
    closest GUID pair. The worker reuses its loaded IP-Adapter image encoder, never downloads
    a model; missing/failed encoders produce a skip code. This is a comparative tile diagnostic,
    not a face detector, calibrated threshold or identity proof. The operator checks distinct
    likenesses and single faces on actual GPU images before enabling snaps. The mind's
    doctor reports the identity adapter's state beside the worker's checks.
- **Content limits.** Adults only, clothed and non-explicit, no real person's likeness, and no
  person's name ever reaches a prompt. Logs hold ids, counts, scores and timings, never a prompt
  or persona text.
- **Snaps rehearsal.** `mind/tests/selfies/test_snaps_rehearsal.py` drives a real `MindApp` with
  snaps on and the fake worker in identity mode through a night (three situational photos and one
  guild photo with files on disk, `selfies status` counts), then a second night whose worker
  hangs: his login leaves the worker's pids dead before the preload and no partial photo, a
  planned photo of a replaced profile photo expires unrendered, and stopping the app removes
  `control.json` and closes its ports.
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
