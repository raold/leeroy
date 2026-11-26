# Standalone draft guards

`review(GuardStores, Draft) -> Verdict` performs bounded reads and comparisons.
It never sends, writes, migrates, logs text or invokes a model. The opt-in talk
adapter in `talk/awareness.py` calls it before delivery. Disabled configuration
returns `send` before touching any input or store.

The rules reuse the single-candidate helpers in `eval.talk`: token shingles and
optional cosine for own repetition, five-word phrases used by two other members
in the current evening, stock openings/closings, filler and parent/thread
relevance, existing `claims.check`, and narrow positive world-evidence checks.
The scratch-only batch detectors are never called. Existing `talk_move` topic
memory adds a repetition reason; a new fact about an old topic remains allowed.

## Configuration

The core config accepts this extension. Call `load_guards_config(path)` separately
when constructing `GuardStores`. No live configuration is changed by this package.
Missing settings mean the defaults (the guards on); malformed or unknown settings disable the
whole table.

```toml
[guards]
enabled = false
semantic_enabled = false
repeat_overlap = 0.70
repeat_cosine = 0.94
parent_cosine = 0.20
thread_cosine = 0.25
repeat_seconds = 259200
gag_daily_cap = 2
stock_daily_cap = 3
phrase_cooldown = true

[guards.gag_caps]
"7" = 1
```

These are provisional task-12 thresholds. Its embedding measurements used fake
word-hash vectors, so semantic enforcement has its own switch, `semantic_enabled` (on by
default since 2026-11-25), for calibration with the caller's embedding model. Lexical overlap and cosine
trigger at `>=`; relevance requires both scores strictly below their thresholds.
Fewer than four words bypass near-duplicate and semantic relevance checks.

## Inputs and visibility

Use `guild`, `officer`, `whisper`, `game`, `discord`, `dm` or `voice` for `Draft.channel`.
For `game`, `room` is the original say/yell/emote/party/raid/raid_warning channel.
For Discord, DM and voice it is an opaque ASCII room identifier, not a display name.
Spoken history stays in its actual voice room and needs the captured audience;
the officers room also requires an officer role. Private spoken pairs remain whispers.
Complete emoji-only Discord messages remain messages on both review passes.
Whispers and DMs include `to_guid`. Parent and thread IDs are explicit evidence;
the guard does not infer a parent from general guild adjacency.

Stored game chat is read from `conn`. Discord history is caller-supplied through
`HistoryLine` and must include the captured reader IDs. All history IDs share one
namespace with parent/thread references; do not supply a second copy of a stored
row. DM pair, room and audience fences and stored officer role apply before any
comparison. Private history cannot veto a public-channel draft. Own public lines
can be compared across game channels. Discord phrase reuse stays in the visible
room. Guild evening and daily ration dates use America/New_York, including DST.

`vectors` maps stored chat IDs to cached vectors; external lines carry their own.
Every vector must be a valid 512-float packed vector in the draft's embedding
space. Embeddings are never generated here. Optional `facts` is an already-loaded
`eval.talk.unawareness.load_facts` snapshot; load and refresh outside the fast path.
That checker recognizes only a few assertions contradicted by positive prior
evidence and has very low coverage; it does not replace an awareness card.

Set `claims_enabled` only when claims are enabled. Existing claim checks run only
with a draft embedding and available tables. Chat-source conflicts additionally
require visible provenance from this member; owner-source claims are allowed.

Reads are bounded to 128 own lines, 256 guild lines from the last 12 hours,
16 recent context lines (including parent), 64 topic moves and 100 adopted active
lexicon gags. External history is limited to its last 400 entries. Old schemas
omit unavailable sources and expose a numeric `missing_sources` score; they do
not gain tables. Coverage is therefore bounded, not exhaustive. Pass `line_id`
only for historical replay to exclude the stored candidate and later row IDs.
Own-repeat, five-minute parent/thread and 12-hour evening-history windows are
independent; shortening `repeat_seconds` does not disable the other checks.

## Engine contract

`Verdict` carries action, fixed codes, numeric scores and IDs, never line text.

