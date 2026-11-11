# Public banter

`[banter] enabled = true` opts in on restart; `per_hour = 2` is the default.
Missing, malformed or unknown settings disable it. The hourly allowance accepts
0–12 and persists across restarts, shared by guild chat and Discord General.

Topics are recent recorded owner deaths, group wipes/loot, posted calendar
birthdays, confirmed rival Trade lines, public jokes with origins, or ten minutes
of quiet. World events can inform either room; a chat-derived joke stays in its
original medium; Trade quotes stay in game too. A rival's quote is not treated as verified fact. Topics expire
after fifteen minutes. At most one running-gag thread fits each local evening;
each gag then rests for three days, loses selection weight with use, and retires
from banter after four uses. Selection weights staleness and prior use instead
of repeating a fixed order. Ordinary topic receipts last seven days; joke-use
counts survive that pruning. Neither receipt copies text.

The chosen 2–4 members are weighted by humour, talkativeness and ties. Hostile
ties, active hurts/feuds and hurt target moods are excluded. Talkative zero
excludes a member. World chattiness zero pauses banter; other values scale the
gap between threads around the default 4, within the configured hourly cap.
Friends can join, a rival with an appropriate
drama dial can needle, and a fond member can defend without disclosing private
affection. Instructions permit only the supplied public observation, never
invented personal traits or blame. Owner teasing is restricted to a single
death/loot thread per local evening (06:00 boundary), shared across rooms.

An admitted thread plans 3–8 single-line turns through the ordinary engine. It
has its own bounded pacing/chain allowance, so the general loop stopper does not
cut every second participant off. Guild threads pause ordinary autonomous game
bursts/openers; Discord threads leave game scheduling alone. Human replies and
private conversations retain their normal paths. Banter keeps its selected
public transport even when voice is available, and Discord also stays one line.
Cards and deterministic pre-send review are required even if the general talk
switches are off; an unavailable card/review drops the turn. Semantic review
continues to follow its separate calibrated switch. Fingerprint hints contain
only fixed style words. Realism timing applies when enabled, without an extra
split or typo-correction line exceeding the thread's count.

No thread starts within 45 seconds of an owner line in its medium. Every pending
turn rechecks that boundary, five-man boss fights and raid combat/wipe/debrief, including after model
generation and before transport. An owner interruption ends banter; the existing
game or Discord reply planner answers him. Banter runs only while he is online
in game or his Discord desk already holds its live lease. It never takes or
extends a GPU lease and cannot preempt an offline night or image job. Discord
uses its own presence, writes no game session/activity clock and sends no game
commands. Deleted/private/vetoed joke origins are rechecked when their database
snapshot changes; repeated unchanged send checks reuse the result. Owner-line
checks use an indexed initial window and cache new rows by id. Threads that
send nothing refund their hourly, topic and evening allowances.

The existing `lexicon` already supplies public jokes with complete origin and
recalled-memory provenance. `banter_jokes` adds up to twenty rewarded candidates
per night through those same screens, with no model call. A candidate needs a
positive explicit funny vote and effective reward of at least 0.5, looking back
seven days so late votes count; private and
officer lines are ineligible. The stage does nothing when reward tables are not
installed. It runs after rewards when available and also attaches to manual
nightly runs. No new schema or image behavior is introduced.
