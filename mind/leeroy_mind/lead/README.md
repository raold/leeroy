# His "you lead"

Before this, every cast member of his group followed him: he led everywhere. Now when he says in
party or raid chat (or whispers a member of his group) "you lead", "take the lead", "lead the way",
"take us questing", "go grind" or "lead us to the inn", a member **leads**: she chooses where to go
and what to do, the others of his group follow her, and he follows her himself (`/follow`). Her
answer says exactly what happened: never "I'm leading" unless she is.

## Switches

- `[talk] lead = true` in `mind.toml` (the default; anything but `true` is off, logged once
  `talk_lead_invalid`). Read at each line. Off, his lines are plain talk (and the raid lead's
  designations) as before. On, nothing changes until he asks someone to lead.
- Native `Leeroy.Lead.Enable = 1` in `mod_leeroy.conf` (default 0, `.reload config`; see
  `modules/mod-leeroy/README.md`). While it is 0 native refuses `off` and she says she can't take
  the lead right now; setting it to 0 ends the lead in force.
- A worldserver whose hello lacks the `lead` feature (before this release) is never sent a
  `lead_do`: in the open world she says she can't take the lead; inside an instance the
  dungeon-clear path below works on any worldserver.

## Reading his line (`parse.py`)

His party, raid and whisper lines; never in his raid's fight, wipe silence or debrief, never while
he is offline. The lead reads his line **before** the raid lead and invitations.

