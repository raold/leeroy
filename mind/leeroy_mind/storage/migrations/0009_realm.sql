CREATE TABLE realm_guild(id INTEGER PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('progression','gankers','drama')),
  faction TEXT NOT NULL CHECK(faction IN ('horde','alliance')), name TEXT NOT NULL, added REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired')));
CREATE TABLE realm_actor(guid INTEGER PRIMARY KEY, guild INTEGER NOT NULL, name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('officer','ganker','applicant')), added REAL NOT NULL,
  place TEXT NOT NULL DEFAULT '', placed REAL,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired')));
CREATE TABLE realm_raider(guild INTEGER NOT NULL, guid INTEGER NOT NULL, class INTEGER NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('tank','healer','melee','ranged')), avg_ilvl INTEGER NOT NULL, checked REAL NOT NULL,
  PRIMARY KEY(guild, guid));
CREATE TABLE realm_fact(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL CHECK(length(text) <= 200),
  source TEXT NOT NULL CHECK(source IN ('event','channel','rumor','rule','benchmark','night')), ref TEXT NOT NULL DEFAULT '',
  public INTEGER NOT NULL DEFAULT 1, ts REAL NOT NULL);
CREATE TABLE realm_knows(fact INTEGER NOT NULL, guild INTEGER NOT NULL, since REAL NOT NULL,
  how TEXT NOT NULL CHECK(how IN ('present','server','forum','trade','own')), PRIMARY KEY(fact, guild));
CREATE TABLE realm_progress(guild INTEGER NOT NULL, map INTEGER NOT NULL, boss_id INTEGER NOT NULL, difficulty INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('none','progressing','killed')), best_pct INTEGER NOT NULL DEFAULT 100,
  killed_at REAL, kill_s INTEGER, source TEXT NOT NULL CHECK(source IN ('rule','benchmark','canon')), updated REAL NOT NULL,
  PRIMARY KEY(guild, map, boss_id, difficulty));
CREATE TABLE realm_grudge(guild INTEGER NOT NULL, guid INTEGER NOT NULL, points REAL NOT NULL, last REAL NOT NULL,
  reason TEXT NOT NULL, PRIMARY KEY(guild, guid));
CREATE TABLE realm_post(id INTEGER PRIMARY KEY, ts REAL NOT NULL, author INTEGER NOT NULL,
  author_kind TEXT NOT NULL CHECK(author_kind IN ('member','actor')), thread TEXT NOT NULL,
  text TEXT NOT NULL CHECK(length(text) <= 400), fact INTEGER);
CREATE TABLE realm_line(id INTEGER PRIMARY KEY, ts REAL NOT NULL, guid INTEGER NOT NULL, guild INTEGER NOT NULL,
  channel TEXT NOT NULL, text TEXT NOT NULL, dropped TEXT);
CREATE TABLE realm_issue(id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, title TEXT NOT NULL, opened REAL NOT NULL,
  closed REAL, outcome REAL, status TEXT NOT NULL DEFAULT 'live' CHECK(status IN ('live','settled')));
CREATE TABLE realm_stance(issue INTEGER NOT NULL, guid INTEGER NOT NULL, value REAL NOT NULL CHECK(value BETWEEN -1 AND 1),
  anchor REAL NOT NULL, stubborn REAL NOT NULL, updated REAL NOT NULL, PRIMARY KEY(issue, guid));
CREATE TABLE realm_plan(day TEXT NOT NULL, guid INTEGER NOT NULL, idx INTEGER NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
  activity TEXT NOT NULL, place TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'planned'
  CHECK(state IN ('planned','running','done','skipped','preempted')), PRIMARY KEY(day, guid, idx));
CREATE TABLE realm_event(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, organiser INTEGER NOT NULL, start REAL NOT NULL,
  end REAL NOT NULL, place TEXT NOT NULL, calendar_id INTEGER, result TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT 'posted' CHECK(state IN ('posted','gathering','running','scored','ended','cancelled')));
