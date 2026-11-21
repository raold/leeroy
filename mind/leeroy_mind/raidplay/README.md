# Raid playbooks (`leeroy_mind.raidplay`)

One playbook per raid boss, as data: what each boss does, what the raid does about it, which
spell ids announce it, whether playerbots' stock raid strategy already handles it, and which
training check scores it. One table for three consumers, so that what a member says, what
bodies do and what the training ground scores are the same thing:

- the raid lead's callouts (the stock pre-pull call per boss lives here too, see below);
- the native responder that moves bodies at cast start (`[raidplay] respond`, see The responder);
- the training ground's per-mechanic scoring (`training/checks.py`, now).

Nothing here calls a model, opens `mind.db` or talks to the bridge.

## Interactive raid mechanics

The local playerbots patch adds the `toc` instance strategy for map 649. Its
`toc twin essence` action is gated by **`Playerbots.TwinValkyrInteractions = 0`**
in the playerbots configuration template. It defaults off. With the setting on
and instance strategies enabled, bots approach the real essence NPC and use its
gossip within four yards. They take the opposite colour to their current twin
target for damage, switch to the vortex colour through its cast and five-second
pulse aura, and remove heroic Touch with the matching essence. Missing NPCs,
dead bots, another map, an ended encounter, vehicles, transports, teleports and
explicit stay/passive commands prevent the action. It never grants an aura,
kill, loot, achievement or encounter state directly.

Valithria keeps the existing `icc` strategy and real spell/portal actions. The
patch fixes their healer allocation, reachable spell selection and movement:
one surface healer remains (two with four or more healers), while the others
heal the dragon and use available portals. A lone surviving healer stays out of
portals and yields dragon healing when a nearby party member is below 60 percent
health. Only living healers in the same instance count; dream-phase healers
keep their assignment. Healing uses known, currently castable spell ranks and
falls back from an unavailable talent to a basic heal. A healer already moving
to its portal keeps that action, and a portal reserved by someone else no
longer blocks healing. Existing dream-cloud collection is unchanged.

These changes require applying the playerbots patch and rebuilding native code
at a release; publishing the playbook does not activate the Twin action. Check
a real pull before calling either encounter verified: essence aura and vortex
survival for Twins; dragon health rising, a surface healer, real portal entries,
cloud buffs and a legitimate completion for Valithria. Automatic checks cover
decision cases and compile the adapters; they do not establish a live clear.

## Coverage

| Raid | Map | Key | Difficulties | Bosses |
|---|---|---|---|---|
| Naxxramas | 533 | `naxx` | 10, 25 | all 15 (`BOSS_NAMES` 533:0-14) |
| Icecrown Citadel | 631 | `icc` | 10, 25, 10h, 25h | all 13 indexes (12 bosses plus Sister Svalna, 631:9) |
| Trial of the Crusader | 649 | `toc` | 10, 25, 10h, 25h | all 5 |
| Ruby Sanctum | 724 | `rs` | 10, 25, 10h, 25h | all 4 (`BOSS_NAMES` 724:0-2 and Halion at 6; 3-5 are his intro stages) |
| Ulduar | 603 | `uld` | 10, 25 | all 14 (the Flame Leviathan .. Algalon) |
| Eye of Eternity | 616 | `eoe` | 10, 25 | Malygos |

Not covered yet: the Obsidian Sanctum (615), the Vault of Archavon (624) and Onyxia's Lair (249). Ulduar has
no heroic difficulty: each hard mode is a boss phase `hard_mode` named after what starts it, and the mechanics
only it brings take `phase = "hard_mode"`. Ulduar's client `SpellDifficulty.dbc` rows are empty, so its 25-man ids
come from the world database's `spelldifficulty_dbc`. A covered raid must have a playbook for every boss index
`wow.BOSS_NAMES` lists as a fight (`raidplay check` fails otherwise).

## The data

