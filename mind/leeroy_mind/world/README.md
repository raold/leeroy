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
installed; otherwise the existing enrichment screen supplies looks.

The tier boundary is cast by default, including databases predating this feature.
Store cast/versions/cards, PersonaCache and persona loading enforce it centrally.
Direct SQL readers in console, tuning, claims, awareness, guards, memory callbacks,
fingerprints and Realm filter it too. Talk, guild life, raids, voice casting and cast
night stages retain those defaults. All-persona name blocking is intentionally
broader for privacy; historical migrations and photo file maintenance keep all rows.
Only this package and the explicit Realm integration opt in to world reads.

Realm world speech uses current online members of the correct active rival guild,
their voice block, existing screens and shared speech rations. Native validation
checks membership again before Trade or General speech. It never moves, pins or
creates actors, casts voices, sends whispers or speaks in our guild channels.
Forum authors and world-organised write-ups recheck membership after reflection.

Photos use the existing clothed, adult, name-screened pipeline and image gate. A
separate image allowance runs after cast images. World faces display a ready gated
candidate without adding an owner choice. The World console filter is read-only,
defaults off and pages cards; the Realm tab also shows them. Both inspection commands
open the database read-only, without migrations, models, bridge or service locks:

```text
python -m leeroy_mind personas world status --data-dir PATH
python -m leeroy_mind personas world plan --data-dir PATH
```
