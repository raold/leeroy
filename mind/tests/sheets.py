"""Invented voice sheets for the tests (Persona.speech): nobody's real sheet, written here."""
from dataclasses import replace

from conftest import TEST_CAST, make_persona
from leeroy_mind.personas.io import save_persona
from leeroy_mind.personas.schema import speech_from_dict
from leeroy_mind.personas.voices import stamped
from leeroy_mind.wow import CLASSES, RACES

GRUFF = {
    "register": "plain, clipped, a bit gruff",
    "shape": "short flat statements, three to seven words",
    "says": ["noted", "sound as", "on my tally", "well so"],
    "never": ["awesome", "lol"],
    "brings_up": ["the allotment", "night shifts at the depot"],
    "with_him": "ribs him over his sums",
    "stance": "solo and content with it",
    "samples": {"answer": "depends. which boss was it",
                "tease": "bold claim from the man who pulled early",
                "tired": "double shift. brain is soup",
                "warm": "glad you logged on. well so",
                "annoyed": "noted. not doing that twice"},
}
BRIGHT = {
    "register": "bubbly, scattered, quick to laugh",
    "shape": "runs two thoughts together, then stops short",
    "says": ["ooh", "wait wait", "bless", "so anyhow", "i tell you"],
    "never": ["whatever", "k"],
    "brings_up": ["her sister's bakery", "the dog that eats socks", "the night bus"],
    "with_him": "flirts by teasing, then changes the subject",
    "stance": "seeing nobody, and cheerful about it",
    "samples": {"answer": "ooh wait wait, depends who is asking",
                "tease": "you meant that, i tell you, i saw",
                "tired": "awake since dawn, bless, eyes closing",
                "flirt": "repeat that please, i liked the sound",
                "annoyed": "no. so anyhow, never doing that twice"},
}


def sheet(data=GRUFF, **over):
    return speech_from_dict({**data, **over})


def give(store, guid: int, data=GRUFF, **persona):
    """Store the test-cast member `guid` with a voice sheet (and persona fields changed, as
    engine_rig.save_member takes them)."""
    name, race, cls = TEST_CAST[guid]
    p = make_persona(guid, name, **persona)
    p = replace(p, speech=stamped(sheet(data), p))  # as an import stamps it: her stance shows
    save_persona(store, p, source="seed", ts=1000.0, race=RACES[race], class_name=CLASSES[cls])
    return p