`data/<key>.toml`, one file per raid, keyed by map id and the instance script's boss index (the
training ground's `MAP:BOSS:DIFFICULTY` contexts; difficulty 0 10-man, 1 25-man, 2 10 heroic,
3 25 heroic, written `"10"`, `"25"`, `"10h"`, `"25h"` in the files).

Sources, in order: this server's own boss scripts (`azerothcore-wotlk/src/server/scripts/...`)
are ground truth for spell ids, timers, phases and difficulty differences; widely known tactics
are written in our own words. Per-difficulty spell ids follow the core's spell difficulty table
(`SpellDifficulty.dbc` plus `spelldifficulty_dbc`). DBM is never a source for the repo (its
licence does not fit); see the DBM facts below.

Per boss: `index`, `name` (must equal `wow.BOSS_NAMES`), `slug`, `creatures`, `script`,
`enrage` (seconds), `phase` list (each with an optional `hp_below` and an `after` in seconds, a value
or a per-difficulty table), optional `call` and `lust` (the stock pre-pull call) and `heroic_call`
(the call on heroic when it must differ), and
`melee_behind = false` with `no_behind_since` for bosses whose melee are not scored for standing
behind the boss.

Per mechanic (`[[boss.mechanic]]`):

- `id`: `<raid>.<boss>.<name>`, unique, stable: later PRs and stored evidence refer to it.
- `kind`: one of `avoid_ground`, `move_out_cone`, `spread`, `stack`, `soak`, `run_out_debuff`,
  `interrupt`, `dispel`, `tank_swap_at_stacks`, `switch_adds`, `kill_order`, `los`, `face_away`,
  `positional`, `kite`, `raid_cooldowns`, `use_object`, `heal_target`, `enrage_timer`, `stop_casting`.
- `roles`: who it is for (`tank`, `healer`, `melee`, `ranged`); `phase`: `all`, one phase id or
  a list of them.
- `spells`: what the encounter casts (what a responder watches for at cast start); `hits`: what
  lands on players when that differs (the damage or debuff, never a summon or the caster's own
  aura; what scoring sees). Either a list for every difficulty or a table per difficulty; a
  missing heroic key falls back to the same size's normal value. `spells` may be empty only
  for a `positional` or `kill_order` rule or when `adds` names the creatures.
