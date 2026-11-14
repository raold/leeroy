# The raid lead

He keeps his group: he stays its game leader (`.playerbots bot add25` and its refill need him as
leader, a bot with the leader flag makes playerbots reset every bot's strategies, and the leader
keeps loot master). The raid lead is a cast member of his group who **calls** it, and his orders
to the group are understood and carried out by the bodies. No raid assistant flag is ever set:
her raid icons are set server-side, so playerbots' assist-tank and assist-healer orders never shift.

## Switches

- `[raidlead] enabled = true` in `mind.toml` (default false; a table with any other key or a
  non-boolean value, or an unreadable `mind.toml`, is off). Read at the mind's start: restart the
  mind after changing it. Off, nothing is attached: no handler, task, op or prompt, and his lines
  go to talk as before.
- `[raidlead] briefs = true` (default false; only with `enabled`; a non-boolean value makes the
  whole table invalid, so the raid lead is off): the boss briefs below. Read at the mind's start.
  Off, the pre-pull call and his plan questions are exactly what they were.
- Native `Leeroy.RaidLead.Enable = 1` in `mod_leeroy.conf` (default 0, `.reload config`); see
  `modules/mod-leeroy/README.md`. The mind asks it: a `sync` at each hello (which also has native
  publish his group, so the group is known at once) and a `probe` a minute while he plays; any
  `raidlead_off` refusal counts too. While native is off or unknown, or the worldserver lacks the
  `raidlead` hello feature, **nothing here leads or speaks**: no default lead, no talk lead, no
  lead recorded for his adopted raid, and the raid calls through her stop. Native refuses every
  raid lead action then, Bloodlust included, and lifts any hold at once.

## Who leads

A raid lead is a cast member of his current group, chosen:

1. in the console's Raid tab (the **Raid lead** picker: a member, "me", or "the guild's pick"),
   through the control endpoint's `raidlead` op (`{"action": "set", "guid"}` or
   `{"action": "clear"}`), behind the console key, Origin and `--no-control` gates;
2. by him in raid or party chat, or by push-to-talk in a room that is not private, with an
   explicit designation only: "Ana, you're leading tonight", "Ana leads", "Ana is raid lead",
   "you lead, Ana", "raid lead: Ana", "let Ana lead", "Ana, take the lead". "I'll lead", "I'm
   leading" or picking "me" takes it himself (nobody calls it, no default). "I'm calling it" never
   changes anything;
3. by default, when he named nobody: the officer in his group (online, with a persona) with the
   most raid nights led (`guild_raid` rows of raid nights the mind called: offline raids, and his
   own groups' raids she led because he named her, `guild:raid:<id>:named-lead`, aside; the same
   exclusion keeps them out of `last_led`'s rotation), then the most attended (`guild_attendance`
   'raided'), then a "raid lead" officer title, then the lowest guid (`designate.py`). A group with
   no officer has no default lead. The default is silent.

It lasts until he changes it or the group breaks: he leaves it, it disbands (`group` events), or
she leaves it (`group`/`guild_group` events). Anything still queued for the old group is dropped.
It persists in meta `raidlead:state` (lead, source, his own lead, his group's members and raid
flag, and the members on hold), so a restart keeps it.

## What she does

The designation feeds guild life: his own add-built group adopted as a raid night
(`guild/raid_night.py` `_adopt`) records her as the raid's lead (`plan.lead_guid`, the
`guild_raid` row, the talk's lead) while `RaidRun.lead_is_human` stays true: he drives it, so no
automatic ready check, countdown or dungeon-clear. The existing raid calls (`guild/calls.py`
`RaidCalls`: marks and the skull rti, the soulstone, Bloodlust on engage, the battle rez) and the
drink-up call (`guild/habits.py`) then come from her; a lead he names later in such a raid speaks
them through `LivePorts.speaking_lead`. When he takes it back ("I'll lead", "me"), names someone
outside that raid, or the raid lead stops, `speaking_lead` returns him and the talk's lead is his
again at once: nobody calls. In talk, `RaidState.set_designated` makes her the lead (callouts in
combat, the wipe debrief) while no raid the mind leads is under way.

Her own callouts, in raid or party chat:

