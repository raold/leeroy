# Outreach

Members keep one durable list of things they mean to tell him. Existing whisper,
game opener and DM clocks select from it; delivery in one medium spends the item
for every medium. An empty eligible list uses the existing topic selection, except
that a tracked life/plan source or exact boss event cannot return through its
legacy fallback. Normal prompt knowledge still retains the event, and a newer
kill of the same boss has a distinct receipt and remains eligible.

## Operator notes

Enable with the following in `mind.toml`, then restart the mind and console:

```toml
[outreach]
enabled = true
```

The default is off. An invalid table disables the feature. Configuration is read
at startup only; disabled outreach installs no hooks or night stage and preserves
the existing topic choices and prompts. Migration `0045_outreach.sql` adds the
durable list and delivery receipts.

Chattiness, talkative and forward still govern the existing clocks. Chattiness
zero leaves the list filling but prevents delivery. A member who owes him a
deferred DM answer cannot start outreach. Outreach never advances a game clock
or counts Discord activity as game activity.

Member photos (`[photos] send`) keep their rate. A DM opener is offered her photo
first, exactly as with outreach off; when a photo goes out on it, the opener's
item is not attached and stays open for a later turn. An answer to his DM carries
no item and keeps its photo offer as before.

The console shows counts by kind and state under **Means to tell you**, and a
ready count on Tonight. It deliberately omits the things members intend to say.
Logs contain member IDs, kinds, delivery media and ending codes, never item text.

## Sources and lifetime

The deterministic `outreach` night stage follows `human_world`, after `life` and
follow-up creation. Each member has a guarded transaction and checkpoint; one
member's failure does not prevent later members from filling their lists. No model
is called by the filler.

| Kind | Source | Audience | Weight | Lifetime |
| --- | --- | --- | --- | --- |
| `life` | Her last day's beats, backed by her active offscreen memory | Pair | 3 | 2 days |
| `plan` | Her live plan claims with valid source provenance | Pair | 2 | Claim expiry, at most 7 days |
| `followup` | Her active `followup:` memories, deduped by original human line | Pair | 4 | 3 days |
| `his_day` | His kill or loot with recorded witnesses including her | Guild | 2 | 1 day |
| `birthday` | Her card birthday on her regional date | Guild | 5 | Her next local midnight |
| `miss_you` | Woman aged 25+, drawn/lust, quiet with him in whispers and DMs for 2 days | Pair | Romance lift × forward, bounded 0.1–10 | 1 day |
| `dm_nudge` | A witnessed login and her newest unanswered ordinary DM under 3 days old | Pair, whisper only | 6 | That login; ready after 2–10 minutes |

Native `LootWon` receipts do not contain a witness list. For those receipts,
outreach uses the latest preceding `Group` event by timestamp and ID in the same
non-null game session, requiring both him and her in its captured membership.
Changed, empty, malformed, absent or unrelated-session groups cannot supply
witnesses. An explicit receipt membership list is checked directly. Current
world membership never supplies historical witnesses. Item names come from the
cached library or screened receipt text, with a generic fallback; boss names come
from the repository catalogue. This accommodates the existing native schema
without changing its protocol.

## Privacy and delivery

There are at most five open items per member; the lowest weight is dropped past
the cap. A member/source pair is inserted once, including after it has ended.
Sources are rechecked before use and delivery. Superseded memories or claims,
deleted Discord posts, unavailable sources and changed scope end eligibility.
Follow-up and plan checks walk the complete bounded provenance graph, including
memories recalled in a member's source line. Missing, unknown or cyclic roots
fail closed. Officer material cannot become pair outreach. Group sources retain
their original readers and must permit the destination pair.

Pair items can only reach that member's whisper or ordinary DM with him. Guild
items may additionally reach a guild or party opener to him, which stays a typed
line on that channel even while she sits in his voice room: voice routing never
moves it into the room, where its receipt could not name it. A nudge never becomes
a DM. A later owner post, including a tombstone, prevents an earlier DM from
becoming an unanswered nudge again.
The nudge's exact verified DM can appear in the labelled pair-history block even
when it is older than that block's normal one-day window; row and token limits
remain unchanged, and unrelated old DMs are not brought in with it.

One turn carries at most one item, and its words appear once in the prompt, in the
item's own instruction line; the turn's recall still searches for them. A photo
offered to a DM opener takes the turn instead, and the item is left open. Its
delivered chat line and `told` receipt share
a transaction; a refused or dropped draft leaves the item open. Receipt checks
exclude only the line being recorded and its linked Discord message from the
`miss_you` silence check, preserving intervening contact in either medium.
Existing source vectors can identify a topic he raises first; named item matches
also end it as `he_raised`. Matching uses the configured embedding service, caches
bounded topic vectors when a source has none, and rechecks source and owner-line
validity after waiting. While matching runs, her spontaneous outreach waits; her
ordinary answers can continue. Ended items never return through either list
selection or these legacy fallbacks.