- `adds`: creature entries of the adds it is about, with their difficulty entries.
- `only`: the difficulties where it exists (default all of the raid's).
- Numbers, each a value or a per-difficulty table: `radius` and `range` (yards), `stacks`,
  `first`, `every`, `duration`, `cast` (seconds), `targets`. Every body-moving kind
  (`avoid_ground`, `move_out_cone`, `spread`, `stack`, `soak`, `run_out_debuff`, `los`) must give a
  `radius` or `range` for each of its difficulties; they come from Spell.dbc's effect radius (with
  the core's `SpellInfoCorrections`) or, where the script sets it, the script.
- `handled_by`: what stock playerbots already does about it, which is the responder's gate:
  - `playerbots`: the raid strategy in `mod-playerbots/src/Ai/Raid/<Raid>/` (or a stock strategy
    every such bot has, such as `behind` for melee damage dealers) handles it for the roles named;
  - `partial`: only for some roles or under some conditions, said in `handled_note` (for example
    the generic `avoid aoe`, which steps out after an effect lands and only for bots with a
    real-player master, so never in all-bot offline raids; class interrupts and cures, which need
    that class in the raid);
  - `none`: nothing in stock playerbots does it.
  `playerbots` lists the registered stock action or strategy names; `raidplay check
  --playerbots-src` proves each is registered. Playerbots has no Trial of the Crusader strategy.
  Its Ruby Sanctum one (`rs`, `src/Ai/Raid/RS`) is scripted to fixed spots: the raid shuttles between two spots
  at each meteor, combustion and consumption are carried to fixed spots and ended there, and its bots get auras
  the encounter does not give (its tanks Pain Suppression, everyone a barrier through each breath, and on heroic
  through each consumption), so what bots survive there says little about the playbook. Its Ulduar strategy
  (`uld`) reaches Yogg-Saron and has nothing for Algalon; with the raid cheat on several of its actions teleport
  bots, strip debuffs or kill adds (Kologarn's eye beams and Crunch Armor, Mimiron's mines and bombs, Hodir's
  Biting Cold, Yogg-Saron's clouds, Crushers, illusions and portals), said beside each mechanic. Its Eye of
  Eternity one (`wotlk-eoe`) has fixed spots, targets and the drakes' abilities, no cheats.
- The responder's reading (`respond.py`, see The responder): `area`, where the danger lies when the spell
  starts, for the kinds it responds to: `avoid_ground` `dest` (default: round the cast's destination, else its
  target), `caster` (round the caster, following it), `line` (a band `radius` either side and `range` long from
  the caster toward its target, else its facing) or `ring` (a ring `range` from the destination, `radius` either
  side; both numbers required for a line or a ring); `move_out_cone` `cone` (with `angle`, the whole cone in
  degrees, as `spell_cone` gives it, else the core's 60); `spread` `group`; `stack` `dest`; `run_out_debuff`
  `target`; `los` `caster`; `nearest` for a `stack` or `avoid_ground` round whichever live creature of its
  `anchors` (creature entries, at most 8, required with it) stands nearest each bot within 40 yards, gathering in
  it or leaving it, and binding the bot whatever phase it is in (Algalon's Black Holes hide a player from Big Bang
  by phasing it); and `none` for any of them when the cast start does not say where or the cast has hit
  before any hook can run (the responder leaves it). `landing = true` (a `dest` or `ring` area) waits for the cast
  to complete and reads the place then (a random offset from the target). A `partial` mechanic of those kinds
  names `respond_roles`, the roles its `handled_note` says stock leaves (`[]` when moving them would fight stock;
  without it the responder leaves the mechanic). `respond_spells` (a list or per-difficulty table, among its
  `spells`; not on a `playerbots` mechanic) names the casts the responder answers: the ones stock leaves, or the
  ones that can be answered before they hit (Impale's 10-man cast; the 25-man one is instant).
- `explain`: one line for the raid lead, in our own words.
- `check`: the training check it maps to: a boss check id, a generic check (`behind`, `aggro`,
  `tank_range`, `healer_oom`) or `none`.

Boss checks (`[[boss.check]]`, ids `<raid>.<boss>.check_<name>`) carry the fields of
`training.mechanics.Mechanic` plus `since`: 1 for the 35 rows the training ground scored before
the playbooks (ICC and Trial of the Crusader, unchanged), 2 for the playbooks' new rows.

## Python API

```python
from leeroy_mind import raidplay

pb = raidplay.load()                     # Playbooks, parsed once per process
book = pb.playbook_for(631, 11, 3)       # BossPlaybook for 25 heroic Sindragosa, or None
book.mechanics                           # only the mechanics that exist at that difficulty
m = raidplay.mechanic("icc.sindragosa.frost_bomb")
m.spell_ids("25h"), m.hit_ids("25h"), m.number("every", "25"), m.kind, m.roles, m.handled_by
m.area, m.responder_roles(), m.responder_spells("25h")   # what the native responder acts on
pb.boss(533, 2).enrage                   # {"10": 360, "25": 360}
pb.covered_maps(), pb.bosses(), pb.check("icc.marrowgar.check_avoid_coldflame")
raidplay.mechanics_for_spell(71055)      # every mechanic casting or landing that id, any difficulty
```

`raidplay.playbook_for(map_id, boss_index, difficulty)`, `raidplay.mechanic(id)` and
`raidplay.mechanics_for_spell(spell_id)` are the module-level shortcuts (the last is a
responder's lookup from a cast start: check the mechanic's `spell_ids(difficulty)` for the
instance's difficulty). Everything returned is frozen; mappings are read-only.

## The responder

`[raidplay] respond = true` in `mind.toml` (default false; read at mind start; an invalid table, or one with keys
other than `respond`, means off) sends native the playbooks' actionable mechanics; native's
`Leeroy.Mechanics.Enable = 1` (`.reload config`) then moves the bodies at cast start
(`modules/mod-leeroy/README.md`, "Boss mechanics at cast start"). It needs `[training] playbooks = true`: the
training ground scores the playbooks' checks that measure it (log `raidplay_respond_off reason=needs_playbooks`
otherwise; `invalid` and `playbook_error` likewise). Restart the mind after changing it. Off, nothing is attached:
no handler, no task, no command, and native has no table, so it moves nobody (exactly today's behaviour).

`respond.actionable(playbooks, map, difficulty)` is the table for one instance: each mechanic of a responded kind
(`avoid_ground`, `move_out_cone`, `spread`, `stack`, `run_out_debuff`, `los`; `soak` is later work) with an
`area` other than `none` and roles stock leaves (`responder_roles()`: every role of a `none` mechanic, the
`respond_roles` of a `partial` one, none of a `playerbots` one), with its casts at that difficulty
(`responder_spells`), `radius`, `range`, `angle` (cones default to 60), `duration` (the playbook's hazard time,
else native takes the spell's own, a spread's ending with its cast) and `landing`. It is bounded to native's
limits (64 rules, 16 spells, 100 yards, 120 seconds), and each table fits one bridge line many times over. Today
43 mechanics qualify (106 rules across the 18 covered raid difficulties): Impale (10-man), Void Zone, Meteor,
Holy Wrath, Detonate Mana, Coldflame, Death and Decay, Shock Vortex, Fire Bomb, Legion Flame, Infernal
Eruption, Fel Lightning, Bladestorm, Hellfire, Halion's Meteor Strike, its burning crater and, on heroic
25-man, the Living Inferno's Blazing Aura; in Ulduar 24 (Algalon's Big Bang and the step out after it, Cosmic
Smash, Vezax's Shadow Crash, Yogg-Saron's Death Rays, XT-002's Searing Light and Gravity Bomb, the council's
Rune of Death, Overload and Static Disruption, Ignis's Scorch and scorched ground, Razorscale's Devouring Flame
and Flame Breath, Hodir's icicles, Thorim's charged orbs, Rune Detonation and Sif's Frost Nova and Blizzard,
Freya's Nature's Fury, Tidal Wave and Unstable Sun Beam, Mimiron's Proximity Mines) and Malygos's Arcane Breath
and Arcane Bomb. Rules with a `nearest` area go only to a worldserver whose hello also lists
`mechanics_nearest` (an older one gets the rest: `raidplay_respond_nearest_unsupported`).

`MechanicFeed` sends every covered map and difficulty as one `raid_mechanics` command at each hello from a
worldserver whose hello lists the `mechanics` feature (an older one gets nothing: `raidplay_respond_unsupported`),
and an instance's table again when his group enters it (the `guild_group` `entered` event, once per instance id
and connection). Native keeps the tables for the connection that sent them, so offline raids and his called raid
nights are covered from hello on. Logs: `raidplay_respond_on tables= mechanics=`, `raidplay_respond_sent map=
difficulty= mechanics=`, `raidplay_respond_refused map= difficulty= error=<code>`,
`raidplay_respond_transport`. The Ops tab's Switches lists it (`training.playbooks` and
`Leeroy.Mechanics.Enable = 1` as its needs). `raidplay show` adds a `responder:` line to each mechanic it acts on.

Which mechanics the data leaves (`area = "none"` or `respond_roles = []`), and why, is said in a comment beside
each in `data/*.toml`: Rotface's Slime Spray and Sticky Ooze, Loatheb's spores and the heroic Lich King's Shadow
Trap (stock moves those bodies itself, often with forced moves), Grobbulus's Slime Spray, Icehowl's Arctic Breath
and the 25-man Impale (instant: they hit before any hook runs), Putricide's Choking Gas Bomb, the gunship's
Rocket Artillery (the deck is a transport), the Beasts' Slime Pool, Paralytic Toxin and Trample, and the twins'
orbs (no place known at cast start). The numbers follow what the core hits, from `Spell.dbc`, `spell_cone` and
the scripts: Coldflame is answered for the targeted cast only (`respond_spells = [69140]`; Bone Storm's four
lines are not one area), along a 70-yard line 5 yards either side (its patches' radius) for the 9 seconds the
trail burns; Fire Bomb by its 8-yard impact (the patch it leaves is a ground hazard); Shock Vortex for the
ordinary one only (stock dodges the empowered one), where it lands, 12 yards round for its 30 seconds; Legion
Flame's run-out lasts its 6-second trail, 15 yards from everyone; Infernal Eruption is a ring 15 yards from the
volcano, 10 either side; Detonate Mana binds every mana user's role.

Ulduar (`data/uld.toml`) and the Eye of Eternity (`data/eoe.toml`): Algalon's Big Bang is answered at its
8-second cast by gathering every healer and damage dealer within the 6 yards of the nearest Black Hole (a Worm
Hole below 20 percent), whose phase hides them from it; tanks stay out, since a Big Bang that finds nobody is
followed by Ascend to the Heavens, and at the landing (64445) all step out again (inside, the hole keeps
burning and Algalon cannot be reached). Left out on purpose: vehicle fights (the Flame Leviathan, Malygos's
drakes: native never moves a bot on a vehicle), mechanics with dozens of emitters each ticking its own
mechanic (XT-002's void zones, Auriaya's pools, Mimiron's hard-mode flames: the hazard scan measures them),
those stock's forced positioning would undo (Mimiron's Frost Bomb, Rapid Burst, Laser Barrage), and turning or
moving beams (Lightning Charge, the eye beams, Thorim's Runic Smash lanes).

Ruby Sanctum (`data/rs.toml`): stock's `rs` strategy handles almost everything with fixed spots, so only Halion's
fire reaches native. Meteor Strike is answered at its cast (74637, which Halion casts at a player as the server-side
74638 hits), 12 yards round where that player stood, for the 7 seconds until the mark's impact; tanks are left to
stock's two tank spots. The crater is answered at the impact itself (74648 and its difficulty versions, cast by the
mark): 3 yards round the mark for the 24 seconds it still burns, so a bot stock moved with a forced move during the
meteor's run (the responder yields to those for the rest of that mechanic) is bound again once the crater burns.
On heroic 25-man each Living Inferno's Blazing Aura is answered at every 2-second tick (75887, the 25-man
version of 75886), 15 yards round the inferno as it walks, each tick a fresh mechanic for 3 seconds: a forced
move of stock's binds the bot only until the next tick, and tanks, who hold the inferno, are never moved. Native
binds a mechanic only to bots in its caster's phase (Halion's two realms share the room), keeps the inferno in
its hazard scan for everyone but tanks, and logs a tick's end only when it moved someone. Left to stock: Tail
Lash (a rear cone; native's cones point forward), the breaths, the Twilight Cutter (a turning beam), Fiery
Combustion and Soul Consumption with the fire they leave (nowhere known at a cast start; the hazard scan
measures it), Enervating Brand, Conflagration, the adds.

## Training

`training/mechanics.py` builds its `Mechanic` rows from the boss checks. By default it uses only
the `since = 1` rows and the original no-behind bosses (gunship, Valithria, Faction Champions),
so scoring is byte-for-byte what it was (`tests/raidplay/test_training_parity.py`).

`[training] playbooks = true` in `mind.toml` (default false, read at mind start; also by
`training rescore` and `training spells check`) scores every check of every covered raid
(checks version 2: the Naxxramas rows and the new no-behind bosses Heigan, Thaddius, Gothik and
the Four Horsemen). Stored pulls checked under version 1 are rechecked once at the next mind
start.

What it changes today: nothing in any stored or new score. The checker turns `avoid` and `adds`
rows into coverage notes, not failures. The new rows that can record a failure (Gluth's Mortal
Wound stacks on tanks, the Four Horsemen's mark stacks) write `tg_failure` rows, but both bosses
also have an `avoid` or `adds` row, so their pulls never count as fully checked and the score
ignores failed mechanics for them (`training/score.py`). The switch is groundwork for when avoid
damage is measured; ICC and Trial of the Crusader rows are unchanged either way. The version 1
avoid rows keep their original names for parity, and two of them name the cast rather than the
hit (Putricide's "Choking Gas Bomb", Lana'thel's "Bloodbolt Whirl"). The score's checker never
resolves an avoid row's names, but the training ground's "what hurt them" report
(`training/hurt.py`) does, so those two rows' avoidable-hit counts stay as they were on `main`;
the mechanics' `hits` carry the ids that land.

## Raid lead calls

`guild/boss_calls.py`'s `BOSS_CALLS` is read from each boss's `call` and `lust` on first use;
`call_for(map, name, difficulty=None)` picks a boss's `heroic_call` (kept in `HEROIC_CALLS`)
when a caller names a heroic difficulty (2 or 3). The 18 ICC and Trial of the Crusader calls moved
here verbatim, except Lord Jaraxxus (ruling, 2026-11-14): his volcano and portal cannot be targeted
on normal, so his call is now "jaraxxus: interrupt, dodge eruptions, kill the mistress" and
the old "kill portals and volcanoes" is his heroic call. Callers that pass no difficulty (today's
raid lead and stock cards) get the normal call, which is safe on heroic too;
Naxxramas bosses have no call yet, so they keep the generic one. If the playbook data cannot be
read, `BOSS_CALLS` falls back to those 18 calls as they were (log `boss_calls_fallback`, once)
so guild life never fails with it; a test keeps the two equal.

With `[raidlead] briefs` on, the raid lead's boss briefs (`raidlead/briefs.py`, see
`raidlead/README.md`) are built from these playbooks: the 2-4 mechanics that matter most to his
group at the difficulty, each said with its `explain` line word for word, in place of the stock
call before the pull and when he asks for the plan. An `explain` is therefore said in raid chat as
written: keep each one a whole sentence that stands on its own.

## Commands

From `mind/` with `--data-dir "$D"`:

```bash
python -m leeroy_mind raidplay show naxx                 # a raid; also show 631 11 --difficulty 3, --json
python -m leeroy_mind raidplay check                     # spell ids against Spell.dbc, coverage, references
python -m leeroy_mind raidplay check --notes             # also notes (and the DBM cross-check if present)
python -m leeroy_mind raidplay dbm-extract --addons F:/azerothcore/Wow355a/Interface/AddOns
```

`check` reads only `Spell.dbc` from the worldserver's `DataDir` (`--dbc DIR` to name it,
`--no-book` to skip it); never the world database. `--playerbots-src
F:/azerothcore/azerothcore-wotlk/modules/mod-playerbots/src` also checks that every stock action
or strategy named is registered there. Exit 0 clean, 1 errors, 2 no spell book.

## DBM facts (local only)

`dbm-extract` reads the installed Deadly Boss Mods Lua files (never runs them) and writes numeric
facts only (creature ids, spell ids, the kind of each warning or timer, timer seconds) to
`<data-dir>/raidplay/dbm.json`. It refuses any output that git would track (the live `mind-data`
sits inside the checkout but is ignored) and a data dir that is or holds the checkout. Nothing
from DBM is ever committed: its test fixture is made up, with invented ids and numbers. When that file exists, `check --notes` lists
spell ids DBM watches that no mechanic names and berserk timers that differ from the scripts.
They are notes for a person to look at; the scripts win where they disagree.

Logs carry counts only.
