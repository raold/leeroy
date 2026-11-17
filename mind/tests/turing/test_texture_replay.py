"""`turing texture`: stored lines typed again with the texture, read only, on invented data."""
import json
import random
import time

from conftest import seed_test_cast
from leeroy_mind.__main__ import main
from leeroy_mind.protocol import Chat
from leeroy_mind.talk import style
from leeroy_mind.turing import texture
from leeroy_mind.turing.corpus import Line

from .test_cli import data_dir

HIS = ["im not sure thats right", "dont pull yet, wait for me", "ok then", "where are you now",
       "cant tonight, work is a mess", "its fine, ill be there"]
THEIRS = ["i'm not sure that's the plan", "don't worry, it's fine", "ok sure", "where is everyone",
          "can't stay long, i've got work", "that's what i said"]


def fill(store, now):
    store.set_meta("human_guid", "1")
    seed_test_cast(store)
    rng = random.Random(7)
    t = now - 6 * 86400
    for i in range(120):
        m = 101 + i % 5
        t += 4000
        store.insert_chat_line(Chat(0, "whisper", 1, "Testmage", m, rng.choice(HIS), 0, 0),
                               session_id=None, origin="human", ts=t)
        store.insert_chat_line(Chat(0, "whisper", m, f"M{m}", 1, rng.choice(THEIRS), 0, 0),
                               session_id=None, origin="mind", ts=t + 15)


def test_replay_types_the_members_lines_again_and_leaves_his(store):
    now = time.time()
    fill(store, now)
    before = store.conn.total_changes
    cal = style.Calibration(careful_apostrophes=0.0, apos_base=0.9, apos_span=0.0)
    data = texture.replay(store.conn, human=1, start=now - 30 * 86400, until=now, cal=cal)
    assert store.conn.total_changes == before
    for part in ("all", "fit", "confirm"):
        r = data["parts"][part]["private"]
        assert r["him"]["lines"] and r["stored"]["lines"] == r["typed"]["lines"]
        assert r["stored"]["dropped"] == 0 and r["typed"]["dropped"] > 0.6
        assert r["him"]["dropped"] == 1
        assert data["parts"][part]["game"]["him"]["lines"] == 0
    whole = data["parts"]["all"]["private"]
    assert whole["him"]["lines"] == 120 and whole["auc_typed"] < whole["auc_stored"] - 0.1
    fit, confirm = (data["parts"][k]["private"]["him"]["lines"] for k in ("fit", "confirm"))
    assert fit + confirm == 120 and min(fit, confirm) >= 30  # by day, about even
    assert data["spread"]["members"] == 5 and data["spread"]["apostrophes"]["drop"] == 5


def test_retyping_puts_her_abbreviations_back_first_and_is_seeded():
    from conftest import make_persona
    t = make_persona(101, "Arkon").typing
    t = type(t)(**{**{f: getattr(t, f) for f in t.__dataclass_fields__},
                   "abbreviations": {"you": "u"}, "case": "lower", "end_punct": "none"})
    line = Line("c:1", 0.0, "private", "w:101", "whisper", 101, False, "u said u would, see u")
    never = style.Calibration(texters=0.0, light_shorthand=0.0, light_extra=0.0)
    out = texture.retype(line, "Arkon", t, never, frozenset(), "a")
    assert " u " not in f" {out} " and out.count("you") == 3
    assert out == texture.retype(line, "Arkon", t, never, frozenset(), "a")


def test_cli_prints_numbers_and_labels_only(store, capsys):
    now = time.time()
    fill(store, now)
    root = data_dir(store)
    assert main(["turing", "texture", "--data-dir", str(root)]) == 0
    out = capsys.readouterr().out
    assert "Whispers and DMs: 120 of your lines, 120 of theirs" in out
    assert "contractions typed without their apostrophe: you 100%, stored 0%" in out
    assert not any(text in out for text in HIS + THEIRS)  # never a line's words
    until = time.strftime("%Y-%m-%d", time.localtime(now - 3 * 86400))
    assert main(["turing", "texture", "--json", "--until", until, "--data-dir", str(root)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert 0 < data["parts"]["all"]["private"]["him"]["lines"] < 120
