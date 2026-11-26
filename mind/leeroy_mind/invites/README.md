# His invitations in game

When he says in game "let's go fishing in Dalaran", "come to the inn", "meet me at the bank" or
"anyone up for a drink?", the members he asks answer in their own words **and their bodies do
it**: she comes, goes to the place, sits, follows, joins his group or fishes. Her answer is
written after native has carried out (or refused) the move, and it says exactly that: never "on
my way" unless the move was taken.

## Switches

- `[talk] invites = true` in `mind.toml` (the default; anything but `true` is off, logged once
  `talk_invites_invalid`). Read at each line, so it follows a reload at his login. Off, his lines
  are plain talk as before. On, nothing changes until he invites someone.
- Native `Leeroy.Invites.Enable = 1` in `mod_leeroy.conf` (default 1, `.reload config`): every
  string the mind sends for an invitation is marked `"via": "invite"` and answers to this key, not
  `Leeroy.Strings.Enable`. While it is 0 native refuses them `off`: the first invitation is
  answered honestly ("can't make it right now"), and his lines are plain talk again for 10
  minutes or until the next hello (`invites_native_off code=`).
- A worldserver whose hello lacks the `invites` feature (before this release) has no `go_to` or
  `fish`: "meet me at X" then brings her to him only when X is in the city he stands in (else she
  can't get there), and fishing brings her to keep him company. Its come_to_me, sit, follow and
  join_group still go (an older native ignores the mark and answers to `Leeroy.Strings.Enable`).

## Reading his line (`parse.py`, `spots.py`)

His whisper, party, raid, guild, officer and say lines; never a bot command (playerbots' own
words, `app.his_command`) or a raid lead order (the raid lead claims those first), never in his
raid's fight, wipe silence or debrief, never while he is offline.

- **What**: `come` (to him: "come here", "c'mere", "come keep me company"), `meet` (at a named
  place: "meet me at the bank", "come to the inn", "see you at krasus", "let's go to dalaran"),
  `fish` ("let's go fishing", "fishing in booty bay?"), `sit` ("fancy a drink?", "come sit with
  me at the inn"), `follow` ("follow me", "come with me"), `join` ("join my group", "group up").
  An activity needs an ask: a cue word ("let's", "wanna", "anyone", "with me", "up for", "you
  in") or an offer put as a question ("fishing tomorrow?"); "do you fish?" is about her, not an
  invitation. "don't", "never mind", "not now" take it back.
- **Who**: in a whisper, the member he whispers; members he names (at most two, in his order);
  in party or raid without names, his group; else ("anyone", "who wants", an open "let's" in
  guild or say) one or two of those who heard him, the most willing first.
- **Where**: his spot (come, fish and sit without a place); a meeting spot; or a place none of
  ours, when she asks him where and nothing moves. A kind of place (inn, bank, fountain, Krasus'
  Landing, docks) is the one in the city he names, else the city he stands in; a city alone is
  its default spot (Krasus' Landing in Dalaran, the inn elsewhere; going fishing, its fishing
  spot). Spots (`spots.py`, mirrored from native's compiled `Lib/LeeroyInviteSpots.h`;
  `tests/invites/test_spots.py` holds the two equal) are where a world NPC of that place stands,
  read from the world DB's creature spawns (2026-11-20): Dalaran's Eventide fountain (Marcia
  Chase, 11 yards from the coin-fishing "Dalaran Fountain Spell Focus"; the places' `dal_fountain`,
  a fishing spot), Krasus' Landing (Aludane Whitecloud), the Legerdemain Lounge (`dal_inn`), the
  Filthy Animal, Dalaran's bank (`dal_bank`), the inns and banks of Orgrimmar, the Undercity,
  Thunder Bluff and Silvermoon (`org_inn`, `org_bank`, `uc_bank`, `tb_bank` are the places'),
  and Booty Bay's docks (`booty_bay_docks`, a fishing spot). A spot the Realm's places whitelist
  also names carries exactly its coordinates.
- **Later**: "later", "tonight", "tomorrow", "after the raid", "in 20 min", ...: nobody moves; she
  says whether she is up for it, and never that she is on her way.
- **The model** reads only a line the patterns could not settle that names an activity or a place
  and reads as an ask ("the inn?", "do you fish?"), when the live model is already loaded and the
  GPU is not paused: one call at reply priority whose JSON schema allows only `none`, `later` and
  the six kinds, and a sureness below 0.7 is talk. The line is held meanwhile; his next line sends
  a line still being read to talk first, so his lines reach talk in his order.

## Her answer and her body (`policy.py`, `service.py`, `words.py`)

She says yes by a draw seeded by his line and her guid, more readily the better she likes him
(affinity toward him through the owner's lens: 0.95 at 20 and up, 0.85 from 0, 0.55 below 0,
never at -30 or below), a little more when fond, drawn or in lust; with `[life] availability`
above 0 her day weighs in (at work or busy at home mostly no, asleep, out, driving or stepped
away no). The mind's own fences say no first: she or he in an instance or a battleground, she
AFK, an invitation of his to her still being carried out (90 seconds at most), the guild's moves
for the hour spent.

What she does, by `string_do` marked as an invitation (native `Guild/Strings.cpp`):

| He asked | She does |
| --- | --- |
| come | `come_to_me` (only while he is in a city or rest area: else "somewhere in town would work") |
| meet at a spot | `go_to` the spot (her slot of two), and waits there 10 minutes; in his group she just goes along |
| sit | `come_to_me` then `sit` beside him; at a named inn, `go_to` it and wait for him there |
| fish | `come_to_me` (or `go_to` the fishing spot he named) then `fish`; in his group `fish` beside him |
| follow, join | `join_group` (his party, never a raid); already in his group, nothing to do |

Each turn of hers waits, after its delay, for her steps (`TurnMeta.ready`, 15 s at most), then her
instruction (an instruction hook) says what he asked, what she decided and what her body did:
"you've gone to the Legerdemain Lounge and you're waiting for him there", or her reason in plain
words ("you're in the middle of a fight", "you're tied up with something for the guild right
now", "you never learned to fish"), with "don't say you're on your way". A second step (sit,
fish) is tried again while she is still landing (`travelling`, `too_far`, `not_online`; 4 s
apart, 4 tries). When her body did more or less than her answer said (it was written before the
move settled, or the fishing was refused after she came), she says one short follow-up line
(`source:invite`). Any other member answering his invitation is told he asked others, not her.

**Limits**: at most 2 members per invitation, 6 members moved an hour across the guild (a
refused move is given back), one invitation pending per member.

**Fishing** is playerbots' own "master fishing" (the strategy its bots take when their master
fishes): in his group she fishes near him as his bots do; outside it native finds water from where
she stands (or a spot to walk to) and puts her stay there, for 15 minutes at most or until he has
come within 40 yards and gone farther than 60 again; her weapons go back into her hands then. A
random bot without a pole is given one by playerbots; one without the fishing skill says she
never learned. GuildFishing's single proved
cast (`bot_do` "go fishing") is not used: it refuses any member in a group or with a master, and
the stay a come_to_me or go_to leaves her (`busy:string`) fences her from its Realm queue.

**His Discord DM** never moves a body: an invitation there ("let's go fishing in dalaran") gets the
instruction to agree to meet up in game later, or not (`invite_dm_later`).

## Logs

`invite_read line= what= who= where= later= members=`, `invite_member line= guid= accept= reason=
steps=`, `invite_body line= guid= kind= outcome= code=`, `invite_followup line= guid= failed=`,
`invite_model line= outcome=invite|talk|talk_first`, `invite_dm_later guid= what=`,
`invites_native_off code=`, `invite_failed error=`. Ids, kinds and codes only, never his words.

## Tests

```sh
python -m pytest -q tests/invites
```
