# Public banter

`[banter] enabled = true` opts in on restart; `per_hour = 2` is the default.
Missing, malformed or unknown settings disable it. The hourly allowance accepts
0–12 and persists across restarts, shared by guild chat and Discord General.

Topics are recent recorded owner deaths, group wipes/loot, posted calendar
birthdays, confirmed rival Trade lines, public jokes with origins, or ten minutes
of quiet. World events can inform either room; a chat-derived joke stays in its
original medium; Trade quotes stay in game too. A rival's quote is not treated as verified fact. Topics expire
after fifteen minutes; a running gag gets at most one thread a day. All topic
receipts are bounded and retained for seven days, without copying their text.

The chosen 2–4 members are weighted by humour, talkativeness and ties. Hostile
ties and hurt moods are excluded. Friends can join, a rival with an appropriate
drama dial can needle, and a fond member can defend without disclosing private
affection. Instructions permit only the supplied public observation, never
invented personal traits or blame. Owner teasing is restricted to a single
death/loot thread per local evening (06:00 boundary), shared across rooms.

An admitted thread plans 3–8 single-line turns through the ordinary engine. It
has its own bounded pacing/chain allowance, so the general loop stopper does not
cut every second participant off. Ordinary autonomous bursts/openers pause while
it runs; human replies and private conversations retain their normal paths.
Cards and deterministic pre-send review are required even if the general talk
switches are off; an unavailable card/review drops the turn. Semantic review
continues to follow its separate calibrated switch. Fingerprint hints contain
only fixed style words. Realism timing applies when enabled, without an extra
split or typo-correction line exceeding the thread's count.

No thread starts within 45 seconds of an owner line in its medium. Every pending
turn rechecks that boundary and raid combat/wipe/debrief, including after model
generation and before transport. An owner interruption ends banter; the existing
game or Discord reply planner answers him. Discord uses its own presence and a
temporary GPU lease, writes no game session or activity clock, and sends no game
commands. Cancellation releases the lease. Deleted/private/vetoed joke origins
are checked again before each send.

The existing `lexicon` already supplies public jokes with complete origin and
recalled-memory provenance. `banter_jokes` adds up to twenty rewarded candidates
per night through those same screens, with no model call. A candidate needs a
positive explicit funny vote and effective reward of at least 0.5; private and
officer lines are ineligible. The stage does nothing when reward tables are not
installed. It runs after rewards when available and also attaches to manual
nightly runs. No new schema or image behavior is introduced.
