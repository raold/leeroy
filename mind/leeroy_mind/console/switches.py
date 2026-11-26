"""The Ops switchboard's one table: every boolean switch mind.toml's loaders define, plus the
few numbers worth a glance. tests/console/test_console_switches.py walks the loaders and fails
when a boolean key is neither a row here nor in EXCLUDED, so a new switch cannot be forgotten.

A row: (table, key, label, needs[, default or NUMBER]). `needs` is '; '-separated; a part that
names another row ("learn.enabled") is checked against that row's state, the rest are words.
A boolean row's default is false unless a fifth value says otherwise."""

NUMBER = "number"

SWITCHES = (
    ('watchdog','enabled','Capture event-loop stalls','mind restart',True),
    ('talk','aware','Awareness cards','',True), ('guards','enabled','Guards','',True),
    ('guards','semantic_enabled','Semantic guard checks','guards.enabled',True),
    ('guards','phrase_cooldown','Phrase cooldown','guards.enabled',True),
    ('claims','enabled','Claims','',True), ('talk','realism','Typing realism','',True),
    ('talk','texture','Typing texture','',True),
    ('talk','combat_quiet','Quiet ambient game chat during combat','native combat_quiet feature; native rebuild',True),
    ('talk','recall_diversity','Recall diversity','',True),
    ('talk','think_private','Private beat before answers to him','',True),
    ('talk','coherence','Conversations hold together (read at start and at his login)','',True),
    ('talk','coherence_length','Long game answers kept whole (read at start and at his login)',''),
    ('talk','wow_grounding','Raid and guild facts and her own lines in WoW talk','',True),
    ('talk','wow_topic','WoW talk stays on the game: chat register, short game lines, DKP bids','',True),
    ('talk','intent','Her want for each turn','',True),
    ('talk','situation','Situation prompts','',True),
    ('talk','shape','Reply shapes','',True),
    ('talk','dm_mirror','DM length mirror (0-10)','talk.shape',NUMBER),
    ('talk','dm_own_time','DM answers on her own time',''),
    ('talk','invites','His invitations in game carried out','Leeroy.Invites.Enable = 1',True),
    ('talk','lead','His "you lead" in game carried out','Leeroy.Lead.Enable = 1',True),
    ('life','availability','Availability (0-10; the world dial overrides it)','',NUMBER),
    ('life','state','Her feeling through the day','',True),
    ('life','day_plans','Member day plans','realm.enabled; Leeroy.DayPlans.Enable = 1',True),
    ('life','dungeons','Members run heroics','server.enabled; Leeroy.MemberDungeon.Enable = 1',True),
    ('signals','enabled','Signals','',True), ('fingerprint','enabled','Typing fingerprint','',True),
    ('callbacks','enabled','Memory callbacks','',True), ('learn','enabled','Rewards','',True),
    ('learn','bandit','Bandit credit','learn.enabled',True), ('learn','coach','Talk coach','learn.enabled',True),
    ('learn','auto_lessons','Lessons approved automatically','learn.coach',True),
    ('learn','capture_preferences','Preference capture','learn.enabled',True),
    ('learn','apply','Learned arms and lessons in live talk','learn.enabled',True),
    ('learn','cycle','Weekly learning cycle','learn.enabled',True),
    ('banter','enabled','Banter','',True),
    ('server','enabled','24/7 server','nights only inside its quiet window',True),
    ('server','reactions','Server reactions','server.enabled',True),
    ('server','channel_topics','Discord channels keep to their topics','server.enabled',True),
    ('groups','enabled','Group DMs','restart the console too',True),
    ('culture','enabled','Culture: running jokes','server.enabled',True),
    ('culture','calendar','Culture: real game events','culture.enabled; Leeroy.Realm.Enable = 1',True),
    ('culture','loot','Culture: loot talk','culture.enabled',True),
    ('culture','bots','Culture: server bots','culture.enabled',True),
    ('culture','emoji','Culture: role and class emoji','culture.enabled',True),
    ('culture','memes','Culture: memes','culture.enabled',True),
    ('outreach','enabled','Outreach','restart the console too',True),
    ('godmode','enabled','Godmode','',True),
    ('geography','enabled','Geography: her place and clock, his home in private','',True),
    ('decisions','enabled','Decisions','server.enabled',True),
    ('decisions','rank_reviews','Decisions: officers review ranks (polls, then SetRank)','decisions.enabled',True),
    ('decisions','boss_strategy','Decisions: boss strategy after two wipes (training changes)',
     'decisions.enabled; training.enabled; Leeroy.Training.Enable = 1',True),
    ('decisions','bank_requests',"Decisions: members' bank requests",
     'decisions.enabled; realm.enabled; Leeroy.Decisions.BankMail.Enable = 1 to hand items over',True),
    ('voice','enabled','Voice comms','the voice service',True),
    ('voice','autostart','Voice service autostart','voice.enabled',True),
    ('voice_notes','enabled','Voice notes','restart voice, mind and console together',True),
    ('romance','activities','Romance activities','realm.enabled; native rebuild',True),
    ('romance','realism','Romance realism','',True),
    ('heat','enabled','Heat: one dial for how far romance goes',
     'restart the console too; governs nobody until a world heat is set',True),
    ('heat','public_cap','Heat: channels others read capped at flirty','heat.enabled'),
    ('drama','member_romance','Member romances','',True),
    ('photos','enabled','Discord photos','restart the console too',True), ('selfies','enabled','Selfies','',True),
    ('videos','enabled','Discord videos from him','photos.enabled; ffmpeg and ffprobe on PATH; restart the console too',True),
    ('photos','send','Members send photos','selfies.enabled; selfies.snaps; server.enabled',True),
    ('photos','requests','Selfie requests','photos.send; selfies.enabled; selfies.snaps; server.enabled',True),
    ('photos','request_priority','Priority selfie requests while playing','photos.requests; chat waits during rendering',True),
    ('photos','scene_from_words','Selfie scenes from his words','photos.requests',True),
    ('photos','unsolicited','Unsolicited selfies from romantic members','photos.requests; renders only while the GPU is idle',True),
    ('selfies','snaps','Situational photos','selfies.enabled; identity adapter fetched',True),
    ('selfies','booth','Photo booth','selfies.enabled',True), ('looks','diversity','Looks diversity','',True),
    ('selfies','variety','Photo style variety','selfies.enabled',True),
    ('selfies','romance_styling','Private photo romance styling','selfies.enabled; selfies.snaps',True),
    ('selfies','varied_sets','Varied selfie sets','selfies.enabled',True),
    ('selfies','identity_sets','Her face in every new set','selfies.enabled; identity adapter fetched',True),
    ('selfies','detailer','Photo detailer: faces','selfies.enabled',True),
    ('selfies','detailer_hands','Photo detailer: hands','selfies.detailer; palm and handpose models fetched',True),
    ('selfies','critic','Photo critic','selfies.enabled; critic_model pulled by the operator',True),
    ('selfies','param_bandit','Photo parameter bandit','selfies.critic',True),
    ('personas','auto_newcomers','Newcomer personas','',True),
    ('tuning','nudge_memories','Ended nudges offered to reflection','',True),
    ('world','enabled','World personas','realm.enabled; Leeroy.World.Enable = 1',True),
    ('world','realm','World personas in the Realm','world.enabled',True),
    ('world','selfies','World persona selfies','world.enabled; selfies.enabled',True),
    ('playstyle','enabled','Play styles','Leeroy.PlayStyle.Enable = 1',True),
    ('realm','enabled','The Realm','Leeroy.Realm.Enable = 1; realm/blocklist.txt',True),
    ('realm','gank_human','Gankers may attack him','realm.enabled',True),
    ('training','enabled','Training','',True),
    ('training','telemetry','Training telemetry','training.enabled',True),
    ('training','act','Training acts on what it learns','training.enabled',True),
    ('training','experiments','Training experiments','training.enabled',True),
    ('training','lab','Nightly training lab','training.enabled',True),
    ('training','spread','Training spread','training.enabled',True),
    ('training','benchmark','Training benchmarks','training.enabled',True),
    ('training','lead_voice','Training lead voice','training.enabled',True),
    ('training','playbooks','Raid playbook checks','training.enabled',True),
    ('control','enabled','Control endpoint','',True),
    ('control','ops_actions','Ops actions','restart the console too',True),
    ('raidlead','enabled','Raid lead','Leeroy.RaidLead.Enable = 1; native rebuild',True),
    ('raidlead','briefs','Boss briefs','raidlead.enabled',True),
    ('raidplay','respond','Mechanic responder','training.playbooks; Leeroy.Mechanics.Enable = 1',True),
    ('turing','enabled','Turing judge','',True),
    ('bakeoff','capture','Bake-off capture (stores full prompts, 14 days)','',True),
    ('prune','enabled','Nightly retention prune','',True),
    ('lab','enabled','Conversation lab','learn.bandit and learn.apply for its arms to reach live talk',True),
    ('lab','promote','Lab winners proposed to the weekly gate','lab.enabled; grader = "tells" (refused with the rubric)'),
)

# Boolean keys the loaders define that are deliberately not rows, each with its reason.
EXCLUDED = {
    ('learn','freeze'): 'a state, not a feature: the Lab tab shows and explains every freeze',
    ('lab','overlays'): "set only in a sandbox's copied mind.toml; the live mind leaves it off",
}

# mind.toml tables with no boolean switch at all (the completeness test names each).
NO_SWITCHES = frozenset({'console','guild','dkp','health','models','cast','memory','bridge','ollama','human',
                         'owner'})


def default(row):
    return row[4] if len(row) > 4 and row[4] is not NUMBER else False


def is_number(row):
    return len(row) > 4 and row[4] is NUMBER
