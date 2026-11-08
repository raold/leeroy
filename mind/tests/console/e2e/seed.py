"""Invented data, using production writers where a public writer exists."""
import base64
import time
from dataclasses import replace

from conftest import HUMAN, TEST_CAST, make_persona
from console.rig import SYNTHETIC_JPEG_B64
from leeroy_mind.clock import SystemClock
from leeroy_mind.library.core import Library
from leeroy_mind.library.chunks import ChunkDraft, SourceResult
from leeroy_mind.guild import store as raids
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Chat, Member
from leeroy_mind.selfies import files
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.tuning.changes import Set, apply_sets, picture
from leeroy_mind.wow import CLASSES, RACES
from selfies.fakes import PHOTO, ready
from nightly.test_night_enrich import APPEARANCE

CAST = {**TEST_CAST, 106: ("Fennel", 2, 3), 107: ("Glimmer", 8, 7),
        108: ("Hazel", 10, 5), 109: ("Ilex", 2, 6), 110: ("Juniper", 6, 11)}


def members():
    return [Member(guid=g, name=n, race=r, class_=c, gender=0, level=80, rank=3,
                   online=g != 110, map=0, zone=1519, spec_tab=0)
            for g, (n, r, c) in CAST.items()]


def seed(data):
    now = time.time()
    store = Store(open_db(data / "mind.db"), clock=SystemClock())
    try:
        store.upsert_cast(members(), now)
        store.upsert_cast([Member(guid=HUMAN, name="Testmage", race=10, class_=8,
                                 gender=0, level=80, rank=0, online=True, map=0,
                                 zone=1519, spec_tab=0)], now)
        store.set_meta("human_guid", str(HUMAN))
        store.set_meta("guild_id", "21")
        personas = {}
        for guid, (name, race, cls) in CAST.items():
            persona = replace(make_persona(guid, name), appearance=APPEARANCE, photo=PHOTO)
            save_persona(store, persona, source="seed", ts=now, race=RACES[race],
                         class_name=CLASSES[cls])
            personas[guid] = persona
        raid = raids.new_raid(store.conn, started=now - 7200, raid_key="icc10", size=10,
                              heroic=False, offline=False, lead_guid=101,
                              tank_guid=101, loot_guid=102)
        raids.record_attendance(store.conn, raid, list(CAST), [])
        raids.end_raid(store.conn, raid, ended=now - 3600, kills=2, wipes=1, outcome="ended")
        apply_sets(store, [Set("member", "101", "talkative", 6)],
                   picture(store, personas), source="console", now=now)
        for guid in (101, 102, 103):
            ready(store, data, guid, 1, now=now)
            # The existing worker fixture's JPEG is deliberately minimal. Browser
            # fixtures need decodable pixels, not just the worker protocol marker.
            for index in range(4):
                for thumb in (False, True):
                    files.candidate(data, guid, 1, index, thumb=thumb).write_bytes(
                        base64.b64decode(SYNTHETIC_JPEG_B64))
        # These Realm records have no standalone public insertion API: their
        # production writers are transaction bodies inside the nightly controllers.
        with store.transaction():
            conn = store.conn
            conn.execute("INSERT INTO realm_post(ts,author,author_kind,thread,text) "
                         "VALUES(?,101,'member','rehearsal','Bring tea for the rehearsal.')", (now,))
            conn.execute("INSERT INTO realm_calendar(kind,creator,at,title,ref,updated,event_id,state) "
                         "VALUES('raid',101,?,'Rehearsal raid night','e2e:raid',?,501,'posted')",
                         (now + 86400, now))
            conn.execute("INSERT INTO realm_rsvp(event,guid,status,ts) VALUES(501,102,'signed_up',?)", (now,))
            conn.execute("INSERT INTO realm_guild(id,role,faction,name,added) "
                         "VALUES(31,'progression','horde','Lantern Company',?)", (now,))
            conn.execute("INSERT INTO realm_actor(guid,guild,name,role,added,place) "
                         "VALUES(901,31,'Kestrel','officer',?,'Dalaran')", (now,))
            conn.execute("INSERT INTO realm_bank(ts,dir,reason,guid,item_id,count,money) "
                         "VALUES(?,'in','Rehearsal supplies',101,2589,20,0)", (now,))
    finally:
        store.close()
    class RehearsalSource:
        name = "curated"

        def load(self, library):
            return SourceResult(self.name, chunks=[ChunkDraft(
                key="rehearsal", kind="strategy", title="Rehearsal tactics",
                text="Bring tea, wait for the tank, and help your guildmates.")])

    library = Library(data / "library.db", clock=SystemClock())
    try:
        library.build([RehearsalSource()])
    finally:
        library.close()
