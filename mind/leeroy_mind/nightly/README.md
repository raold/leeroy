# GPU admission and requested photos

The arbiter keeps live leases in event order and serializes model administration under one
lock. Ordinary nightly image stages still require no live leases. Requested-photo borrowing
is attached only with `[photos] requests = true`, `send = true`, `[selfies] enabled = true`,
`snaps = true`, and `[server] enabled = true`; restart the mind after changing these switches.
See [the photo operator notes](../photos/README.md#requested-photos) for what the owner sees.

`GpuArbiter.borrow(holder)` allows the `server` and `discord` presence leases. It refuses
human, offline-raid, voice-GPU and other job leases, paused GPU use, or any night/booth pass
or Render selfies run (`selfies/manual.py`) still running or cleaning up. Its temporary request lease prevents another image pass from
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

## Relationship notes (`personas clean-notes`, 2026-11-16)

The `notes` stage had already written sentences from those copies into `relationship.notes`
("I picked up '<word>' and '<word>' from him"; "... hear him say '<word>' to me"), which
every prompt with the other end in the talk shows. Since then the stage neither reads a slang
copy whose word is everyday (`relations._everyday_slang`, notes evidence and pins) nor keeps such
a sentence in what the model writes (`personas/clean_notes.strip`: a sentence citing an
everyday or typo word as picked up, any word of a quoted list, or as said "to me"). The stored
notes are cleaned by a data step over every relationship row of every member:

```bash
python -m leeroy_mind personas --data-dir "$D" clean-notes              # dry run: counts and ids
python -m leeroy_mind personas --data-dir "$D" clean-notes --go         # mind stopped
python -m leeroy_mind personas --data-dir "$D" clean-notes --undo STAMP --go
```

The dry run opens mind.db read-only. `--go` refuses while the mind holds `mind.lock`, snapshots
mind.db (`snapshots/mind-pre-clean-notes-*.db`), takes the junk sentences out of each note (the
rest stays as written; a note of nothing else is left empty) and writes receipt
`personas:clean-notes:<stamp>` with each changed row's note before and after. `--undo STAMP
--go` puts back each note still as the run left it and marks the receipt undone.

# Life beats: he lives far away (2026-11-15)

The night's life beats invented his texts, notes in her flat and visits, and those memories
surfaced in chat as real. Now every life prompt ends on `life.FAR_AWAY` (after the built-in
prompt or the owner's overlay): he lives far away, they only talk in the game and on the guild's
Discord. Once her romance arc has met him in person (stage weekend or after, an `arc_meetup`
row) it reads `FAR_AWAY_MET` instead: no "never met", still no invented contact in her everyday
life. His names match as whole words only (a pet rabbit is never him). A beat or new thread title that names him with contact off the game (a text, call,
note, gift, visit, his presence: `life.meets_him`) is refused before the judge (grounding log
`meets him offline`; the thread tries again next night), and such a beat is never shown as one
of the last three again. A beat that names him or shares two content words with her secret or
deep disclosure is stored `private` with him (`life.private_beat`), so it reaches only her
whispers and DMs with him; every other beat stays open. The server's Discord seeds
(`server.seeds._life`) skip a beat whose memory is private or retired, so it never opens a
public conversation.

`python -m leeroy_mind nightly life-contact --data-dir "$D" [--show] [--go]` handles the beats
already stored. A dry run (the default) reads mind.db read-only and lists ids and owners (texts
with `--show`). `--go` needs the mind stopped (it holds the instance lock), snapshots first
(`snapshots/mind-pre-life-contact-*.db`), retires every active life-beat memory (offscreen,
evidence `beat:<id>`) that meets him (status `archived`, never deleted) and raises every other
open life-beat memory that names him or touches her secret to private with him. The night's
encounters and the romance arc's weekend and visit memories (meetings by design) are never
touched. Receipts: meta `life_contact:retired:<id>` and `life_contact:rescoped:<id>` with the
earlier status or scope. Chat and extraction memories that repeated an invented contact ("you
texted me about my sandwich") stay active: the step does not reach them.

# Structured reflection replies

The JSON answer pass starts with 1,024 output tokens. If an invalid answer's
model diagnostics explicitly report `done_reason=length`, the existing correction
attempt can use up to 2,048 tokens. Other validation failures retain the original
budget. There are still at most two requests, with the same model context,
timeout, concurrency limit and per-request budget callback.

Long reasoning notes keep their concluding end while reserving space for the
correction. The existing token estimate is checked against each complete request,
including its correction suffix, answer budget and template margin; required
evidence that exceeds that estimate fails the unit before a model request.

`nightly_json_cut` records attempt number and token/character counts.
`nightly_json_failed` records failure class and the last reply's termination
reason and counts. Neither carries the answer, notes or model error text. These
diagnostics distinguish confirmed truncation from malformed replies; older
`ReflectionError` records alone do not establish that truncation caused them.