- **Ready check**: "rc" or "ready check" said as a command. Native `raid_control ready_check` with
  him as its leader; she tells the raid in her words after the ack.
- **Pull countdown**: "pull in 5", "pulling in 5", "10 sec to pull" (1-10 s): "pulling in N", then
  "go". These two are fixed lines (like `guild/lines.py`'s countdown), never a model's.
- **Skull**: "kill skull": native `raid_lead mark` puts skull on his hostile target, set by her (or
  him), then the order sets the bots' rti to skull.
- **Wipes**: no release unless he orders it ("release and run back"): inside an instance the dead
  wait for a rez as GroupRez rules. The wipe's one line is the talk's own debrief, from her.
- **The plan**: "what's the plan?", "how does this fight work?" (or asked of a member by name):
  she answers from code-built facts (who leads, the group's roles, the last order, the boss here)
  and the guild's standing pre-pull call for it (`callouts.boss_plan`, `guild/boss_calls.BOSS_CALLS`).
  With the briefs on she answers with the boss's brief instead (below).

## Boss briefs (`[raidlead] briefs`)

A brief is the boss's plan built by code from the raid playbooks (`briefs.py`, read through the
hook `callouts.boss_brief(context)`), never by a model, so no mechanic or tactic in it is made up:
the playbook's 2-4 mechanics that matter most to his group at that difficulty, each said with its
own playbook line (`explain`, whole and unchanged) after a header naming the boss and difficulty
("Lord Jaraxxus, 25 heroic: ..."). Which ones: those only this difficulty has first (Jaraxxus's
attackable volcano and portal on heroic), then by kind (adds and kill order, interrupts, ground to
leave, run-outs and soaks, stacking and spreading, positions, then one role's job, then cooldowns
and enrage), then by how many of his group each concerns, then the playbook's order. A mechanic is
kept only when his group has one of its roles (tank, healer, melee, ranged; his own role counts);
one that is only the tanks' only when tanks are named in his group. At most 3 raid chat lines of
at most 255 bytes; a mechanic that does not fit whole is left out, never cut.

- **Before the pull**: guild life's pre-pull call (`guild/calls.RaidCalls`, on dungeon-clear's next
  boss in a raid night) says the brief in place of the standing call, from the run's lead, its
  lines word for word (each an exact line: typed, unstyled, never voiced). One call under the
  calls' 20 s gap, said outside the calls' lock so a pull's Bloodlust never waits behind it; a pull
  starting, or the run ending, stops the lines not yet sent. The training ground's own pre-pull
  call still wins over it.
- **His questions**: "what's the plan?" for the boss here (the last his group engaged on his map),
  or one he names: "what do we do on heigan", "what's the plan for the lich king?", "how does
  sindragosa work?", "explain festergut", "anub'arak strat?" (a boss by its name, its playbook
  slug or a word of its name no other boss shares; a named-boss question is talk while the briefs
  are off). She posts the brief's lines word for word, then her own words in one short line from
  the other facts (lead, roles, last order), told not to repeat the plan or name a mechanic. One
  brief at a time; the same boss's not again within 20 s.
- **Difficulty**: from the raid he drives (an adopted add-built group), else his group's last
  `guild_group` event inside an instance on his map (`entered`, which carries the difficulty).
  Not known (a group he formed by hand, or a boss he names in another raid), no brief.
- **Fallback**: no playbook for the boss (Ulduar and the small raids today), the difficulty not
  known, nobody in his group the mechanics are for, or the playbook data unreadable (logged once,
  `raidlead_brief_unavailable error=`): the standing call, exactly as with the briefs off.

## His orders

The fixed list (`orders.ORDERS`): follow me, come back, stay here, stack on me or on a named
member, spread out, run out, back off (to his side), hold / wait for my pull / stop attacking,
attack my target, kill skull, tanks swap (taunt), Bloodlust/Heroism now, release and run back.
Addressees: everyone/all/raid, tanks, healers, dps (damage), melee (damage), ranged (damage), or
one member by name; "taunt" defaults to the tanks, "lust" to a shaman of the group.

