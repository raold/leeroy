# The dojo

An isolated environment for training leeroy's combat policies with deep
reinforcement learning. It runs against its own AzerothCore worldserver,
databases and fixture characters, never the live realm, and steps the training
world on a virtual clock so it can run faster than real time.

This is the foundation increment: an isolated layout and its preflight check;
the environment protocol with its lifecycle state machine and loopback
transport, written once in pure C++ (`modules/mod-dojo/src/Lib`, no
AzerothCore includes) and once in Python (`leeroy_dojo`); a deterministic fake
worker; a checksummed episode recorder; `mod-dojo` registered as a native
module that builds into an isolated core; a disposable training realm with two
synthetic fixture actors in a training-only arena; exclusive control of those
actors through a narrow executor that plays the part of a client (plan Task 6);
and each actor's observation under visibility and a perceptual delay, with
episodes that reset by tearing the arena and the actors down and building them
again (plan Task 7). There is no learner and no Torch or Gymnasium dependency,
and nothing in the worldserver opens an endpoint yet: commands come from the
console.

## Layout

The dojo keeps everything apart from the installed server. A layout file
names each root by absolute path (see
[`scripts/dojo/layout.example.json`](../scripts/dojo/layout.example.json)):

| Root | Holds |
|---|---|
| `source` | a worktree of this repository (`modules/mod-dojo`, `dojo/`) |
| `core` | a separate AzerothCore checkout, from the recorded baseline commit |
| `playerbots` | a separate mod-playerbots checkout, likewise |
| `build` | a fresh CMake build tree, never the installed `build/` |
| `runtime` | binaries, private configuration, ports, process receipts and logs |
| `data` | manifests, trajectories, checkpoints and evaluation output |

The core's `modules/` holds only `mod-playerbots` and `mod-dojo`, each a
junction into the isolated checkouts. The training realm has its own MySQL
instance (its own data folder and loopback port, `database.server`), schemas
and restricted database user, built from the dojo core's own SQL; it never
connects to the live database server. `runtime/dojo.marker` marks a training
instance:

```json
{"marker": "leeroy-dojo-training", "instance": "dojo-01"}
```

## Preflight

`scripts/dojo/preflight.ps1` (PowerShell 7.2+) checks a layout and prints one
`ok`/`FAIL`/`note` line per check. It never starts a server, connects to a
database or prints a credential; the only programs it runs are git (rev-parse,
remote, status, merge-base, with fsmonitor off) and the version queries of
cmake, Python and vswhere.

```powershell
pwsh -NoProfile -File scripts/dojo/preflight.ps1 -Layout <layout.json>                    # check
pwsh -NoProfile -File scripts/dojo/preflight.ps1 -Layout <layout.json> -Json              # machine-readable
pwsh -NoProfile -File scripts/dojo/preflight.ps1 -Layout <layout.json> -WriteProvenance   # record baselines
```