CREATE TABLE realm_rsvp(event INTEGER NOT NULL, guid INTEGER NOT NULL, status TEXT NOT NULL, ts REAL NOT NULL,
  PRIMARY KEY(event, guid));
CREATE TABLE realm_calendar(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, creator INTEGER NOT NULL, at REAL NOT NULL,
  title TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '', updated REAL NOT NULL);
CREATE TABLE realm_mail(id INTEGER PRIMARY KEY, ts REAL NOT NULL, sender INTEGER NOT NULL, kind TEXT NOT NULL,
  subject TEXT NOT NULL, body TEXT NOT NULL, items TEXT NOT NULL DEFAULT '[]', item_guid INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','sent','taken','failed')), taken REAL);
CREATE TABLE realm_bank(id INTEGER PRIMARY KEY, ts REAL NOT NULL, dir TEXT NOT NULL CHECK(dir IN ('in','out')),
  reason TEXT NOT NULL, guid INTEGER NOT NULL, item_id INTEGER NOT NULL, count INTEGER NOT NULL, money INTEGER NOT NULL DEFAULT 0);
CREATE TABLE realm_arena(team_id INTEGER PRIMARY KEY, side TEXT NOT NULL CHECK(side IN ('cast','rival','social')),
  guild INTEGER NOT NULL, type INTEGER NOT NULL CHECK(type IN (2,3,5)), captain INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE realm_match(id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('arena','wg','duel','gank','benchmark')),
  winner TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}');
CREATE TABLE realm_recruit(id INTEGER PRIMARY KEY, guid INTEGER NOT NULL UNIQUE, name TEXT NOT NULL, persona TEXT NOT NULL,
  applied REAL NOT NULL, asked REAL, answered REAL, answer TEXT, trial_until REAL,
  state TEXT NOT NULL CHECK(state IN ('applied','asked','admitted','declined','withdrawn')));
CREATE TABLE realm_step(name TEXT NOT NULL, sha256 TEXT NOT NULL, approved REAL NOT NULL, applied REAL,
  result TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(name, sha256));
CREATE TABLE realm_canon(achievement INTEGER NOT NULL, guid INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '', ts REAL NOT NULL,
  tries INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL CHECK(state IN ('planned','granted','refused','blocked')), PRIMARY KEY(achievement, guid));
CREATE TABLE realm_canon_kill(map INTEGER NOT NULL, boss_id INTEGER NOT NULL, difficulty INTEGER NOT NULL,
  moment TEXT NOT NULL, day TEXT NOT NULL, PRIMARY KEY(map, boss_id, difficulty));
CREATE TABLE realm_kick(guid INTEGER PRIMARY KEY, ts REAL NOT NULL, rank INTEGER NOT NULL, asked REAL,
  answer TEXT CHECK(answer IN ('yes','no')), answered REAL);
CREATE TABLE realm_meeting(id INTEGER PRIMARY KEY, week TEXT NOT NULL UNIQUE, mode TEXT NOT NULL CHECK(mode IN ('live','night')),
  agenda TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT '{}', mail INTEGER);
CREATE TABLE realm_goal(achievement INTEGER PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('open','done','dropped')),
  progress TEXT NOT NULL DEFAULT '{}', updated REAL NOT NULL);
CREATE TABLE realm_notable(day TEXT NOT NULL, guid INTEGER NOT NULL, memory INTEGER NOT NULL, PRIMARY KEY(day, guid));

-- Bounded diagnostics only; never evidence of movement, ownership or release.
CREATE TABLE realm_receipt(seq INTEGER PRIMARY KEY, command_id TEXT NOT NULL, kind TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('ack','job')), ok INTEGER NOT NULL CHECK(ok IN (0,1)),
  reason TEXT NOT NULL, value TEXT NOT NULL DEFAULT '0', ts REAL NOT NULL,
  UNIQUE(command_id,stage));
