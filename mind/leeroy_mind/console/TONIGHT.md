# Tonight and the feature switchboard

Tonight is the first console tab. The default page still opens Comms; `#tonight`
opens Tonight directly. The tab polls once a minute while visible. Failed polls
retain the last good data and show its age. Faces respect Hide photos.

The seven cards are read-only: today's posted raids and signups; unanswered DM
threads, letters, selfie choices and open polls; the existing top-five DKP due
list; up to six bank items below half their stock target; your consumables; the
next seven days on the calendar; and observed game presence and the last finished
night. DMs, letters, officer lines and memories are never displayed. Poll counts
read as not set up until the Decisions tables exist. An unanswered DM means the
member's newest surviving message is later than your last message in that DM.

Kit counts include your bags and character bank. Targets are two flasks, ten
potions and ten food items for a three-hour raid. Missing enchants and gems are
outside this first version. Missing character, stopped mind and failed reads
have distinct explanations; none means an empty inventory. Item names use the
local library, falling back to item IDs if it is unavailable.

The new `kit` control operation accepts no arguments and only reads the game
database through the existing armory. Callers share one in-flight read with a
15-second wait and reuse successful replies for 30 seconds. It remains available
during other Ops actions. Tonight gives each database section one second and the
kit card up to 2.8 seconds, running them concurrently so the request stays within
three seconds. As with Ops, a stalled source owns one daemon worker until it
returns; subsequent polls cannot create more workers for that source. A late
result can be used on the next poll.

Guild presence uses the console's recent cached mind status; unknown stays
unknown when the mind or bridge is down. Old nights without retained failure
counts show an unknown count, not zero. Calendar dates use the console host's
local timezone, matching the existing calendar desk.

Ops now includes the feature switchboard. It reads at most 64 KiB of mind.toml,
shows only known switches as on, off or invalid, and lists dependencies. It does
not show local config values, read native configs, or read the blocklist's
contents. Native settings and rebuild requirements are informational; blocklist
presence is checked. A newer mind.toml is marked for restart when the running
process's start time is available. No toggle changes configuration from here.

Deploy this PR with the normal mind-only `bash scripts/ac-release.sh <commit>`
flow, then restart the console to load the new tab and static files. No migration
or native rebuild is needed. The implementation and tests do not deploy it or
change any live services.
