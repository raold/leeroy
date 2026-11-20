"""The Ops switchboard's one table: every boolean switch mind.toml's loaders define, plus the
few numbers worth a glance. tests/console/test_console_switches.py walks the loaders and fails
when a boolean key is neither a row here nor in EXCLUDED, so a new switch cannot be forgotten.

A row: (table, key, label, needs[, default or NUMBER]). `needs` is '; '-separated; a part that
names another row ("learn.enabled") is checked against that row's state, the rest are words.
A boolean row's default is false unless a fifth value says otherwise."""

NUMBER = "number"

SWITCHES = (
    ('talk','aware','Awareness cards',''), ('guards','enabled','Guards',''),
    ('guards','semantic_enabled','Semantic guard checks','guards.enabled'),
    ('guards','phrase_cooldown','Phrase cooldown','guards.enabled',True),
    ('claims','enabled','Claims',''), ('talk','realism','Typing realism',''),
    ('talk','texture','Typing texture',''),
    ('talk','recall_diversity','Recall diversity',''),
    ('talk','think_private','Private beat before answers to him','',True),
    ('talk','coherence','Conversations hold together (read at start and at his login)',''),
    ('talk','coherence_length','Long game answers kept whole (read at start and at his login)',''),
    ('talk','intent','Her want for each turn',''),
    ('talk','situation','Situation prompts',''),
    ('talk','shape','Reply shapes',''),
    ('talk','dm_mirror','DM length mirror (0-10)','talk.shape',NUMBER),
    ('talk','dm_own_time','DM answers on her own time',''),
    ('life','availability','Availability (0-10; the world dial overrides it)','',NUMBER),
    ('life','state','Her feeling through the day',''),
    ('signals','enabled','Signals',''), ('fingerprint','enabled','Typing fingerprint',''),
    ('callbacks','enabled','Memory callbacks',''), ('learn','enabled','Rewards',''),
    ('learn','bandit','Bandit credit','learn.enabled'), ('learn','coach','Talk coach','learn.enabled'),
    ('learn','auto_lessons','Lessons approved automatically','learn.coach'),
    ('learn','capture_preferences','Preference capture','learn.enabled'),
    ('learn','apply','Learned arms and lessons in live talk','learn.enabled'),
    ('learn','cycle','Weekly learning cycle','learn.enabled'),
    ('banter','enabled','Banter',''),
    ('server','enabled','24/7 server','nights only inside its quiet window'),
    ('server','reactions','Server reactions','server.enabled',True),
    ('groups','enabled','Group DMs','restart the console too'),
    ('culture','enabled','Culture: running jokes','server.enabled'),
    ('culture','calendar','Culture: real game events','culture.enabled; Leeroy.Realm.Enable = 1'),
    ('culture','loot','Culture: loot talk','culture.enabled'),
    ('culture','bots','Culture: server bots','culture.enabled'),
    ('culture','emoji','Culture: role and class emoji','culture.enabled'),
    ('culture','memes','Culture: memes','culture.enabled'),
    ('outreach','enabled','Outreach','restart the console too'),
    ('godmode','enabled','Godmode',''),
    ('decisions','enabled','Decisions','server.enabled'),
    ('voice','enabled','Voice comms','the voice service'),
    ('voice','autostart','Voice service autostart','voice.enabled'),
    ('voice_notes','enabled','Voice notes','restart voice, mind and console together'),
    ('romance','activities','Romance activities','realm.enabled; native rebuild'),
    ('romance','realism','Romance realism',''),
    ('heat','enabled','Heat: one dial for how far romance goes',
     'restart the console too; governs nobody until a world heat is set'),
    ('heat','public_cap','Heat: channels others read capped at flirty','heat.enabled'),
    ('drama','member_romance','Member romances',''),
    ('photos','enabled','Discord photos','restart the console too'), ('selfies','enabled','Selfies',''),
    ('photos','send','Members send photos','selfies.enabled; selfies.snaps; server.enabled'),
    ('photos','requests','Selfie requests','photos.send; selfies.enabled; selfies.snaps; server.enabled'),
    ('photos','scene_from_words','Selfie scenes from his words','photos.requests'),
    ('selfies','snaps','Situational photos','selfies.enabled; identity adapter fetched'),
    ('selfies','booth','Photo booth','selfies.enabled'), ('looks','diversity','Looks diversity',''),
    ('selfies','variety','Photo style variety','selfies.enabled'),
    ('selfies','romance_styling','Private photo romance styling','selfies.enabled; selfies.snaps'),
    ('selfies','varied_sets','Varied selfie sets','selfies.enabled'),
    ('selfies','identity_sets','Her face in every new set','selfies.enabled; identity adapter fetched'),
    ('selfies','detailer','Photo detailer: faces','selfies.enabled'),
    ('selfies','detailer_hands','Photo detailer: hands','selfies.detailer; palm and handpose models fetched'),
    ('selfies','critic','Photo critic','selfies.enabled; critic_model pulled by the operator'),
    ('selfies','param_bandit','Photo parameter bandit','selfies.critic'),
    ('personas','auto_newcomers','Newcomer personas',''),
    ('tuning','nudge_memories','Ended nudges offered to reflection','',True),
    ('world','enabled','World personas','realm.enabled; Leeroy.World.Enable = 1'),
    ('world','realm','World personas in the Realm','world.enabled'),
    ('world','selfies','World persona selfies','world.enabled; selfies.enabled'),
    ('playstyle','enabled','Play styles','Leeroy.PlayStyle.Enable = 1'),
    ('realm','enabled','The Realm','Leeroy.Realm.Enable = 1; realm/blocklist.txt'),
    ('realm','gank_human','Gankers may attack him','realm.enabled',True),
    ('training','enabled','Training',''),
    ('training','telemetry','Training telemetry','training.enabled',True),
    ('training','act','Training acts on what it learns','training.enabled',True),
    ('training','experiments','Training experiments','training.enabled',True),
    ('training','lab','Nightly training lab','training.enabled',True),
    ('training','spread','Training spread','training.enabled',True),
    ('training','benchmark','Training benchmarks','training.enabled',True),
    ('training','lead_voice','Training lead voice','training.enabled',True),
    ('training','playbooks','Raid playbook checks','training.enabled'),
    ('control','enabled','Control endpoint','',True),
    ('control','ops_actions','Ops actions','restart the console too'),
    ('raidlead','enabled','Raid lead','Leeroy.RaidLead.Enable = 1; native rebuild'),
    ('raidlead','briefs','Boss briefs','raidlead.enabled'),
    ('raidplay','respond','Mechanic responder','training.playbooks; Leeroy.Mechanics.Enable = 1'),
    ('turing','enabled','Turing judge',''),
    ('bakeoff','capture','Bake-off capture (stores full prompts, 14 days)',''),
    ('prune','enabled','Nightly retention prune',''),
    ('lab','enabled','Conversation lab','learn.bandit and learn.apply for its arms to reach live talk'),
    ('lab','promote','Lab winners proposed to the weekly gate','lab.enabled; grader = "tells" (refused with the rubric)'),
)

# Boolean keys the loaders define that are deliberately not rows, each with its reason.
EXCLUDED = {
    ('learn','freeze'): 'a state, not a feature: the Lab tab shows and explains every freeze',
    ('lab','overlays'): "set only in a sandbox's copied mind.toml; the live mind leaves it off",
}

# mind.toml tables with no boolean switch at all (the completeness test names each).
NO_SWITCHES = frozenset({'console','guild','dkp','health','models','cast','memory','bridge','ollama','human'})


def default(row):
    return row[4] if len(row) > 4 and row[4] is not NUMBER else False


def is_number(row):
    return len(row) > 4 and row[4] is NUMBER