| Action | Caller responsibility |
|---|---|
| `send` | Deliver the draft, then record the confirmed delivery. |
| `rewrite` | Request one screened new draft, telling the model what is wrong in words, then review again. |
| `react` | Use a fixed nod/like or laughter reaction when no answer is owed. |
| `drop` | Suppress an unsolicited line; retry once when an answer is owed to the owner. |

For answers owed to the owner, a second unfavorable verdict is advisory: send
the better screened candidate. React also prompts one attempt at a real answer.
Existing hard content, name, slash and delivery gates still apply.

`echo_parent` (owner, 2026-11-14) is a drop code that stays hard even when the
answer is owed: a draft that says back the message it answers (the whole of a
message of 6 or more words in the same mood, a question as a question or a statement
as a statement, its letters 90% alike; or a run of 8 words copied from one of his
statements; apostrophes ignored, her own lines never count) is never sent, and never
chosen as the owed answer's fallback. When both drafts echo, the answer drops
(`guard_echo`). The screened retry is told what is wrong in words
(`review.rewrite_hint`), not by code names, which the model could not act on.
Measured on the live history before the change: it flagged exactly the 3 echoes
among 84 Discord replies to him and none of 683 whisper replies. Turning his
question into its answer ("Are the flasks ready?" / "The flasks are ready.") is an answer.