- **Start**: "you lead", "you lead the way", "take the lead", "lead the way", "lead on", "lead
  us", "show us the way", "your lead", "can you lead?", "wanna lead?", "who wants to lead?",
  "someone take the lead" (`quest`: new rpg's own day of quests, camps and grinding); "go grind",
  "take us grinding", "find us some mobs to kill" (`grind`); "take us questing", "find us some
  quests" (`quest`); "lead us to <place>", "take us to <place>", "lead the way to <place>"
  (`travel`, to one of the invitations' meeting spots, `invites/spots.py`; a place none of ours
  moves nobody and she asks where).
- **Not a start**: a line about another time ("Ana, you're leading tonight", "you lead
  tomorrow") is the raid lead's designation, as before; "you're in charge", "you're leading" too.
  Negations ("don't lead"), "you lead a busy life", "lead ore" are talk.
- **End** (only while a lead of his is in force): "i'll lead", "i'll take it from here", "i'm
  leading", "let me lead", "my lead", "stop leading", "stop", "that's enough", "follow me", "back
  to me", "come back", "regroup on me" (never with a no or another time: "don't let me lead",
  "i'll lead tomorrow"). A whisper ends it only to the member who leads. "follow me" is a
  playerbots command too: it still reaches playerbots, and nobody answers it. While a handover to
  dungeon-clear is still waiting (below) his end cancels it; while his ask is still being carried
  out (its ack not in yet) his end waits for it, 20 s at most, and ends what it started. His "i'll
  lead" in party or raid chat also takes the raid's calling back from her
  (`RaidLeadService.take_back`, as raid chat's own "i'll lead" does), with no line of its own.

## Who leads

The member he whispers; else the member he names (in his group: a member outside it did not hear
his party line, which stays talk); else his group's raid lead (`raidlead/`), else a tank of his
group (by class and spec, as guild life reads roles), else an officer, else anyone of his group;
within each the most raid nights led, then attended (`raidlead/designate.py`), then the lowest
guid. Only members online, not AFK, with a persona. A member he named or whispered also becomes
his raid lead (`RaidLeadService.designate`, no line of her own), as "Ana, you lead" did before.

## What happens

**In the open world** (his chat line's map is a continent or Ebon Hold): native `lead_do`
`start` (`Guild/Lead.cpp`, pure policy `Lib/LeeroyLead.h`). She masters herself (a self-mastered
bot, as an offline raid's driver, so playerbots keeps her as the others' master), stops following
him and runs playerbots' own strategies: `new rpg` with `grind` for questing (new rpg's statuses
pick quests, camps, grinding and wandering; a flight or outdoor PvP is steered back to wandering),
`grind` with new rpg kept to its grinding statuses, or new rpg's walk to the spot for travel
(`go camp`). The cast members of his group on her map who followed him take her as their
playerbots master, so their follow and formation follow her. **She waits for him**: whenever he is
more than 40 yards behind (or on another map) her activity stops where she is; it goes on once he
is back within 20 yards. Never a teleport: playerbots' stuck teleport is held off, and a travel
that gets no closer in 2 minutes ends instead.

Mind refusals before anything is sent: she is not in his group (`not_in_group`), not online,
AFK, he is in a battleground, a raid the guild leads is forming or under way (`raid_led`), a
travel place none of ours (`unknown_place`) or on another continent (`other_map`), no `lead`
feature (`unsupported`). Native's own (`LeadRefusal`): `off`, a fence (Realm lease, training,
an offline raid, a bot job), dead, in combat (hers or his group's), an instance or battleground,
queued, travelling, he is dead, not his group, he doesn't lead it, a finder group, farther than
100 yards from him (`too_far`), `bad_spot`, `other_map`, already there (`here`).

**Inside a dungeon or raid** (any other map but a battleground's, every expansion's and the Wrath
5-mans the boss tables leave out among them): guild life's `raid_control` `dc_on` for his group: dungeon-clear's
tank leads the group boss to boss (native names the tank dungeon-clear elects when another,
`leader_mismatch:<guid>`, and it is asked once more; `leader_mismatch:0` is `no_tank`). The member
he named calls it (raid lead). His end sends `dc_off`; the run stays his to stop until the game
confirms it stopped (or that no run is left: `no_group`, `not_cast`, `no_tank`).

**Its end**: his end (native `lead_do` `end`), or native's own (published as the job `lead`,
state `ended`, reason): `arrived` (travel), `left` (he stayed behind 10 minutes), `timeout` (60
minutes), `no_path`, `dead` (each gets one short line of hers, `source:lead`), `instance` (his
group went into an instance: after 10 seconds dungeon-clear takes it, as above, and she says
who leads now), and the quiet ones: `logout`, `group`, `off`, `taken`, `reset`, `fenced`,
`teleported`, `pvp`, `gone`, `replaced`, `his`. Every change native made is given back from its
journal: the others follow him again, she follows him again and loses the strategies it added
(ones she had before the lead and a wait took off come back). An end native sends before the
start's ack is kept and applied once the ack is in.

**A restart**: at each hello with no lead of the mind's own in force, the mind sends a quiet
`lead_do` `end` (a lead an earlier mind left has nobody to end it). A lead of an earlier
worldserver (another `started_ms`) is forgotten.

## Her answer (`words.py`)

The member who leads answers his line (talk's owed answer: `talk.engine.on_human_chat` asks
`reply_turns`), after the outcome is known (`TurnMeta.ready`, 15 s at most), and the instruction
hook tells her what happened: "you're leading his group out questing: you choose where to go and
which quests to do, and he is following you", "Arkon is leading his group from boss to boss now",
or her reason in plain words with "don't say you're leading". Any other member answering is told
he asked someone else.

## Logs

`lead_read line= activity= who= where= guid= kind= code=`, `lead_body line= guid= kind=start|end|dc_on
outcome= code=`, `lead_ended guid= why=`, `lead_handover guid= outcome= code=`, `lead_followup guid=`,
`lead_cleared why=hello`, `lead_failed error=`; native `lead_started guid= activity= followers=`,
`lead_refused guid= activity= code=`, `lead_paused guid=`, `lead_resumed guid=`, `lead_steered guid=
from= to=`, `lead_follower_released guid=`, `lead_ended guid= why= followers=`. Ids, codes and
counts only, never his words.

## Tests

```sh
python -m pytest -q tests/lead
```
