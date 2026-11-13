# Group DMs

Small chats include the owner and two to four cast members. Enable them with
`[groups] enabled = true`; restart the mind and console. The other settings are
`max_open` (1–12, default 6) and `member_starts_per_week` (0–3, default 1).
An invalid table disables groups. Existing histories stay stored when disabled.
Both `[talk] dm_own_time` settings work: off answers immediately through normal
typing; on computes each chosen member's due time independently. Deferred answers
survive a restart, stop after three days, and release when own-time is turned off.
Edits recalculate timing for still-owed members without choosing new recipients.

The Comms DM list contains a New group button, names, faces and unread counts.
Members can be added or removed, and a group renamed. Leaving closes it; removing
members below two also closes it. Closed histories remain readable. All writes use
the existing keyed, same-Origin control op, and respect `--no-control` and mind-down
refusals. Voice notes remain pair-only.

## Privacy

`gdm:<id>` is Discord and restricted, **never pair-private or public**. Every stored
line captures its readers. A line, claim, awareness observation or derived memory
can reach a pair or another group only if every current reader was among those
historical readers. Guild, officer, party, raid, voice and public Discord turns
cannot disclose it. Adding someone hides older lines, pins and reactions from
member prompts; it does not rewrite history. The owner's console retains his own
history. The existing cross-medium pair timeline never contains group lines.
Extraction threads include the membership epoch and captured readers. Every join
or departure starts a separate prompt, including remove/rejoin at the same clock
instant. An officer's group awareness card never includes guild-bank figures;
non-officers receive the normal group card.

Group memories use private scope with `scope_with = GROUP` (-2), plus evidence
validation in `hidden_ok`. Both checks are required. The memory walk follows nested
references up to 512 levels and refuses an unresolved remainder. Bare group channel
names and missing reader snapshots cannot authorize disclosure. Romance prompt
blocks, bond moments, explicit overlays and romantic slash emotes are excluded.

## Background and night work

With the server engine enabled, groups share its leases, admission limits, guards
and cancellation. They have at most two admitted conversations per local calendar
day, each two to four lines. Only public guild event seeds or a member's own life
beat qualify; the life owner must open it. Membership changes end an in-flight
conversation. Seven days without an owner post makes a group quiet; a new owner
post wakes it. Admission spends the daily allowance even if guards later drop the
conversation, avoiding repeated failed starts.
Topic cooldowns are tracked separately for each group.

With `[server] enabled = true`, the nightly language stage makes at most one new
group per night within the global weekly cap. With the server off, it still handles
feud departures but creates no group waiting for an introduction it cannot post.
Every member must like every other member at affinity 40 or more in
both directions, have a persona, be present rather than away, and have no feud in
the proposed group. Its screened name uses the starter's character name and
“friends”; its first line is generated the next local day. A feud causes a member
to leave at night, with a code-built system notice. No model invents memberships.
Other topics wait for the starter's introduction. If she leaves beforehand, her
pending introduction is cancelled and the surviving group can talk normally.
Membership changes and renames log ids, counts and action codes, never names or text.

## Operator notes

Apply the migration containing `discord_group` and `discord_group_member`, then
restart mind and console. No native rebuild or voice-service change is needed.
The feature is off by default; this PR does not change the live setting.
Disabling groups stops their actions and generation without deleting history.
Take the normal database snapshot before release. An older reader does not know
the new scope sentinel and treats `gdm:` as public game chat; restore that snapshot
when rolling back to code from before group support. The deeper provenance walk
(512 levels, failing closed beyond that) also applies with groups disabled. This
is an intentional privacy-hardening exception to unchanged off-mode behavior.

Known review leftovers: the recent-own-lines and move caches lack historical
readers, so their group entries remain hidden rather than inferring access from
today's membership. Restoring those repetition hints needs captured provenance.
The nightly departure's mirrored `chat_line` remains `origin='game'`, author 0:
the schema permits only human/mind/game, and labelling a system notice as a member
utterance would be misleading. Its Discord row remains `bot`/`group-system`.

Tests use synthetic messages and cast members. The audience matrices are in
`tests/discord/test_groups.py`; live fake-pipeline timing and membership tests in
`test_group_live.py`; night and server admission tests in their `test_groups.py`
files. Console control gates, old-schema reads, JS read marks, and 390px browser
flows in both themes exercise the owner-facing path.
