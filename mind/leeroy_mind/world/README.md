# World personas

All switches default off. An invalid `[world]` table disables the extension.

```toml
[world]
enabled = false
realm = false
selfies = false
world_per_night = 12
world_selfies_per_night = 10
```

A native rebuild is required. `get_world_roster` and `world_say` both refuse
`realm_off` unless `Leeroy.Realm.Enable = 1` and `Leeroy.World.Enable = 1`.
World defaults to 0, and `.reload config` changes admission immediately. The mind
logs a refused roster ACK once per process, with a fixed code. Read polls carry
a separate request token and never reserve bridge command IDs permanently.

Native snapshots admit guilds only when every member has a verified random-bot
account. They exclude the configured guild, human GUID and entire auction-bot
accounts, with no character-level floor. Missing account evidence refuses a guild.
Snapshots are bounded, paged and committed only when complete and acknowledged.
They are observations, not activity: no chat line, game-event row or session clock.
Missing members retire without deleting their personas. Observations expire after
one hour; reconnecting refreshes them, as does the five-minute poll and the night.

Generation uses reflection, existing persona screens and private prompt overlays.
The nightly plan survives restarts; three separate failed nights suspend a target.
Rival guilds have priority, then rank within each guild. Cards state character level;
world members have no owner romance. The task 46 appearance sampler is used when
installed; its `LooksRefused` and a missing sampler both fall back to the existing
screened enrichment path without consuming a failed night.

The tier boundary is cast by default, including databases predating this feature.
Store cast/versions/cards, PersonaCache and persona loading enforce it centrally.
Direct SQL readers in console, tuning, claims, awareness, guards, memory callbacks,
fingerprints and Realm filter it too. Talk, guild life, raids, voice casting and cast
night stages retain those defaults. All-persona name blocking is intentionally
broader for privacy; historical migrations and photo file maintenance keep all rows.
Only this package and the explicit Realm integration opt in to world reads.
Existing native Realm actors keep their actor cards, moves, whispers and night
beats even when they have world personas; additional world speakers are separate.
A world bot present in an authoritative cast roster becomes eligible for the
newcomer stage only with `[personas] auto_newcomers = true`. With that setting off,
stop the mind and run `personas generate`; its plan and the doctor's missing-persona
note include these promoted members. Both paths use the stored world persona as
seed data for the normal generator and commit promotion only after normal screens
and the age floor pass, with source `generated`. A concurrent persona change or
cast removal wins over the pending generation. Promotion passes unchosen rendered
world selfie sets without deleting their files, allowing a first set from the new
cast looks. Any unrendered queued set uses the new cast persona when it renders.

Whispers and General moves select the first available actor in the rotation, never
an ordinary world speaker. A fresh night beat prefers an in-city actor for Trade;
otherwise Trade keeps the combined rotation. A guild with no actor skips actor-only
actions, while ordinary world members can still speak in Trade and General.

Realm world speech uses current online members of the correct active rival guild,
their voice block, existing screens and shared speech rations. Native validation
checks membership again before Trade or General speech. It never moves, pins or
creates actors, casts voices, sends whispers or speaks in our guild channels.
Forum authors and world-organised write-ups recheck membership after reflection.

World photos also require `[selfies] enabled = true`. Planning counters use an
owned read-only connection off the event loop; each photo pass snapshots the
member/persona set once, refreshing after local writes.

Photos use the existing clothed, adult, name-screened pipeline and image gate. A
separate image allowance runs after cast images. World faces display a ready gated
candidate without adding an owner choice. The World console filter is read-only,
defaults off and pages cards; the Realm tab also shows them. Both inspection commands
open the database read-only, without migrations, models, bridge or service locks:

```text
python -m leeroy_mind personas world status --data-dir PATH
python -m leeroy_mind personas world plan --data-dir PATH
```
