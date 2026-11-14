# Raid playbooks (`leeroy_mind.raidplay`)

One playbook per raid boss, as data: what each boss does, what the raid does about it, which
spell ids announce it, whether playerbots' stock raid strategy already handles it, and which
training check scores it. One table for three consumers, so that what a member says, what
bodies do and what the training ground scores are the same thing:

- the raid lead's callouts (the stock pre-pull call per boss lives here too, see below);
- a native responder that can move bodies at cast start (a later PR);
- the training ground's per-mechanic scoring (`training/checks.py`, now).

Nothing here calls a model, opens `mind.db` or talks to the bridge.

## Coverage

| Raid | Map | Key | Difficulties | Bosses |
|---|---|---|---|---|
| Naxxramas | 533 | `naxx` | 10, 25 | all 15 (`BOSS_NAMES` 533:0-14) |
| Icecrown Citadel | 631 | `icc` | 10, 25, 10h, 25h | all 13 indexes (12 bosses plus Sister Svalna, 631:9) |
| Trial of the Crusader | 649 | `toc` | 10, 25, 10h, 25h | all 5 |

Ulduar (603) is not covered yet. A covered raid must have a playbook for every boss index
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
`enrage` (seconds), `phase` list, optional `call` and `lust` (the stock pre-pull call), and
`melee_behind = false` with `no_behind_since` for bosses whose melee are not scored for standing
behind the boss.

Per mechanic (`[[boss.mechanic]]`):

- `id`: `<raid>.<boss>.<name>`, unique, stable: later PRs and stored evidence refer to it.
- `kind`: one of `avoid_ground`, `move_out_cone`, `spread`, `stack`, `soak`, `run_out_debuff`,
  `interrupt`, `dispel`, `tank_swap_at_stacks`, `switch_adds`, `kill_order`, `los`, `face_away`,
  `positional`, `kite`, `raid_cooldowns`, `use_object`, `heal_target`, `enrage_timer`, `stop_casting`.
- `roles`: who it is for (`tank`, `healer`, `melee`, `ranged`); `phase` (a phase id or `all`).
- `spells`: what the encounter casts (what a responder watches for); `hits`: what lands on
  players when that differs (what scoring sees). Either a list for every difficulty or a table
  per difficulty; a missing heroic key falls back to the same size's normal value.
- `only`: the difficulties where it exists (default all of the raid's).
- Numbers, each a value or a per-difficulty table: `radius` and `range` (yards), `stacks`,
  `first`, `every`, `duration`, `cast` (seconds), `targets`.
- `handled_by`: `playerbots` when the stock strategy in `mod-playerbots/src/Ai/Raid/<Raid>/`
  already reacts to it (then `playerbots` names the stock action), else `none`. Playerbots has
  no Trial of the Crusader strategy.
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
pb.boss(533, 2).enrage                   # {"10": 360, "25": 360}
pb.covered_maps(), pb.bosses(), pb.check("icc.marrowgar.check_avoid_coldflame")
```

`raidplay.playbook_for(map_id, boss_index, difficulty)` and `raidplay.mechanic(id)` are the
module-level shortcuts. Everything returned is frozen; mappings are read-only.

## Training

`training/mechanics.py` builds its `Mechanic` rows from the boss checks. By default it uses only
the `since = 1` rows and the original no-behind bosses (gunship, Valithria, Faction Champions),
so scoring is byte-for-byte what it was (`tests/raidplay/test_training_parity.py`).

`[training] playbooks = true` in `mind.toml` (default false, read at mind start; also by
`training rescore` and `training spells check`) scores every check of every covered raid
(checks version 2: the Naxxramas rows and the new no-behind bosses Heigan, Thaddius, Gothik and
the Four Horsemen). Stored pulls checked under version 1 are rechecked once at the next mind
start. Today's checker turns `avoid` and `adds` rows into coverage notes, not failures; the new
rows that can fail are Gluth's Mortal Wound stacks on tanks and the Four Horsemen's mark stacks.

## Raid lead calls

`guild/boss_calls.py`'s `BOSS_CALLS` is built from each boss's `call` and `lust`; `call_for` is
unchanged. The 18 ICC and Trial of the Crusader calls moved here verbatim; Naxxramas bosses have
no call yet, so they keep the generic one.

## Commands

From `mind/` with `--data-dir "$D"`:

```bash
python -m leeroy_mind raidplay show naxx                 # a raid; also show 631 11 --difficulty 3, --json
python -m leeroy_mind raidplay check                     # spell ids against Spell.dbc, coverage, references
python -m leeroy_mind raidplay check --notes             # also notes (and the DBM cross-check if present)
python -m leeroy_mind raidplay dbm-extract --addons F:/azerothcore/Wow355a/Interface/AddOns
```

`check` reads only `Spell.dbc` from the worldserver's `DataDir` (`--dbc DIR` to name it,
`--no-book` to skip it); never the world database. Exit 0 clean, 1 errors, 2 no spell book.

## DBM facts (local only)

`dbm-extract` reads the installed Deadly Boss Mods Lua files (never runs them) and writes numeric
facts only (creature ids, spell ids, the kind of each warning or timer, timer seconds) to
`<data-dir>/raidplay/dbm.json`. It refuses an output path inside the git checkout other than
the data dir. Nothing from DBM is ever committed. When that file exists, `check --notes` lists
spell ids DBM watches that no mechanic names and berserk timers that differ from the scripts.
They are notes for a person to look at; the scripts win where they disagree.

Logs carry counts only.
