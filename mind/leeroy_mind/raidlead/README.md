# The raid lead

He keeps his group: he stays its game leader (`.playerbots bot add25` and its refill need him as
leader, a bot with the leader flag makes playerbots reset every bot's strategies, and the leader
keeps loot master). The raid lead is a cast member of his group who **calls** it, and his orders
to the group are understood and carried out by the bodies.

## Switches

- `[raidlead] enabled = true` in `mind.toml` (default false; a table with any other key or a
  non-boolean value, or an unreadable `mind.toml`, is off). Read at the mind's start: restart the
  mind after changing it. Off, nothing is attached: no handler, task, op or prompt, and his lines
  go to talk as before.
- Native `Leeroy.RaidLead.Enable = 1` in `mod_leeroy.conf` (default 0, `.reload config`); see
  `modules/mod-leeroy/README.md`. A worldserver without the rebuild does not list `raidlead` in
  its hello features, and the raid lead then does nothing. With the key at 0 native refuses
  `raidlead_off`: the order is logged and nobody says anything.

## Who leads

A raid lead is a cast member of his current group, chosen:

1. in the console's Raid tab (the **Raid lead** picker: a member, "me", or "the guild's pick"),
   through the control endpoint's `raidlead` op (`{"action": "set", "guid"}` or
   `{"action": "clear"}`), behind the console key, Origin and `--no-control` gates;
2. by him in raid or party chat, or by push-to-talk in a room that is not private: "Ana, you're
   leading tonight", "Ana leads", "Ana is raid lead", "you lead, Ana", "raid lead: Ana", "let Ana
   lead"; "I'll lead" takes it himself (nobody calls it, no default);
3. by default, when he named nobody: the officer in his group (online, with a persona) with the
   most raid nights led (`guild_raid`, offline raids aside), then the most attended
   (`guild_attendance` 'raided'), then a "raid lead" officer title, then the lowest guid
   (`designate.py`). A group with no officer has no default lead. The default is silent.

It lasts until he changes it or the group breaks: he leaves it, it disbands (`group` events), or
she leaves it (`group`/`guild_group` events). It persists in meta `raidlead:state` (lead, source,
his own lead, the assistant flag we set, his group's members and raid flag), so a restart keeps
it. In a raid group native makes her **raid assistant** (`raid_lead assist`) and takes the flag
off a lead he replaced; a party has no assistant flag.

## What she does

The designation feeds guild life: his own add-built group adopted as a raid night
(`guild/raid_night.py` `_adopt`) records her as the raid's lead (`plan.lead_guid`, the
`guild_raid` row, the talk's lead) while `RaidRun.lead_is_human` stays true: he drives it, so no
automatic ready check, countdown or dungeon-clear. The existing raid calls (`guild/calls.py`
`RaidCalls`: marks and the skull rti, the soulstone, Bloodlust on engage, the battle rez) and the
drink-up call (`guild/habits.py`) then come from her; a lead he names later in such a raid speaks
them through `LivePorts.speaking_lead`. In talk, `RaidState.set_designated` makes her the lead
(callouts in combat, the wipe debrief) while no raid the mind leads is under way.

Her own callouts, in raid or party chat:

- **Ready check**: "rc", "everyone ready?", "ready?". Native `raid_control ready_check` with her as
  initiator once she is assistant (else him); she tells the raid in her words after the ack.
- **Pull countdown**: "pull", "pull in 5", "10 sec to pull" (1-10 s): "pulling in N", then "go".
  These two are fixed lines (like `guild/lines.py`'s countdown), never a model's.
- **Skull**: "kill skull", "everyone on skull": native `raid_lead mark` puts skull on his hostile
  target, set by her (assistant or party) or him, then the order sets the bots' rti to skull.
- **Wipe**: a boss wipe of his group's fight releases the members seen dead
  (`order release`) and she calls it ("release and run back") with the count the job confirmed.
- **The plan**: "what's the plan?", "how does this fight work?" (or asked of a member by name):
  she answers from code-built facts (who leads, the group's roles, the last order, the boss here)
  and `callouts.boss_plan`, which asks `callouts.boss_brief(context)` first: the single hook for
  the raid playbooks (`mind/leeroy_mind/raidplay/`, a later PR). It returns None for now, and
  the guild's standing pre-pull call (`guild/boss_calls.BOSS_CALLS`) answers instead.

## His orders

Everything he says in raid or party chat (and `raid_warning`), and his push-to-talk transcripts
from a room that is not private, is checked for an order from a **fixed list** the bodies can do
(`orders.ORDERS`): follow me, come back, stay here, stack on me or on a named member, spread out,
run out, back off (stop and fall back to him), hold / wait for my pull / stop attacking, attack
my target, kill skull, tanks swap (taunt), Bloodlust/Heroism now, release and run back.
Addressees: everyone, tanks, healers, damage, melee (damage), ranged (damage), or one member by
name; "taunt" defaults to the tanks, "lust" to a shaman of the group (her faction's spell).

- **Recognition**: deterministic patterns first (`orders.parse`). A line addressed to the group,
  a role or a member and led by an order word that no pattern resolves goes to the **live model**,
  only when it is already loaded and the GPU is not paused (`GpuPaused` and an unloaded model mean
  no model): one call at reply priority whose JSON schema's enums are the order list plus
  `none`, the addressee kinds and the names of his group, so it can never invent an action or a
  member; the answer is checked again in code. Confidence below 0.7 makes her ask one short
  question; a sure "none", no model or an invalid answer gives the line back to talk
  (`MindApp.release_to_talk`), as if never held. A line playerbots already takes as its own
  command (a first word such as follow, stay, attack, release, ready or tank; `his_command`) stays
  playerbots', so nothing is done twice. Questions are no orders; chatter never triggers one.
- **Execution**: mod-leeroy's `bot_do` `"order"` (or `"cast"` bloodlust/heroism) on the addressed
  cast members of his group only, behind the Realm, training, offline-raid and pending-job fences;
  native re-checks everything at the mutation boundary.
- **Acknowledgement only after the game confirms**: she (or the member he named, out of a fight)
  acknowledges in her own words wrapped around the facts of the job's terminal: who did it, who
  did not and why. No terminal, nothing is said. A refused or impossible order gets a plain
  "can't do that one, <reason>" (a fixed line, never the model), never a fake "on it".
- **Hold** ("wait for my pull") is released when his group engages a boss (a silent `follow`
  order, which clears `passive` and keeps them fighting).

**Fences.** Orders are his only, never from members' lines. Nothing runs while he is offline or
not leading a group, while a raid the mind leads (dungeon-clear) is under way, while a raid is
being formed or an offline raid holds the guild; the talk's existing rules (login quiet, raid
phases, guards, typing) apply to every line.

## Logs

`raidlead_set lead= source=`, `raidlead_cleared reason=`, `raidlead_assist lead= ok= error=`,
`raidlead_order action= count= done= outcome= error=`, `raidlead_refused reason=`,
`raidlead_unclear reason=`, `raidlead_model_order action= who=`, `raidlead_model_failed error=`,
`raidlead_failed error=`: ids, counts and codes only, never his words, names or persona text.