It refuses rather than passes whenever it cannot positively identify what it
protects: the layout's `production` paths must exist, the live core must be a
git checkout, the live `CMakeCache.txt` must name that core, and the live
`worldserver.conf` must yield readable database targets (a named pipe or an
unparseable value is refused). It then refuses a root that is, is inside or
contains the live install, its core, build, configuration, modules or
protected folders; a module set other than playerbots and dojo (plus listed
experiment modules); a module whose `.cmake` or `Add<dir>Scripts()` loader does
not follow its folder name (CMake would skip the one silently and the link
would miss the other), or a `mod-dojo` that does not ship `Dojo.Enable = 0`;
a junction or symlink that leaves the isolated checkouts,
including one nested anywhere inside the core or a module; a missing or
mismatched marker; a training database that is not on loopback, names a live
schema (this machine under any name: loopback, host name or interface address,
is the live server) or uses the live database user; a production or busy port,
whether the layout requests it or a training `worldserver.conf` or
`authserver.conf` would bind it (a missing key means AzerothCore's default);
a non-loopback dojo endpoint; missing client data; and a core or playerbots
checkout that does not descend from the baseline commits in the provenance
manifest. `-WriteProvenance` records the branch, commit, remotes and SHA-256
fingerprints of every dirty file for the live checkouts the dojo derives from
and for the isolated checkouts. It writes nothing until every live-install and
root check has passed, and then only inside the data root, never over a file
that is not a provenance manifest. Later runtime commands require that
manifest and the marker. Exit status is 0 when nothing failed, 1 on a failed
check and 2 for an unreadable layout.

## The native module

`modules/mod-dojo` follows mod-leeroy's shape: `CMakeLists.txt` hands `src/`
to the script loader, `mod-dojo.cmake` sets up the core's `modules` target,
`src/mod_dojo_loader.cpp` defines `Addmod_dojoScripts()` and
`conf/mod_dojo.conf.dist` ships the keys:

| Key | Default | Meaning |
|---|---|---|
| `Dojo.Enable` | `0` | the master switch |
| `Dojo.Marker` | `""` | must be `leeroy-dojo-training` for `Dojo.Enable = 1` |
| `Dojo.Instance` | `""` | the layout's instance, 1-64 of `A-Za-z0-9._:-` |
| `Dojo.Scenario.Manifest` | `""` | a scenario manifest by absolute path |
| `Dojo.Scenario.Sha256` | `""` | its pinned hash; both or neither |

The runtime (`src/Runtime`) reads them once at start and logs one line: `off`
(`Dojo.Enable = 0`), `refused` (on, but a disabled build, a missing marker or
instance, or scenario keys that are not an absolute path and a 64-digit
lower-case hash; logged as an error) or `armed` (a training build with its
training configuration). Off and refused start nothing and control no bot.
Armed loads the pinned scenario (see below); its fixture actors then move only
through the console's `dojo` commands, and there is still no endpoint: the
step driver brings that. A config reload is reported
and never applied. `src/Lib` stays pure and is compiled into the core as well
as into the standalone tests. The disabled variant compiles no fixture,
arena, command or tick code at all.

The build has two variants, chosen by the CMake option `AC_ENABLE_DOJO`
(default `OFF`). Off is the disabled variant every core containing mod-dojo
gets: it refuses `Dojo.Enable = 1`. `ON` is the training variant, which
`mod-dojo.cmake` configures only when the core's other modules are absent or
disabled (playerbots and dojo, D02). The switch reaches the runtime sources
alone as `DOJO_TRAINING_BUILD`, so flipping it recompiles only those, and they
refuse to compile without it, which also catches a skipped `mod-dojo.cmake`.
The core's own time and control hooks will read the same switch: their patch
series (`patches/azerothcore/`) declares it beside the other options in the
core's `conf/dist/config.cmake`, reports it in `src/cmake/showoptions.cmake`
and adds its definition there, as `WITH_COREDEBUG` does; `mod-dojo.cmake`'s
own declaration then does nothing.

mod-leeroy and mod-dojo both put their `deps/` on the `modules` target, and
the first include directory wins, so their vendored `nlohmann/json.hpp`
copies must stay byte-identical: `tests/test_module.py` and `mod-dojo.cmake`
both refuse a difference.

## Isolated build

`scripts/dojo/configure.ps1` and `scripts/dojo/build.ps1` build the layout's
core into its own build root. Both run the preflight first and stop unless it
passes; neither uses `ac-build.sh` or touches the live build.

```powershell
pwsh -NoProfile -File scripts/dojo/configure.ps1 -Layout <layout.json> [-Variant disabled|training]
pwsh -NoProfile -File scripts/dojo/build.ps1 -Layout <layout.json> [-Jobs 4]
```

`configure.ps1` configures a missing build root fresh (Visual Studio 2022,
x64, RelWithDebInfo only, static scripts and modules, both servers, no tools)
with Boost, MySQL and OpenSSL from the layout's `toolchain` section, and
reconfigures an existing one only when its cache names the dojo core and the
same generator. It checks that the generated script loader registers exactly
`mod_playerbots` and `mod_dojo` and that the variant reached the cache and the
runtime sources, then writes `dojo-configure.json` in the build root.

Both also run `scripts/dojo/patches.ps1` (below) and stop unless the isolated
playerbots checkout carries exactly the dojo's playerbots patch series;
`configure.ps1 -ApplyPatches` lets it apply a missing tail first.

`build.ps1` lowers its own priority to below normal (cmake, MSBuild and every
compiler inherit it), builds with a true global compiler cap (`-m:N`,
MultiToolTask, `EnforceProcessCountAcrossBuilds`, `CL_MPCount=N`, no node
reuse; default 4, at most 16) and reports the peak number of compilers its
build ran. Never
interrupt it mid-compile (CLAUDE.md, build trap 4); stopping the script leaves
the build running. It then checks the link, not just the exit code:
`modules.lib` defines `Addmod_dojoScripts`, `AddSC_dojo_world` and
`Addmod_playerbotsScripts`, `worldserver.pdb` names them,
`worldserver.exe` carries the configured variant's tag and not the other's,
and the copied `mod_dojo.conf.dist` keeps `Dojo.Enable = 0`. Its receipt goes
to the build root's `dojo-build.json` and to the data root's `builds/`.
Building starts nothing: running the worldserver needs the training realm and
its configuration.

## Training realm

`python -m leeroy_dojo realm --layout <layout.json> <command>` (Python 3.12,
stdlib only) provisions and runs one disposable realm. It never connects to
the live server: the layout's `database.port` may not be a live database port,
every client call refuses any other port before it runs, and anything that
changes data first checks the server's port, datadir and recorded
`server_uuid`. Credentials are generated locally, live only in git-ignored
files under the dojo runtime (client option files and the instance's configs)
and reach `mysql.exe`/`mysqladmin.exe` through `--defaults-file`, never a
command line or a log.

| Command | Does |
|---|---|
| `db-init` | writes `<server root>/my.ini` (loopback, its own port and data folder, X plugin, named pipes, shared memory, binary log and performance schema off, a small buffer pool) and initializes a fresh data folder with the installed MySQL binaries; an `--init-file` sets the root password, creates the four dojo schemas and a user with `ALL` on exactly those (`_` escaped in the grants) and nothing else |
| `db-start` / `db-stop` / `db-status` | runs `mysqld --defaults-file=<my.ini>` as a hidden below-normal-priority process (never a Windows service), shuts it down with `mysqladmin shutdown`, reports it |
| `db-check` | the instance's identity and options, its schema set, its users, the restricted user's exact grants and four refusals (create a schema, read `mysql.user`, grant itself more, drop another schema's table) |
| `provision` | copies the dojo build's worldserver and the DLLs it loads into the instance's `bin/`, then writes `configs/` from the build's `.dist` files and `scripts/dojo/training-rules.json`; `--scenario` pins a manifest's hash in `mod_dojo.conf` |
| `schema-build` | runs the staged worldserver with `--dry-run`: the core's own auto-setup builds and updates the dojo schemas from the dojo core's SQL (`SourceDirectory`), the playerbots module's included, then loads the world once and exits |
| `accounts` | creates the RA admin (gm level 3, password kept in `secrets/ra.json`) and the fixture account (its password discarded) and points realm 1 at the dojo world port |
| `world-start` / `world-stop` / `world-status` / `ra <command>` | starts the worldserver after the preflight passes (working directory the instance, `AC_*` overrides stripped from its environment), drives it through RA on loopback, and stops it with `server shutdown` so players are saved |
| `acceptance --scenario <json>` | opens the training arena, verifies both actors, closes it and compares the fixture rows before and after (see below) |

The training configs are each `.dist` with a reviewed set of differences:
loopback `BindIP` and `Ra.IP`, `MapUpdate.Threads = 0`, no console, RA on,
SOAP off and no raised process priority for the worldserver; for playerbots,
no random-bot population, accounts, guilds or arena teams, no queues and no
cheats (`Playerbots.BotCheats = ""`, empty world-buff matrix). Gameplay keys
keep their stock WotLK values; the rules file lists the ones that must, every
other key must equal its `.dist` value and every `.dist` key must be present
so no compiled-in default can differ unseen. The preflight enforces the rules
before any start, and the armed worker checks the effective values again
(rates, expansion, level cap, arena preparation, map threads, bot cheat mask,
world buffs, random-bot settings) and refuses its scenario if any differs.
`TempDir` stays inside the instance, because the core's updater writes its
mysql option file there.

### Scenarios and fixtures

A scenario manifest (`scenarios/*.json`) freezes the arena (map, battleground
type, arena type, bounds, preparation and match time, unrated), the two
synthetic fixture actors (account, names, arena side, start positions, a
staging point outside the arena) and their one shared loadout: level, race,
class, gender, appearance, talents with ranks and spell ids, glyphs, equipment
by slot, the kit's spells and ranks, the whole active spellbook and the
initial maximum health and mana. `python -m leeroy_dojo scenario
check|hash|pin <json>` validates its shape and pins the SHA-256 of its
canonical JSON next to it (`<name>.sha256`); mod-dojo's pure parser computes
the same bytes and refuses the same malformed documents
(`modules/mod-dojo/tests/fixtures/scenario_cases.json`). At start the armed
worker loads the manifest, refuses a hash other than the pin and validates
every id against the installed data: the map is an arena and the
battleground's start positions are the manifest's, the bracket exists, race
and class exist together, each kit spell has its name and rank and is the
highest rank for the level, each talent is the class's and its rank is the
talent's last, each glyph casts its spell, each item exists by name without
random properties and fits the race, class and level. A failed lookup refuses
the scenario; nothing is substituted.

`frost_mirror_small_v1` is two level-80 Human Frost Mages (71 points in Frost,
the Frost Nova and Ice Lance glyphs, Savage and Hateful Gladiator gear with no
gems or enchants) with the kit Frostbolt, Ice Lance and Frost Nova at their
highest ranks. The spellbook also freezes what ordinary rules give every such
character at each login (general abilities, the racial, the class skill
lines' first ranks of Fireball and Frost Armor, the talents' own spells); the
action catalog, not the spellbook, restricts what an actor may use.

The worker's console commands (RA or console only):

| Command | Does |
|---|---|
| `dojo status` | state, scenario hash, actors, arena, job and result, and the last evidence as one `dojo-evidence {json}` line |
| `dojo scenario` | every rule and id check, `ok` or `FAIL` |
| `dojo fixture create` | creates the named characters on the fixture account, refusing a name another account holds |
| `dojo fixture build` | logs them in, moves them to the staging point, applies the loadout under the core's own rules (`LearnTalent` checks points, tiers and dependencies), clears transient auras, verifies the result against the manifest, saves and logs out |
| `dojo arena open` | logs them in, verifies both against the manifest and each other, creates an unrated instance of the stock arena that no queue can join (registered for updates, never offered to the free-slot queue), invites and sends one actor to each side, and verifies them in the map: on their side, at their start (within 1 yd), on the ground, in bounds, loadout as frozen, full health and mana, identical to each other |
| `dojo arena close` | takes them out, during preparation or after the gates opened, removes the auras and the raid-leader flag the arena left behind, restores the staging baseline (nothing on cooldown, full health, mana and durability), logs them out and waits for the empty instance to go |
| `dojo fixture restore` | logs them in, moves them to the staging point, restores the staging baseline without touching items, talents or spells, verifies the loadout, saves and logs out |

The arena's stock win check stays off for the dojo's own instance for as long
as it exists, so leaving, logging out or dying there ends no match and records
no outcome or achievement criterion; the dojo will decide outcomes itself. A
fixture earns no achievement progress anywhere (the criteria and completion
hooks refuse it). A logged-in fixture is owned by the external controller
(below) from its AI's first tick. Every untriggered cast by a fixture that no
executor command produced is counted as a stock cast and reported; an owned
fixture's count is 0. This is ordinary arena entry, preparation and leaving
for one actor per side, not a claim that a stock queue supports 1v1.

`acceptance` snapshots every row of the fixtures (character, achievements,
arena stats and teams, quests, reputation, skills, spells, talents, glyphs,
items, mail, cooldowns, auras, groups) and realm-wide counts (characters,
arena teams and logs, battleground statistics, accounts, random-bot accounts
and characters, guilds) before opening and after closing. A progression change
fails it; position, played time, logout time and similar bookkeeping are
reported. It also fingerprints the live install before and after (config
hashes, the shared `Data` folder's listing, the live servers' processes and
the MySQL service) and records the worker's established TCP peers, which must
all be the dojo instance. Its report goes to
`<data root>/runs/<stamp>-realm-acceptance/`.

## External control and execution

### The playerbots hook

The dojo's only change to mod-playerbots is an external-controller interface
in `PlayerbotAI` (`src/Bot/PlayerbotAI.{h,cpp}`), shipped as a reviewed patch
series in [`patches/dojo/mod-playerbots/`](../patches/dojo/mod-playerbots)
(never `patches/mod-playerbots/`, which `scripts/ac-snapshot.sh` owns). It is
inert until a caller acquires a bot:

- `AcquireExternalControl(owner)` returns an epoch, unique for the process's
  lifetime, or 0 for a self-bot, a bot with a game-client master or one that
  is already owned. It drops what was queued for stock behaviour: reactions,
  chat commands and replies, queued packets and a pending interrupt request.
  `ReleaseExternalControl(epoch)` takes only the current epoch, so a stale
  owner cannot release a newer one, and resets the AI as a far teleport does
  (`Reset(true)`), so stock behaviour resumes from a clean state. Ownership
  lives in the `PlayerbotAI` and ends with it: a logout or shutdown ends it.
- The gate sits in `UpdateAI` right after the state checks and the fall
  information, before the cheat refill, `UpdateAIReaction`, the
  preparing-cast handling (cast cancellation and facing), the transport and
  group-master updates and `UpdateAIInternal` (chat replies, commands, packet
  handlers, the engines). Owned with no command is simply this gate's idle
  state.
- While owned, `HandleCommand` (both forms), `QueueChatResponse`,
  `DoSpecificAction`, `DoNextAction`, `CastSpell`, `CastVehicleSpell`,
  `RequestSpellInterrupt` and the master's packet handlers refuse their input
  and count it; the bot's own outgoing packets are dropped except root,
  unroot, knockback and dismount, which emulate what a client does.
- Housekeeping stays outside the gate: teleport and world-port acks and the
  bot session's packets (`PlayerbotHolder::UpdateSessions`), login and logout.

`scripts/dojo/patches.ps1 -Layout <layout.json> [-Apply] [-Json]` checks the
isolated checkout against the series: tracked files clean, and its commits
since the provenance baseline exactly the series by stable patch id; `-Apply`
applies a missing tail with `git am --3way` and never resets or drops a
commit. It also checks the gate's place in `UpdateAI` on every run, as the pin
guard check does. The nightly `patches` workflow applies the series on top of
`patches/mod-playerbots` and runs the playerbots codestyle check.

### The controller and its executor

`src/Runtime/DojoExecutor.*` owns the scenario's two fixture actors, and only
them: the director registers the characters it resolved on the fixture
account, and the controller acquires a registered actor as soon as its AI
exists, unless the console handed it back. It never holds a pointer across
ticks; each tick it looks the actor up and compares epochs, so a logout or a
new AI shows as ownership lost. Acquisition neutralizes what stock behaviour
may have started (a cast, the spell queue, movement, auto-attack, a pet) and
traces it.

The executor plays the part of a client, so ordinary server rules apply. A
cast is a `CMSG_CAST_SPELL` request with the actor's cast count handed to
`WorldSession::HandleCastSpellOpcode`: costs, cooldowns, the global cooldown,
the spell queue window, range, line of sight, facing, crowd control and
immunity are the core's, and the result is what the server tells the session
(`SMSG_CAST_FAILED`, `SMSG_SPELL_START`, `SMSG_SPELL_GO`). Stop-cast is
`CMSG_CANCEL_CAST`, selection `CMSG_SET_SELECTION`, auto-attack
`CMSG_ATTACKSWING` and `CMSG_ATTACKSTOP`, each through its handler.
Facing turns in place with an orientation-only spline; movement is a spline at
the actor's own run (or run-back) speed for `250 ms`, keeping its facing, to the
first collision along the direction (`MovePositionToFirstCollision`). Movement
is refused while dead, stunned, rooted or without control; turning only while
dead or without control (a rooted client can turn). A command's parts run in
the order stop-cast, hold, facing, ability, movement, so a move launched with
a cast-time spell cancels it under the core's own rule. PlayerbotAI's
`CanCastSpell` and `CastSpell` (facing adjustment, pet autocast) are never
used, and nothing else is castable: pets are unsupported and a pet found at
acquisition is dismissed.

### Catalog, masks and the command queue

The v1 catalog (`src/Lib/DojoActionCatalog.*`, `leeroy_dojo.actions`, schema
`leeroy-dojo-actions-v1`) is entry 0, the noop, then every
`<ability>/<movement>/<facing>`: abilities `none`, one cast per kit spell in
manifest order, `stop_cast`, `target_enemy`, `clear_target`, `attack_on`,
`attack_off`; movements `hold` and eight directions relative to the facing;
facings `keep`, `toward`, `away` (the visible enemy). A cast's target slot is
fixed by its spell: the enemy for a spell that needs an explicit unit target,
the caster otherwise. For `frost_mirror_small_v1` that is Frostbolt, Ice
Lance and Frost Nova only, 244 actions, SHA-256
`60d4f7e4b05d7548bb142bb2e71e0a862809658470fd61dbe499fde86242712a`; the
acceptance publishes it as `<data root>/manifests/actions-<scenario>.json`.

Masks are a pure function of what the actor knows (`ActorKnowledge`, taken
from its delayed observation; see below) and mask only what cannot succeed
(D23); the noop never is. A wait (cooldown, global cooldown, a cast in
progress) masks only when it outlasts the knowledge's age (the 100 ms
perceptual delay), the command delay and the core's spell queue window
together; mana only when one regeneration tick could not cover the cost; range
only beyond a 3-yard slack. Facing with `keep`, roots, stuns and immunity
are left to execution, and so is line of sight as a rule; but an enemy out of
the actor's sight is not visible to it (see the observation below), so once
that has been perceived its enemy-targeted actions are masked as `no visible
enemy`, and a forced one is still the core's to refuse. Turning away
while casting a front-facing spell is the one combination masked in every
state. The knowledge read calls only getters, and the cost and range lookups
pass no spell, so `ApplySpellMod` registers and drops no charge; `dojo
control maskcheck` fingerprints both actors (selection, auras with stacks and
charges, cooldowns, global cooldowns, the current spell, the spell queue,
position, splines) before and after any number of queries. Both languages run
the shared cases in `modules/mod-dojo/tests/fixtures/action_cases.json`.

`DojoActionQueue` holds one pending command per actor (D30): it becomes due
150 ms after its input; a new command replaces a pending one that has not run
and is recorded as superseding it; a noop keeps whatever is pending; an
executing command is never superseded. Release, logout and shutdown drop the
pending command and record why.

### Tracing

Every action is traced with its producer, origin, action id, input, due and
execution times and the outcome or reason: `executor`, `acquire`, `release`,
`harness` (acceptance set-up), `core` (a cast the core cancelled because the
actor moved, a selection or auto-attack the core ended) or `stock` for
anything no one here asked for, which is what a gate failure would look like:
an untriggered cast, a movement spline or motion generator the executor did
not start, a new selection, auto-attack or pet. Each record carries the episode
generation it was made in and is kept in a bounded buffer of 4096 that the
tooling drains into its run files with `dojo control trace since <seq>`; a gap
in sequence numbers is reported as lost records. Only ownership, generation
and neutralize records, dropped or refused commands, changed fingerprints and
stock actions are also logged to `Server.log` as `dojo-trace {json}` lines.

| Command (console or RA) | Does |
|---|---|
| `dojo control status` | ownership, epochs, the AI's skipped updates and refused inputs, the pending command, counters and each actor's state, as one `dojo-control {json}` line |
| `dojo control own <0\|1\|all>` / `release <...>` | acquire again, or hand an actor back to stock behaviour |
| `dojo control act <0\|1> <action>... [force]` | submit commands by name or index; a masked action is refused unless forced |
| `dojo control masks <0\|1> [all]` | the actor's knowledge, the reason per ability and facing, and with `all` the mask bits |
| `dojo control maskcheck <0\|1> <n>` | n mask queries, and whether either actor's fingerprint changed |
| `dojo control trace [n]` / `trace since <seq> [max]` / `actions` | the last trace records, or those after a sequence number; the catalog's canonical JSON |
| `dojo harness place\|mana\|restore\|los\|kill\|cooldowns\|disconnect` | acceptance set-up in the training arena, each traced as `harness`: placement, mana, the staging baseline, a line-of-sight probe, a death by the actor's own hand (no kill credit), clearing an actor's cooldowns and global cooldown (returns the time), and ending an actor's session as a dropped connection would |

`python -m leeroy_dojo realm --layout <layout.json> control-acceptance
--scenario <json>` runs the plan's Task 6 acceptance and writes its evidence to
`<data root>/runs/<stamp>-control-acceptance/`: owned idle actors produce
nothing; a released actor resumes stock behaviour and stops when re-owned;
mask queries change nothing; the global cooldown, insufficient mana, rooted
movement, line of sight, interruption, a superseded command and a target
moving behind a cast are refused or carried out by the core's rules with the
reason recorded; and the worldserver shuts down while both actors are owned,
its logs showing ownership held to the end. Fixture rows are compared before
and after and the live install is fingerprinted, as in the realm acceptance.

## Observations and episodes

### The actor observation

`leeroy-dojo-observation-v1` (`src/Lib/DojoPerception.*`, `leeroy_dojo.observation`)
is an ordered list of numbers, each a raw quantity divided by its field's
scale and clipped to the field's bounds, with a defined value for when what it
describes is missing. Both languages build the same schema from the action
catalog and hash its canonical JSON
(`modules/mod-dojo/tests/fixtures/observation_cases.json`); for
`frost_mirror_small_v1` it has 137 values, SHA-256
`81802928597a300914e6b0004747a07c7ebb07fa87884556cb8191287c9e41a0`. In order:

| Group | Fields |
|---|---|
| context | class one-hot (the ten WotLK classes), level, talent points per tree |
| match | the time left of the scenario's match limit |
| self | alive, health, mana, global cooldown, casting and which kit spell, cast time left and progress, a request in the core's spell queue; per kit spell known, cooldown, cost and in range; position in the arena frame, facing, distance to the edge, velocity, moving, run speed; rooted (and for how long), stunned, silenced, frozen, in combat, Fingers of Frost charges; the pending command (present, ability, moves, due) |
| enemy | present, visible, age since last seen; health, mana and alive as last seen; its position relative to the actor's own; while visible, whether it faces the actor, its velocity, its cast bar, its effects (rooted, stunned, frozen, slowed) and whether it targets the actor; per kit spell a cooldown inferred only from uses the actor saw, with an unknown flag |
| events | the four newest kit casts (started, completed, stopped) and deaths the actor saw, newest first: present, age, by self, kind, kit spell |

Each world tick, after the maps have updated, the runtime
(`src/Runtime/DojoObservation.*`) reads both actors' authoritative state with
getters only and gives each actor a percept: itself, the enemy only while it
is visible (in the map, detectable and in line of sight from eye height), and
the cast starts, casts and interruptions its own session was told of whose
caster it could see. The enemy's cooldowns, global cooldown, spell queue and
pending command never enter a percept; neither does where it is while out of
sight. A percept matures 100 ms after it was taken (the perceptual delay,
D30), and only matured percepts update the actor's memory (the enemy last
seen, its kit uses seen, recent events) and make its observation and mask
knowledge at a decision, so any change reaches an actor no sooner than 100 ms
later and a hidden one not at all. The delay is checked from timestamps, not
by waiting, so it holds unchanged on simulation time. The authoritative state
is kept as separate debug records and printed on separate console lines; it
is never part of an observation.

### Episodes and their reset

An episode is a generation. `dojo episode reset` starts the next one and
returns at once; the reset completes over later ticks. It first drops every
pending command and in-flight cast record of the old generation and holds both
actors' perception, so nothing of the old episode can execute, be traced to it
or be perceived in the new one; an action id from before the generation is
refused if it ever reaches the executor. The director then tears down
whatever is there (out of the arena, the dead revived by leaving, the staging
baseline restored, both logged out, the old instance closed with no outcome)
and reconstructs it: fresh logins (new `Player` and `PlayerbotAI` objects, so
no aura, diminishing return, threat, combat, spell event, projectile, motion or
AI cache survives), the staging baseline restored and the loadout verified, a
new unrated instance with one actor per side. After the stock preparation the
gates open; the episode then verifies the clean state of both actors (loadout,
full health and mana, no transient aura, cooldown, global cooldown,
diminishing return, combat, selection, auto-attack, movement, spell, spell
queue, pet or pending command, freshly owned, at its start and facing, on its
side), starts their perception, and becomes ready only once each actor has an
observation made under the delay and both still stand at their start. Until
then nothing reports the reset complete; a failure leaves the episode failed,
and the next reset starts from whatever is there. A running episode is aborted
(never scored) when an actor goes offline or loses ownership, perception
stops, or the core ends the arena; the scenario's match limit ends it.

| Command (console or RA) | Does |
|---|---|
| `dojo episode reset [reason]` | starts the next generation's reset; `dojo-episode-reset {json}` with its generation and token |
| `dojo episode status` / `evidence` | phase, generation, timings and counts; the last reset's clean-state evidence and both initial observations |
| `dojo episode observe [full]` / `decide <a0> <a1> [force]` | both actors' observations now (with the values and mask bits); one decision for both actors answering the same observations, masked actions refused unless forced |
| `dojo episode schema` / `debug [n]` | the observation schema and the actor context; the last n authoritative debug records |
| `dojo episode abort [reason]` / `fault <leave\|arena\|none>` | abort the episode (an abandoned reset's completion is then ignored); arm a one-shot teardown fault for the next reset |

`python -m leeroy_dojo realm --layout <layout.json> reset-acceptance
--scenario <json> [--resets N]` runs the plan's Task 7 acceptance and writes
`<data root>/runs/<stamp>-reset-acceptance/`: a reference reset straight after
a worldserver restart, then N consecutive resets from idle, mid-combat, a
death, a cast in progress, a root, a command inside its delay, a projectile
in flight, an actor's session ended, the console connection dropped mid-reset,
both teardown faults, an abort mid-reset and hidden changes (the actor's own
cooldown cleared, the enemy's cooldown cleared, the enemy moved out of sight),
then a second restart reference. Each completed reset must pass the worker's
checks, equal the reference in clean state and initial observations, equal the
manifest's own expectation, change no progression row and show no stock action
or old-generation record after it began. The run keeps actor observations
(`observations.jsonl`) and authoritative debug records (`debug.jsonl`) in
separate files, beside the drained trace, every reset's record and the
worker's logs.

On the controller's side `leeroy_dojo.memory.EpisodeMemory` holds every
actor's history and every policy's memory for one episode, clears them all
when a reset result arrives and refuses a result from another episode or an
older generation afterwards.

## Protocol

Version 1 is NDJSON on a loopback socket between one controller and one
worker. Each line is a JSON object of at most 256 KiB (UTF-8, without the
newline) with `"v": 1`, a `"type"` and a request `"id"` (1-64 characters of
`A-Za-z0-9._:-`). Unknown fields, more than 64 nested containers, NaN, a byte
order mark, invalid UTF-8 and an integer that no double can hold are refused;
such a number makes the whole line not JSON, so its id is not echoed. A line
of only spaces, tabs and carriage returns is skipped; anything else, a lone
vertical tab or form feed included, is decoded. Every request line gets
exactly one response line: `<type>_result` with `"ok": true`, or
`"type": "error"` with a `code` and a free-text `message` of at most 256
bytes (an echoed field name or worker reason is clipped).

| Request | Fields | Result |
|---|---|---|
| `hello` | `protocol {min,max}`, optional `client`, `expect {observation_schema, action_schema, scenario}` | `session`, `protocol`, `state`, `bounds`, `worker`, `scenario`, `schemas`, `actors`, `actions`, `clock`, `rng` |
| `reset` | `session`, `episode` (new), `decision` (0), `scenario_seed`, optional `scenario` | `episode`, `decision`, `generation`, `scenario`, `scenario_seed`, `sim_ms`, `observations` |
| `step` | `session`, `episode`, `decision`, `actions`: one `{actor, action}` per controlled actor | `decision`, `next_decision`, `generation`, `requested` (with `action_id`), `elapsed_ms`, `substeps`, `sim_ms`, `observations`, `events`, `rewards`, `terminated`, `truncated`, `outcome` |
| `status` | optional `session` | `state`, `session`, `episode`, `decision`, `generation`, `in_flight`, `draining`, `abort_reason` |
| `close` | `session`, optional `reason` | `session`; the worker then closes the connection |

The connection's session comes from `hello`, and reset, step and close must
carry it. The worker reports its scenario, observation and action schemas,
the actor slots and teams, the action catalog (`actions[0]` is always
`noop`), the clock (`substep_ms`, `decision_ms`, paced or not) and what its
RNG can promise (`scenario_seed`, `combat_replay`) separately. `observations`
is one `{actor, obs, mask}` per actor in slot order: `obs` is that actor's
permitted numbers and `mask` one 0 or 1 per catalog action, with noop always
open. A step must name a command, an explicit noop included, for every actor;
commands for one decision share the observation they answer. Each command's
`action_id` is `decision * actors + actor`, and every execution event that
results from it, possibly in a later step, carries that id. `rewards` is one
`{actor, terminal, shaping}` per actor. `outcome` is null while the episode
runs, else `{reason, winner}`: `death` (winner is a team),
`simultaneous_death` or `time_limit` (no winner) end it as `terminated`;
`external` is a known-valid cutoff and sets `truncated` instead.

Lifecycle: `booting -> ready -> resetting -> awaiting_action -> stepping ->
awaiting_action | terminal`, with `aborted` and `closing` exits. A reset is
accepted in `ready`, `awaiting_action`, `terminal` and `aborted`, never while
an abandoned operation is still draining. A step is accepted only in
`awaiting_action`. Disconnecting, closing, a worker fault, a failed reset or
an invalid or oversized result aborts the episode: the worker is told to tear
it down, and it is never delivered as a lost game. The next session starts a
fresh episode with a reset.

Exactly-once rules:

- One reset or step is in flight at a time; another gets `busy`.
- An exact duplicate (same id, same canonical payload) of the request in
  flight is answered when it completes, up to `max_copies` answers in all
  (the original included; the transport's inbox capacity, 16); a copy beyond
  that gets `busy` at once. A duplicate of a recent result is answered from
  the bounded cache, without touching the world. A different payload under a
  known id is `conflicting_duplicate`. A duplicate whose result has left the
  cache is validated afresh and fails as `wrong_step` or `stale_episode`.
- A request refused before it was applied leaves no trace; resending it
  after a fix is a new request.
- Each reset and abort starts a new generation. Completions and world
  callbacks carry the generation they began under and are ignored once it
  is old, so an abandoned operation cannot touch the next episode.
- Each `begin_reset` and `begin_step` effect carries a `token`, unique for the
  worker's lifetime, and a completion names the generation and token of the
  operation it finishes. Anything else is stale, so a late or repeated
  completion of decision N can never answer decision N+1, and only the
  abandoned operation's own completion ends a drain.
- Reconnecting never guesses which step was applied: the old session's
  episode is aborted.

| Error | Meaning |
|---|---|
| `malformed` | not JSON, not an object, a missing, unknown or ill-typed field |
| `line_too_long` | over the line bound; the connection is closed after it |
| `unsupported_version` | a protocol, schema or scenario the worker does not speak |
| `not_ready` | not allowed in the current state (no hello yet, booting, terminal, draining) |
| `bad_session` | not this connection's session |
| `busy` | another reset or step is in flight, or `max_copies` copies of this one already are |
| `stale_episode` | not the current episode, or an episode id used in the last 64 resets |
| `wrong_step` | not the expected decision index |
| `conflicting_duplicate` | a known request id with a different payload |
| `invalid_action` | a missing, repeated or unknown actor, or an unknown or masked action |
| `aborted` | the episode was aborted; `reason` is `disconnect`, `closed`, `fault`, `reset_failed` or `invalid_result` |
| `result_overflow` | the result would exceed a bound; the episode is aborted, never truncated |

Bounds, published in `hello_result`: 256 KiB per line, 2 controlled actors,
64 events per step result, 8 cached results, 64 remembered episode ids, one
request in flight and 16 answers owed to it (`max_copies`).

The worker adapter performs the state machine's effects: `begin_reset` and
`begin_step` (each with its generation and token), `abort`, `release` (the
session ended) and `close_connection`. Worker payloads are typed as strictly
as requests: an actor slot, mask bit or millisecond count is an integer,
never a bool or a float, and a result that cannot be encoded aborts the
episode. Both implementations run the golden cases in
`modules/mod-dojo/tests/fixtures/dojo_protocol.jsonl` (worker descriptions,
line framing, decoding and whole sessions); its expectations are written out
in `make_dojo_protocol.py` without importing either one.

## Transport

`Dojo::Transport` (C++, header-only Boost.Asio) and `leeroy_dojo.transport`
(Python, for the fake worker) are the worker's side of the socket. They bind
loopback only, port 0 for an ephemeral port that the worker records in its
process receipt. One controller at a time: another connection gets a `busy`
error line and is closed. The io thread only frames lines and decodes them
into pure data; the worker thread polls connect, line and disconnect events
and sends responses. Nothing is dropped to relieve pressure: while the worker
has not taken 16 lines, or 64 responses wait unwritten, reading stops and TCP
pushes back on the controller. Each connection has a serial, and a response
for a connection that has gone is dropped, since its session has ended.

## Fake worker, recorder and smoke

`leeroy_dojo.fake_worker` runs the transport, the lifecycle machine and
`fake_game.FakeDuel`, a deterministic two-actor duel that is not WoW but has
the contract's shape: 10 ms substeps in 200 ms decisions, a 150 ms command
delay, one pending command per actor (a new one supersedes it, noop keeps
it), a bolt whose damage lands after its cast with the action id that started
it, an instant lance, a global cooldown, rejections with a reason,
simultaneous death and time limits, the +1/-1/0 terminal reward with
optional potential shaping, a worker-lifetime clock that a reset does not
rewind, and seeded combat that replays exactly. Its records of what the world
did keep at most 100 000 entries each. `leeroy_dojo.client` is a blocking
controller that resends an identical line after a timeout (never more copies
than the worker's `max_copies`), discards the extra answers it is owed, and
raises `UnexpectedAnswer` for any answer it never asked for.

`leeroy_dojo.recording` writes an episode as `header.json` (scenario, seeds,
worker and schema versions, clock and RNG capabilities, bounds, policies),
chunks of up to 64 decision records each closed by a trailer with its
SHA-256 and a hash chain that starts from the header's SHA-256, and
`footer.json` with the outcome, the header's hash and every chunk's hash,
written last. A chunk is renamed into place only when complete. The episode's
directory is named after its id, which must be one safe path component (no
leading or trailing dot, no colon, no Windows device name). `verify_episode`
names a missing, unfinished, altered or foreign chunk, a replaced header, a
missing footer, a terminal record that is not the last, and a last record
whose flags do not match the footer's `terminated` or `truncated` outcome
(an aborted episode may end anywhere); `load_transitions` refuses an episode
that is incomplete or aborted.

```bash
.venv/Scripts/python -m leeroy_dojo protocol-smoke --fake [--out DIR] [--json]
.venv/Scripts/python -m leeroy_dojo verify-episode DIR/episodes/smoke-a
```

The smoke plays three recorded episodes against an in-process fake worker:
one with a step delivered twice and a result replayed from the cache, one cut
by dropping the connection right after a step was sent, and one on a new
session that must replay the first exactly. It checks that every decision
advanced the world once, that the old session is refused, that the cut
episode is aborted and excluded, and that a deleted final chunk and a crash
before the footer are both caught. It writes only under its output directory
(a new temporary directory by default) and exits 0 when every check passes.

## Development

The package is standard library only and targets Python 3.12, which current
PyTorch wheels support and which is already installed here for the voice
service; its environment is kept apart from the mind's and the voice
service's.

```bash
uv venv --python 3.12 dojo/.venv
uv pip install --python dojo/.venv/Scripts/python.exe -e "dojo[test]"
cd dojo && .venv/Scripts/python -m pytest -q
```

The pure C++ library builds without AzerothCore (Visual Studio 2022 or GCC);
the transport needs Boost's headers:

```bash
cmake -S modules/mod-dojo/tests -B build-dojo-tests -DDOJO_BOOST_INCLUDE_DIR=F:/azerothcore/boost_1_92_0
cmake --build build-dojo-tests --config RelWithDebInfo --parallel 4
build-dojo-tests/RelWithDebInfo/dojo_tests.exe
```

CI builds the same tests with GCC and `-DDOJO_WARNINGS_AS_ERRORS=ON`
(`-Wall -Wextra -Werror`; `/W4 /WX` with MSVC).

The tests run without AzerothCore, MySQL, Torch or a running server.
`test_module.py` checks the module's source tree statically: names, defaults,
the runtime's includes and the shared `json.hpp`. The preflight tests build a
synthetic install in a temporary directory and are
skipped when `pwsh` or `git` is missing; one of them runs the preflight with
only logging shims on `PATH` and listeners on every database and game port,
and another checks that nothing outside the data root changes. They link
directories with junctions on Windows and symlinks elsewhere, so CI (Linux)
covers the symlink paths only. After changing a golden case, run
`python modules/mod-dojo/tests/fixtures/make_dojo_protocol.py`; a test
fails when the committed fixture differs from its generator.