Widened (identity audit, 2026-11-15) with the thresholds otherwise unchanged: a run of
8 words copied across his sentences (a question among them) is an echo, and so is her
line opening with his first 5 words or more when the copy carries past the end of his
first sentence and that first sentence has 3 words or more ("are you sure? its
kinda..."). A run inside one question of his is still an answer, and so is his short first
sentence said back alone ("you drive me wild" / "you drive me wild too") or after a short
interjection ("haha. you drive me wild" / "haha you drive me wild too"). Words he asked
for are the answer, never an echo ("repeat after me...", "now say it:"; never "i'd say
that's fair"). On 2,063 stored replies that follow his line in their conversation it
flags exactly the 4 known echoes (the 3 above and the cross-sentence copy that passed the
first version).

## Identity (identity audit, 2026-11-15)

`self_male_slip` (a woman's draft) and `self_female_slip` (a man's) are rewrite codes
for a member speaking with the other sex's body or role in the first person (`identity.py`:
"my" plus anatomy, "i'm a real gentleman", "i'm not some nice gentleman", "as a guy, i",
"bury myself in you", a first-person "i'll ... in you"; the mirror list for men). Never
counted: her words about him, a bare correction ("i'm not a guy lol"), self-mockery and
idioms ("i'm such a dad", "a dad joke", "i'm your man for the job", "i'm not some guy",
"i'm such a mom", "a mom friend"), quoted words, and a sentence reporting what someone said
("he said i'm a real man"). Like `echo_parent` they are in `HARD_CODES`: never sent and
never chosen as an owed answer's fallback.

A draft with a hard code is never the turn the model goes on from: its retry is the composed
prompt with what was wrong said at the end of the instruction (`review.fresh_hint`), never
the draft as an assistant turn (the audit: the model reads the last text and continues it).
Owed answers never vanish (owner, 2026-11-15: every message from him gets a reply): when
every draft had a hard problem, the pipeline sends the longest clean part of any draft that
slipped (each slipping sentence left out, at least 3 words, a tie to the earlier draft,
reviewed again with nothing hard left), else makes one fresh generation the same way and
sends it if nothing hard remains, and only then drops (`guard_identity` after a slip,
`guard_echo` after echoes). An unowed line that slips twice is dropped. On the stored
history the lists flag the 5 known slips and nothing else.

`persona_conflict` is a rewrite code for a first-person line contradicting the speaker's
own persona (`personas.facts`: who she lives with, her kids, her pets, her job, her
country; "i'm single" from a married member, "my dog" from a cat owner). Never counted:
idioms ("not married to this spec"), jokes ("lol jk"), a hedge ("i'd say"), what follows
"if i ...", someone named in fun ("she's my wife now", "listen up my children", "my kids in
this guild"), a pet in the game ("my cat form", "my cat pulled the boss"; "kitty" is never
read as a pet). The owner's own edits of her life win: when the persona is about to hold
something against her, the guard reads her active `owner` memories (godmode implants, his
seeded history) and his godmode life events in force (`facts.owner_texts`), and a topic one
names (a partner, kids, pets, where she lives, her job) is never held against her. The
persona also outranks stored chat claims: a `claim_conflict` whose claim itself contradicts
the persona (an echo or an invention stored as hers) is ignored. On the stored history it
flags 6 lines, all inventions (pets, a son, an echoed "single, no kids"); one member's "my
dog" lines are not flagged because the owner's seeded history gives her a dog. The persona
row is read with the role, in the same statement; the owner's edits only on a conflict.

A rewrite takes priority over drop, which takes priority over react. The optional
`await judge_relevance(reasoner, stores, draft)` is a separate model call that may
clear only the relevance finding. Errors remain rewrites; changed context is
rejected and independent guards are rerun after the await. The caller owns the
reasoner's privacy/logging policy, model budget and cancellation.

`record_sent(stores, delivered_draft, receipt_id=...)` is the explicit write API.
`delivered_draft.now` is required and must be the fixed confirmed delivery time,
preserved across retries, including retries after midnight. Receipt IDs are stable
opaque ASCII delivery IDs. The call joins the caller's transaction and never
commits. Retrying the same ID with the same matched keys is a no-op; reusing it
with different keys on that delivery day is an error. Serialize review, delivery
and recording per member when exact concurrent cap enforcement is required:
review is not a reservation and cannot enforce caps
against concurrent sends by itself. Persist delivery identity/time through crash
recovery; recording cannot prove a remote send succeeded.

Receipts live in existing `meta` keys `guards:sent:<local-day>:<guid>`; no migration
is needed. Values contain hashed receipt IDs, hashed stock phrases and gag IDs,
not text. Rejected candidates and reactions spend no text allowance. Caps count
across channels per member/day; lexicon joke/slang caps require active adoption.
A slang term of one everyday word or a typo of one (`talk/everyday.py`) is never rationed or
recorded; a running joke always is.
Confirmed delivery prunes receipt keys older than the preceding fourteen local
calendar days, in the caller's transaction. Retries outside that retention window
are not deduplicated. Other metadata is untouched. Missing receipt tables omit the
check; malformed receipts return a fixed rewrite code rather than resetting caps.

## Phrase cooldown

`phrases.py` (2026-11-15, the owner's Turing-test push) works on phrases, where the
line overlap above works on whole lines: of 255 lines that reused a 4-word run from the
member's last 3 days, only 14 reached the 0.70 overlap. With `phrase_cooldown = true`
(the default; `false` turns it off, restart the mind) a draft gets a rewrite code when
one of its phrases is:

| Code | Rule |
|---|---|
| `phrase_conversation` | one she already used in 2 or more of her lines in this conversation (in one when it holds a stock entry): her lines in the same room, or her whispers and DM with him (one conversation), each within 30 minutes of the next, at most 3 hours back |
| `phrase_day` | one she used in 2 or more of her lines in the last 24 hours, any channel (the third use is flagged) |
| `phrase_cast` | one 3 or more other members used in the last 48 hours; 3+ words with 2 outside STOP |
| `stock_member` | a line of `stock_phrases.txt` she used in the last 72 hours |
| `stock_cast` | a line of `stock_phrases.txt` the cast used 3 or more times in the last 24 hours |
| `phrase_exemplar` | a run of one of her own earlier lines her prompt showed as an example (`Draft.exemplars`, `[talk] wow_grounding`, talk/grounding.py): 4+ words, or 3 with 2 outside STOP, however long ago the line was said |

A phrase is 2-5 words inside one comma clause, with a word outside `STOP` (two for a
two-word phrase), no member's, his or an active nickname's name, no boss, raid, dungeon,
battleground or arena name the code knows (`wow.py`, so a lowercase "the lich king" is a
name too) and no capitalised proper noun. Words are folded first: case, apostrophes, `u`/`ur`, `gonna`, `rn` and the cast's
other abbreviations, so stored (styled) lines and the plain draft compare. Item placeholders,
bracketed names, URLs and mentions break a phrase. The conversation's subject is never her
tic (owner's live DM test, 2026-11-16: a sandbox replay of his DM session had all 6 answers
rewritten, four of them for the place and plans he was writing about, said back): the
subject is the content words (outside `STOP`) he and other members wrote in this conversation
in the last 3 hours, each speaker's newest 10 lines, with simple inflections folded (two words
of 4+ letters sharing a prefix of 4 letters and all but 2 letters of the shorter:
"norway"/"norwegian", "fast"/"faster"). A phrase whose every content word is in the subject is its
topic for all three phrase rules; a two-word phrase is no candidate when either word is; a
stock entry is skipped only when its content words are his (his "thinking about you" said
back), since the cast saying it is what the stock ration is for.
`stock_phrases.txt` holds the curated stock roleplay and AI phrases; its header says how to
extend it ("^" anchors one to a clause opening: `^at least` is the consolation frame).

Its lines come in two classes (controller ruling, 2026-11-16). A leading `!` marks **slop**
(the roleplay epithets and tics: "you absolute ...", "menace", "the audacity", "lose my mind"):
over its ration it is a rewrite on every turn. Every other line is **common** ("sitting here",
"every single", "at least", "i swear"): over its ration it is a rewrite only on a turn not owed
to him. On an answer owed to him (`Draft.real_answer_due`) a common entry, alone or inside a
phrase the three phrase rules flag, gives no code: `Found.pending` / `Verdict.pending` carry
the list's own words for it, the line goes out with no extra model call, and her next turn's
instruction ends "Don't say '<entry>' again; say it your own way or not at all." (the
pipeline's `_phrase_pending`, kept 12 hours, cleared once that turn is said; mind.log
`phrase_hint_pending guid= n=`, a count only). `stock_cast`'s guild-wide ration is unchanged
on turns not owed to him. Replay of his DM drafts (owed): the sandbox session's 6 first drafts
went from 6 rewritten to 1 (a non-stock phrase), 3 leaving a hint; the 7 live drafts of
2026-11-16 from 5 to 2 (both slop), 4 leaving a hint.

Two bounded reads per draft: the names (persona, `meta human_name`, active nicknames) and
the last 72 hours of member and owner lines (`chat_line_ts`, at most 4,000 rows). A line
already dropped as a whole-line repeat or echo stays dropped; phrases only add a rewrite.
`Verdict.phrases` carries up to three phrases in the draft's own words (never logged, not
in `repr`); `rewrite_hint(codes, phrases)` adds "Don't say 'X' again; say it your own way
or not at all." Evidence is her own lines in any channel (including group DMs) and other
members' lines counted, never quoted: the hint names only the draft's own words, never
who else said them or where. Like the stock ration's receipts, the reads cross audiences
(`memory/AUDIENCES.md`): her whispers, DMs, officer and group-DM lines, and other members'
private lines, count toward a ration on a public draft. Only the decision crosses; no text
does, and nothing from them reaches the prompt. A review-required turn (banter, the
server) under a disabled `[guards]` table gets the review but no cooldown
(`LiveReview.config_for`), so an operator with guards off sees no phrase rewrites.

