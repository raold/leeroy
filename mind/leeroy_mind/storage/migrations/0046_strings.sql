-- The owner's strings (D6, godmode): an intent he gives a member, carried out in her own way or
-- reported back as not taken with a reason code. Part A runs the Discord kinds; the game kinds
-- are listed for the native strings that follow. args is the kind's JSON (his screened text
-- stays in mind.db, never a log); change ties it to the tuning change log when one made it.
-- day_plan (D6 Part C) joins this file at that part's merge.
CREATE TABLE godmode_intent (
  id INTEGER PRIMARY KEY, member INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('post','thread','react','dm_him','bring_up','come_to_me','join_group',
                                     'follow','duel_me','emote','sit')),
  args TEXT NOT NULL CHECK (json_valid(args)),
  status TEXT NOT NULL CHECK (status IN ('pending','taken','not_taken','expired','cancelled')),
  reason TEXT, created REAL NOT NULL, due REAL NOT NULL, settled REAL, change INTEGER REFERENCES tuning_change(id));
CREATE INDEX godmode_intent_pending ON godmode_intent(status, member, due);
