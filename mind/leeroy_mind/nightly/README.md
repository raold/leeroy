# GPU admission and requested photos

The arbiter keeps live leases in event order and serializes model administration under one
lock. Ordinary nightly image stages still require no live leases. Requested-photo borrowing
is attached only with `[photos] requests = true`, `send = true`, `[selfies] enabled = true`,
`snaps = true`, and `[server] enabled = true`; restart the mind after changing these switches.
See [the photo operator notes](../photos/README.md#requested-photos) for what the owner sees.

`GpuArbiter.borrow(holder)` allows the `server` and `discord` presence leases. It refuses
human, offline-raid, voice-GPU and other job leases, paused GPU use, or any night/booth pass
still running or cleaning up. Its temporary request lease prevents another image pass from
starting. The request service additionally requires confirmed offline game presence and
enforces its durable hourly/day limits at admission.

Borrow admission closes before draining active non-embedding model exchanges. The client
registers each exchange atomically with the open check, and cancellation joins its HTTP
thread before releasing admission bookkeeping. No later mind chat call can autoload a model
beside the image worker. Embeddings bypass this borrow gate and stay resident. Free GPU's
separate pause gate still rejects/cancels all model work, including embeddings.
Chat jobs waiting for borrow admission lend their scheduler slots, so embeddings still run
when every chat slot was occupied. Those jobs rejoin the normal priority queue before
starting an exchange; concurrent children share one slot admission. Voice pin/unpin also
fences its residency check and joins its HTTP thread before borrowing can unload models.

The caller registers each worker with `room.own_worker(worker)` before startup and uses
`room.make_room()` for room-making retries. A hard lease arriving mid-borrow requests
cancellation immediately, then waits behind worker teardown. The borrow owns stop and live
reload even when startup fails, rendering times out, or cancellation repeats. It never
releases that ownership merely because the render's 180-second work budget expired.

Live reload uses the current model revision and context size, pins it, then reopens chat.
A transient reload error gets one retry. If worker shutdown or reload remains unproved,
admission stays closed with an explicit failure; another image pass cannot start. The next
live admission attempts recovery before waiting on the gate, so it cannot deadlock waiting
for its own repair. Free GPU pause interrupts borrowing and suppresses live reload until
resume; shutdown drains any registered worker before closing shared resources.

`DeadlineCredit` counts overlapping closed-admission time once from a turn's enrollment.
Pipeline expiry checks and the scheduler share that token rather than independently adding
time to copied deadlines. The credit covers safe cleanup/reload as well as the bounded
image work, and does not extend unrelated queue waits. The off path attaches no borrow gate
or credit tokens and keeps the ordinary deadline behavior.

# Slang and `lexicon clean-junk`

The `slang` stage (`vocab.py`) promotes a word that three public speakers used within 14 days
of its first public use. The first night read the whole history at once, so every word then
looked new, and ordinary words ("you", "now", "the") became slang: each adopter got a
"Picked up 'X' from Y." memory, and the guard's gag ration then rewrote any draft holding the
word. A word is now slang only when `vocab.junk_reason` finds none of:

- `common`: an everyday word (`talk/everyday.py`): about 45,000 English words from SCOWL
  levels 10 to 40, capitalised ones lowercased (weekdays, months, holidays, nationalities, "tv";
  `talk/data/everyday_english.txt`, permissive licence and its sources' notices in
  `talk/data/SCOWL-Copyright.txt`), the typo step's `COMMON_WORDS`, and a short list of chat
  shorthand, apostrophe-less contractions and their stubs ("don" from "don't"), congratulations
  and stock MMO abbreviations (dps, dkp, afk, loot).
- `typo`: a slip of an everyday word: runs of three or more letters collapse into one
  ("reallly", "heyyyy"), or one inserted letter, one swap of neighbours or one dropped doubled
  letter turns it into one of 5 letters or more ("conrats", "grtas", "wehre", "withut").
  `GAMER_WORDS` keeps gamer slang and game shorthand that reads as such a slip ("pwned",
  "gief", "leet", "mage", "spec"); add to it when the night screens a real one.
- `cast_common`: the cast already says it in more than 1 public line in 200 (and more than 10).
- `not_distinct`: on a corpus of 500 public lines or more, its first speaker uses it less than
  twice as often per line as the cast does: they did not coin it.

Coined words and game slang stay possible ("zugzug", "gank", "kekw"). Each night logs
`slang_screened night= common= typo= cast_common= not_distinct=` (counts only, for the reasons
met). `spread` never revives or spreads a retired slang term or an everyday or typo one, and
the guard never rations a one-word everyday or typo slang term (a running joke it rations).

Stored junk from before the fix is retired by a data step, never deleted:

```bash
python -m leeroy_mind lexicon clean-junk --data-dir "$D"            # dry run: counts and a sample
python -m leeroy_mind lexicon clean-junk --data-dir "$D" --go       # mind stopped
python -m leeroy_mind lexicon clean-junk --data-dir "$D" --undo N --go
```

The dry run opens mind.db read-only and never migrates. `--go` refuses while the mind holds
`mind.lock`, holds it throughout, snapshots mind.db (`snapshots/mind-pre-clean-junk-*.db`)
before writing, then in one transaction sets each junk term `dormant` with `retired` set, its
adopters inactive and its holders' keyed copies archived (also early copies without a joke
tag), and writes receipt `lexicon:clean-junk:<n>` in meta with every prior value. A second
`--go` finds nothing. `--undo N --go` (also lock and snapshot first) restores what the receipt
changed where nothing changed it since: a term no longer retired by that receipt (vetoed, say)
keeps its adopters inactive and its copies archived, and a member already holding 8 active
terms (lexicon's adoption limit) gets no adoption back, nor its copy. Vocab rows keep
`slang = 1`, nicknames and jokes are untouched, and member nicknames an old junk term displaced
from their 8 adoption slots are not reactivated.