If the screened retry still has a flagged phrase and nothing else is wrong
(`talk/pipeline.py` `_without_phrases`), `phrases.strip` cuts a closing epithet ("..., you
absolute menace"), else the phrase's comma clause, else its sentence; the rest is styled again
and reviewed once more. A clause is never cut across a word of `TURNS` (but, though, if,
unless, because, except, until, so, while, yet: "that sounds fun, but i'd rather stay in" cut
to "that sounds fun" says the opposite; the sentence goes instead, or the line); a
conjunction the cut leaves hanging at the front of a sentence goes with it ("you're trouble,
but yes, i'll come" -> "yes, i'll come"); a cut that takes the sentence's question takes its
"?" ("it was good, how was your day?" -> "it was good."); an anchored stock entry
(`^at least`) is cut only where a clause opens with it, never from "at least ten". When
fewer than three words survive, or no cut is possible, the line is dropped
(`guard_phrase_refused`). **An answer owed to him only ever loses a closing epithet**: any
other cut could change what she answered, so the retry goes as written; it is never dropped
for a phrase alone and never dropped `expired` on this path. Any other code on the retry
keeps the old rule.

Replay on the live history (read-only, 2026-10-31 to 2026-11-15, 4,425 member lines
without templates and short `gz`): 502 lines flagged (11.3%; game 7.4%, voice 9.3%,
Discord 20.8%), 185 of them answers owed to him. `phrase_conversation` 161,
`phrase_day` 95, `phrase_cast` 36, `stock_member` 120, `stock_cast` 291 (a line can
carry several). Of the 226 lines reusing a 4-word run of hers from the last 3 days, 140
(62%) are flagged. A hand-judged sample of 60 flagged lines: 45 real tics or cross-member
sameness, 9 borderline (ordinary "at least", "every single"), 6 false positives (topic
nouns only she repeated in one conversation: "grain density", "chicken wrap"). Mean check
4.7 ms. A false positive costs the one retry; if the retry keeps the phrase (a topic noun
often is the subject), a non-owed line is then clause-cut or dropped, an owed one goes as
written. Replay measures flag rate on what was said, not behaviour after rewrites. These
numbers predate the subject rules and the slop/common classes of 2026-11-16, which only ever
flag less; on the sandbox DM session the drafts the guard reviewed went from 6 of 6 rewritten
to 0 of 6 (2 leaving a hint for her next turn).

To replay (for example after extending the list), open `mind.db` with `?mode=ro` and, for
each member line of `chat_line` (`origin='mind'`, oldest first), call
`phrases.check(review.Reader(conn), Draft(guid, channel, text, to_guid=..., room=...,
now=ts, line_id=id), ts)`: `line_id` keeps the line and everything after it out of its
own evidence. Leave out template lines (`utterance.why` `say_as`) and short `gz`.

Rollback: remove `phrase_cooldown` from `[guards]` before running older code, which reads
an unknown key as an invalid table and turns all guards off.

## His line said back as her opener, and a member's wrong pronoun (2026-11-16)

The evening DM audit (38 graded answers to his whispers and DMs after #347) found echoes in 10
and none was a copy `echo_parent` holds: his short line said back as her first words, quoted
back, or what he said he did restated to him as "you ...?". Two soft codes, no switch, on every
channel (they need `[guards] enabled`):

- `echo_opener` (`opener.py`): her opening (first 40 words) repeats a run of one of his clauses
  holding all its content words (two words at least; never one with i/you/we, a greeting, or
  one she gives back with "too"; his question said back as a statement is its answer), quotes
  his words, or restates what he said he did ("you" + his act word; when he asked her
  something, only as her first words), or reports what he asks ("you're asking to ...").
  The retry is told "answer it without saying it back". A second draft that still opens on it
  goes to the phrase cooldown's strip (`talk.pipeline._without_phrases`, `CUT_CODES`):
  `opener.cut` takes the echoing clauses out when 3+ words stand alone; an answer owed to him
  that cannot be cut goes as written (never dropped), a bystander line is dropped
  `guard_rewrite_refused`. `Verdict.echo_of` carries his line for the cut (repr-hidden).
- `member_misgender` (`pronouns.py`): her draft names guild members of one gender by character
  name (else his line she answers does) and calls them only "he"/"she" of the other sex after
  the name. Quiet for a name she speaks to, someone called "he"/"she" before the name, both
  genders named, a guild member without a persona (`cast_member`), his names (meta
  `human_name`, `GuardStores.his_names`), a game name, a person or pet word. The hint names
  the member and the pronouns only (`Verdict.pronouns`, repr-hidden).

Measured offline (read-only, counts): on the 10 graded echoes 8 caught (the misses: an echo
deep in a run-on, one of an earlier message), 0 of 28 clean answers flagged; pre-#347 run 5 of
11, 0 of 27; 2 of 200 random stored answers to his lines flagged, both his line said back (18 of
all 2,074, each on reading his line said back). `member_misgender`: both graded cases caught, 0
of 145 other graded lines; 12 of about 5,450 stored member lines, 11 of them a real
misgendering (officer attendance threads), 1 ambiguous.

## Her voice sheet (2026-11-17)

A member with a voice sheet (`Persona.speech`, `personas/speech.py`; imported by `personas
voices`) is held to it by three string checks on her stored sheet (`voice.py`). No switch (they
need `[guards] enabled`); a member without a sheet is reviewed exactly as before.

- Her own words (`says`) are **rationed, not cooled**. A phrase that is one of her words (it
  holds one whole and nothing else of content, or is a run inside one) is never
  `phrase_conversation`, `phrase_day` or `stock_member`. Instead each of her words, a single
  word included, may be in about one in three of her lines in a conversation: the draft is
  flagged when it would be use number `uses + 1` among her last 9 lines there and that is more
  than 2 and more than `ceil((lines + 1) / 3)` (a third use needs seven lines, a fourth ten).
  Over the ration it is named as `phrase_conversation`, so the existing hint ("Don't say
  '...' again") and strip apply. `phrase_cast` and `stock_cast` still count her word.
  A slop entry (the stock list's `!` lines) is never her own word: a say-word inside one
  exempts nothing, in either loop of `phrases.check`, and the import refuses such a sheet
  (`says_slop`, the same test: `voice.hers`).
- `voice_never`: the draft uses a word of her `never` list (whole words, any case; an entry with
  no word in it, such as "!!", as written). Soft: one rewrite, the hint names the words
  (`Verdict.never`, repr-hidden: "You never say 'x'."). The list is enforced whether or not the
  turn's block showed it (a private turn's block may leave never-words out for room).
- `voice_sample_copy`: the draft holds one of her five sample lines word for word (as a run of
  words when the sample has 4 or more, else as the whole line). Soft: one rewrite. The samples a
  turn's prompt showed are also `Composed.samples`, so a word-for-word copy of one (3 words or
  more) is first the pipeline's existing fatal `sample` tell in generation (in
  `OWED_HARD_TELLS`), as it always was for the generator's samples, and is regenerated. Only
  this guard code is soft: an owed answer is never withheld for it.
- A never-word that her own typing produces after the guards have read the line (her
  abbreviations, her typing texture's shorthand and habitual spellings) cannot be caught here;
  the import refuses such a sheet (`never_in_typing`).

Neither code is in `HARD_CODES`, `DROP_CODES` or the pipeline's `CUT_CODES`: a bystander line
that still has one after its rewrite is dropped `guard_rewrite_refused`, and an answer owed to
him is never dropped for it (the best draft is kept, as for every soft code).

## WoW turns (`[talk] wow_topic`, 2026-11-20)

`Draft.wow_topic` is set by the live adapter on a WoW turn with `[talk] wow_topic` on
(`talk/wowtopic.py`: grounding's `wow_turn` channels, and say or yell while he raids; never a
whisper, a DM, a group DM or #general). On such a draft only:

- `chat_register` (`register.py`), soft: the draft reads as written prose, not raid or guild
  chat: a first sentence of 6+ words that opens with a capital, ends in "." or "!", holds a pair
  chat contracts ("it is", "do not", "I am", "cannot") with no contraction anywhere; a line
  opening "It is" (never "it is what it is"); a pet name ("honey", "hun", "sweetie", "darling",
  "babe" anywhere, "dear", "love", "sugar", "hon" only as an address); a written stage note (a
  bracketed label such as "[objection: ...]", never a recipe's "[Pattern: ...]", or one word
  alone in parentheses written as a note, "(Tidy.)", never "(lol)"). One rewrite with
  `HINTS['chat_register']`; never in `HARD_CODES`, `DROP_CODES` or `CUT_CODES`, so an answer
  owed to him is never dropped for it.
- Her own words (`says`) are rationed tighter: once in her last 9 lines of the conversation is
  enough (`phrase_conversation`), and so are two of her lines of the last day anywhere
  (`phrase_day`). The prompt side leaves a word of hers she used in one of her last 8 lines out
  of the voice block's "You say, only now and then" on those turns (`personas.speech.unused`).

The stock list gained generic talk ("rng gods", "fingers crossed": common), theatre metaphors
("curtain call", "standing ovation", "take a bow", "steal the show", ...) and two scripted
openers ("okay so listen", "second thing first"), the last two groups slop, for every turn.

## Live adapter

`[guards] enabled = true` admits review independently of `[talk] aware = true`.
Both are on by default. The card uses up to 600 estimated tokens for guild/officer/
Discord, 450 for whispers/DMs, and 400 for other game channels, plus a fixed
delimiter, inside `LIVE_BUDGET`, before the final instruction. If the prompt is
over budget, the card is removed first. Model cards use names and relative ages;
diagnostic cards may retain IDs. Voice uses its actual room and captured audience. Game and Discord chat
evidence never cross media. DMs and whispers retain the exact pair; owner claims
need visible cited provenance too. Uncited owner corrections are omitted. Discord
cards and guards omit game state. Text emotes at a member are visible only in
game say or that whisper pair; romantic emotes only in that pair's whispers.

Every styled draft is checked before typing. One rewrite per turn goes through
the existing real-name, content and tell screens, then review again. Deterministic
template announcements bypass model review and its rations entirely. Unsolicited
reaction verdicts send a game nod/laugh or react to the Discord parent. Answers
owed to the owner remain text answers: whisper replies, answers to his DMs, named
replies, pushback, named Discord answers and replies to his targeted emotes. Public
bystanders, drawn replies, hellos and goodnights can be dropped without a retry.
Reactions spend the turn's pacing budget
without recording a spoken line, memory use, game session or Discord game activity.
The first planned answer to his question is owed regardless of the speaker picker;
later drawn/topic/memory answers are not. This applies separately to game and Discord
planning. Only the member his emote targets is owed an emote answer, not joiners.

Typed lines are checked again after typing; a new unfavorable verdict can drop
an unsolicited line but remains advisory for an owed answer. Per-member locks
cover generation and review; delay, voice hold and typing run outside them.
Realism bursts recheck their original reviewed text before the first fragment;
later fragments inherit the verdict. Scope, sequence and combat checks still run
for every fragment. This prevents a short fragment from being mistaken for filler.
The ordinary send lock still protects delivery. The utterance receipt persists
`guard_verdict` and `guard_rewritten`; guard drops store no draft text and log
only fixed codes, member and memory IDs.

Any review/embedding exception logs only `guard_review_failed_open` and the guid,
then sends through the ordinary delivery gates. Card failures omit the card and
log `awareness_card_failed`. Cancellation propagates. Semantic
checks still require `semantic_enabled`; enabled claims use the existing embedder
and contradiction checker. Claims alone embed only the draft. At most seventeen texts are embedded per check, with
a five-second timeout, no open read transaction, and a 512-vector memory cache.
SQLite snapshots and guard evaluation run on worker threads using separate
read-only connections. Audience, officer status, member/group state and draft
coordinates are captured on the event loop first; no worker calls the live world.
Context scans use channel/time bounds and the
`chat_line_channel_ts` index. Game world evidence refreshes at most once a minute.
These are bounds, not a latency SLA.

Run `python -m pytest -q -s tests/guards/test_rehearsal.py` for the four-turn
FakeLLM/FakeBridge comparison. Off: four text lines, one contradiction, one filler
after a question, one location mismatch. On: four text answers, no reactions, zero
of those flags. Thus each of those detector rates is 25% to 0% per attempted turn
(25% to 0% per sent line); repetitions are zero in both. The script selects its
rewrite answers and uses fake embeddings; this measures integration, not live
quality, semantic calibration or a false-positive rate.

## Measurement limits

`calibration.measure(stores, [Case(draft, acceptable=...)])` returns only aggregate
counts and latency. `acceptable=None` is unlabeled and yields no false-positive
estimate. Human-labeled acceptable cases supply the false-positive denominator;
synthetic fixture accuracy does not estimate live-history accuracy. Replaying
observed history measures flag rate, not counterfactual behavior after rewrites.
Historical ration evaluation needs contemporaneous delivery receipts; historical
claim evaluation needs contemporaneous claims. Current mutable records cannot
reconstruct those states. The caller must report those omissions and missing
parent/vector coverage alongside rates.

The 2026-11-08 private read-only-copy replay covered 1,529 supported text lines
from 92 members in 14 days; 618 voice lines were outside the API at that time. Overlap
thresholds 0.70, 0.80 and 0.90 each flagged one line (0.065402 per 100).
The default 0.70 path took 1.3514 ms median / 3.7511 ms p95. Enabling fake
word-hash semantics flagged 109 lines (7.128842 per 100), including 108 relevance
findings, at 2.2403 / 8.8337 ms. Of 277 reciprocal directed parent candidates,
263 had usable, visible relevance context. There were no stored parent links,
human labels or delivery receipts. Claims were disabled for this replay, and
current roles/adoptions were not historical reconstructions. World evidence had
no verified kills and generated no findings. These rates cannot identify a
false-positive rate or justify enabling semantic thresholds in production.