- **An order needs an explicit addressee or an unambiguous phrase.** "dps, hold", "everyone stack
  on me", "Ana, run out" are orders; so are a few whole phrases with no everyday meaning: "wait
  for my pull", "stop attacking", "hold your dps", "stack on me" (or on a member), "spread out",
  "tanks swap", "lust now", "back off", "fall back", "kill skull", "release and run back",
  "come back to me", "regroup on me", "hold position", "attack my target". A single word or a
  short everyday phrase ("stop", "hold on", "go", "focus", "get back", "range up", "with me",
  "on him", "you ready?") is never an order, typed or spoken. Questions are no orders.
- **Before playerbots' words.** His raid and party lines that match this vocabulary are the raid
  lead's even when they begin with a playerbots word ("wait for my pull", "dps, hold", "tank
  swap"); every other playerbots command ("follow", "stay", "attack", "release" alone) passes
  through as before.
- **The model** reads an addressed line led by an order word that no pattern resolves ("everyone
  get behind the boss"), only when the live model is already loaded and the GPU is not paused:
  one call at reply priority whose JSON schema's enums are the order list plus `none`, the
  addressee kinds and the names of his group, so it can never invent an action or a member; the
  answer is checked again in code. The reading runs beside the worker; his next line sends any
  line still being read to talk first, so his lines reach talk in the order he wrote them.
  Confidence below 0.7 makes her ask one short question; a sure "none", no model or an invalid
  answer gives the line back to talk (`MindApp.release_to_talk`), and the signals then see talk,
  not a command.
- **Execution**: mod-leeroy's `bot_do` `"order"` on the addressed cast members of his group only
  (Bloodlust/Heroism is the order `lust` on one shaman, so native's switch covers it), behind the
  Realm, training, offline-raid and pending-job fences; native re-checks everything.
- **Only a hold is passive, and only for damage dealers.** Tanks and healers are never put on
  hold: "everyone hold" holds the damage dealers, "Ana, hold" for a healer or "tanks, hold" is
  refused in words, and native refuses them too. Spread, run out and back off are single moves
  (playerbots' own "move from group", or a move to his side) that leave nothing passive behind.
  A hold lifts on the first of: his group's fight starting (his pull) or ending, a later stay,
  stack, follow, come back or attack order on that member, or 30 seconds. Native lifts it itself
  on its world tick (a bot between maps or logged out keeps its hold until it is back, then it is
  lifted; never dropped unlifted); the mind also sends a `lift` order on a boss engage, after 30
  seconds and at each hello (the members on hold persist in meta), and retries one a member
  skipped after 5 s, doubling up to 2 minutes, never while the bridge is down.
- **Each line of his is carried out once.** A member playerbots already released (his typed
  "release ..." reaches playerbots too) counts as released, so there is no second line about it.
- **Acknowledgement only after the game confirms**: she (or the member he named, out of a fight)
  acknowledges in her own words wrapped around the facts of the job's terminal: who did it, who
  did not and why. No terminal, nothing is said. A refused or impossible order gets a plain
  "can't do that one, <reason>" (a fixed line, never the model), never a fake "on it". An
  addressee he named who is not here is refused by name, never replaced by someone else. A bot
  already on his target, or one that cannot see it yet, counts as attacking.

**Fences.** Orders are his only, never from members' lines, whispers, Discord, DMs or group DMs.
Nothing runs while he is offline or not leading a group, while a raid the mind leads
(dungeon-clear) is under way, while a raid is being formed or an offline raid holds the guild;
the talk's existing rules (login quiet, raid phases, guards, typing) apply to every line. A
failure inside the check gives the line to talk.

## Logs

`raidlead_set lead= source=`, `raidlead_cleared reason=`, `raidlead_native on=`,
`raidlead_order action= count= done= outcome= error=`, `raidlead_lift why= count= done=`,
`raidlead_lift_dropped guid= reason=`, `raidlead_refused reason=`, `raidlead_unclear reason=`,
`raidlead_model_order action= who=`, `raidlead_model_failed error=`, `raidlead_claim_failed error=`,
`raidlead_failed error=`, `raidlead_brief context=MAP:BOSS:DIFFICULTY lines= via=prepull|question
outcome=said|cut|dropped|recent`, `raidlead_brief_unavailable error=`; native
`raidlead_hold_lifted guid=`. Ids, counts and codes only, never his words, names or persona text.
