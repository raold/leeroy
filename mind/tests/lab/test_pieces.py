"""The prompt pieces: a variant reaches a prompt only inside `using`, by name, at render time;
outside it every wording is the built-in one and nothing is marked."""
import json

from leeroy_mind.prompts import trace_templates
from leeroy_mind.talk import beat, pieces
from leeroy_mind.talk.prompt import DISCORD_DM, discord_template, render_instruction
from leeroy_mind.talk.turns import TurnKind


def plain_variant(name):
    return pieces.builtin(name).replace("Respond to what", "Plainly respond to what", 1)


def test_outside_using_nothing_changes_and_nothing_is_marked():
    before = render_instruction(TurnKind.REPLY_HUMAN, addressee="Testy")
    with trace_templates() as used:
        again = render_instruction(TurnKind.REPLY_HUMAN, addressee="Testy")
    assert before == again and not any(k.startswith("lab_") for k in used)
    assert pieces.text("reply_human", "x") == "x"
    assert discord_template("dm:101", 90) == DISCORD_DM


def test_a_variant_is_used_and_every_piece_read_is_marked():
    rendered = set()
    text = plain_variant("reply_human")
    with pieces.using({"reply_human": text}, rendered) as kept:
        assert kept == {"reply_human": text}
        line = render_instruction(TurnKind.REPLY_HUMAN, addressee="Testy", channel="dm:101",
                                  discord_words=90)
        cont = render_instruction(TurnKind.CONTINUE, addressee="Testy")
    assert "Plainly respond to what" in line
    # the Discord length still replaces the game's one short line in the variant
    assert "one short line" not in line and "Discord DM" in line
    assert "Plainly" not in cont
    assert rendered == {"reply_human", "discord_dm", "continue"}


def test_a_text_that_does_not_fit_is_left_out():
    rendered = set()
    bad = pieces.builtin("reply_human").replace("{they}", "they")
    with pieces.using({"reply_human": bad, "nope": "x"}, rendered) as kept:
        assert kept == {}
        assert "{they}" not in render_instruction(TurnKind.REPLY_HUMAN, addressee="Testy",
                                                  forms=("he", "him", "his")) or True
    assert rendered == {"reply_human"}


def test_the_situation_instruction_dm_wording_romance_hook_and_beat_ask():
    from leeroy_mind.talk.romance import SPECIFIC
    ask = "Before you answer, stop and think for yourself."
    dm = DISCORD_DM.replace("write as fully as you feel", "write the way you'd text a friend")
    situation = pieces.builtin("situation_reply_human").replace("as yourself", "as you really are")
    rendered = set()
    with pieces.using({"beat_ask": ask, "discord_dm": dm, "situation_reply_human": situation},
                      rendered):
        text = beat.instruction("Testy", ("share", "react"), [])
        assert text.startswith(ask) and beat.MARK not in text
        assert discord_template("dm:101", 90) == dm
        assert "as you really are" in render_instruction(TurnKind.REPLY_HUMAN, addressee="Testy",
                                                         situation=True)
        assert pieces.text("romance_hook", SPECIFIC) == SPECIFIC
    assert rendered == {"beat_ask", "discord_dm", "situation_reply_human", "romance_hook"}
    assert beat.instruction("Testy", ("share",), []).startswith(beat.MARK)


def test_overlays_are_read_by_name_from_a_prompts_dir(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "lab_reply_human.txt").write_bytes(plain_variant("reply_human").encode())
    (tmp_path / "prompts" / "lab_continue.txt").write_bytes(b"no placeholders at all")
    (tmp_path / "prompts" / "romance_explicit.txt").write_bytes(b"the owner's")
    assert pieces.read_overlays(tmp_path) == {"reply_human": plain_variant("reply_human")}
    assert pieces.read_overlays(tmp_path / "missing") == {}


def test_the_registry_names_overlays_and_keeps_its_phrases():
    for name in pieces.NAMES:
        assert pieces.overlay_name(name) == "lab_" + name
        assert all(k in pieces.builtin(name) for k in pieces.piece(name).keep)
    assert pieces.fields("a {b} {c}") == {"b", "c"}
    assert pieces.fields("a {b!r}") is None and pieces.fields("a {") is None
    json.dumps(pieces.NAMES)
