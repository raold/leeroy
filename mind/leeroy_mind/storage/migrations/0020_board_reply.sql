-- Board replies (P3): the owner's replies, from the guild console, to a Realm forum post or a
-- letter in his mailbox. Only the running mind writes them: its control endpoint's `board` op
-- stores one `waiting` (leeroy_mind/board.py), and the Realm night takes it up and settles it.
--
-- kind and parent: what he replied to, a realm_post id ('post') or a realm_mail id ('letter');
-- to_guid: that post's author or that letter's sender. text is already cleaned (board.clean_reply):
-- at most 200 characters for the forum, 400 for a letter.
-- state: 'waiting' until the night or he settles it, then 'answered' (the answer is answer_post
-- or answer_mail), 'posted' (his forum reply stands, nobody answered), 'refused' (its text did
-- not pass the night's screen; reason says why, and it is never retried) or 'withdrawn' (he took
-- it back while it waited). night is the night that took it, settled when it stopped waiting;
-- fact is his own forum fact. At most one waiting reply per post or letter.
CREATE TABLE board_reply (
  id INTEGER PRIMARY KEY CHECK(id >= 1),
  ts REAL NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('post','letter')),
  parent INTEGER NOT NULL CHECK(typeof(parent) = 'integer' AND parent >= 1),
  to_guid INTEGER NOT NULL CHECK(typeof(to_guid) = 'integer' AND to_guid BETWEEN 1 AND 4294967295),
  text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 400 AND (kind = 'letter' OR length(text) <= 200)),
  state TEXT NOT NULL CHECK(state IN ('waiting','answered','posted','refused','withdrawn')),
  reason TEXT CHECK(reason IS NULL OR (length(reason) BETWEEN 1 AND 64 AND reason NOT GLOB '*[^a-z0-9_]*')),
  night TEXT, settled REAL,
  fact INTEGER, answer_post INTEGER, answer_mail INTEGER,
  CHECK((state = 'waiting') = (settled IS NULL)),
  CHECK(state != 'refused' OR reason IS NOT NULL),
  CHECK(kind = 'post' OR (fact IS NULL AND answer_post IS NULL)),
  CHECK(kind = 'letter' OR answer_mail IS NULL)
);
CREATE UNIQUE INDEX board_reply_one_waiting ON board_reply(kind, parent) WHERE state = 'waiting';
