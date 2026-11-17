"""Invented voice sheets for the tests (Persona.speech): nobody's real sheet, written here."""
from dataclasses import replace

from conftest import TEST_CAST, make_persona
from leeroy_mind.personas.io import save_persona
from leeroy_mind.personas.schema import speech_from_dict
from leeroy_mind.wow import CLASSES, RACES

GRUFF = {
    "register": "plain, clipped, a bit gruff",
    "shape": "short flat statements, three to seven words",
    "says": ["noted", "fair enough", "on paper", "right then"],
    "never": ["awesome", "lol"],
    "brings_up": ["the allotment", "night shifts at the depot"],
    "with_him": "teases him about his sums",
    "stance": "single and in no hurry about it",
    "samples": {"answer": "depends. what did the log say",
                "tease": "bold claim from the man who pulled early",
                "tired": "long shift. brain is soup",
                "warm": "glad you logged on. right then",
                "annoyed": "noted. not doing that twice"},
}
BRIGHT = {
    "register": "bubbly, scattered, quick to laugh",
    "shape": "runs two thoughts together and trails off",
    "says": ["ooh", "hang on", "bless", "right so", "tell you what"],
    "never": ["whatever", "k"],
    "brings_up": ["her sister's bakery", "the dog that eats socks", "the night bus"],
    "with_him": "flirts by teasing, then changes the subject",
    "stance": "seeing nobody, and cheerful about it",
    "samples": {"answer": "ooh hang on, that depends who wants to know",
                "tease": "you did that on purpose, tell you what, i saw it",
                "tired": "been up since six, bless, my eyes are closing",
                "flirt": "say that again, i liked how it sounded",
                "annoyed": "no. right so, we are not doing that again"},
}


def sheet(data=GRUFF, **over):
    return speech_from_dict({**data, **over})


def give(store, guid: int, data=GRUFF, **persona):
    """Store the test-cast member `guid` with a voice sheet (and persona fields changed, as
    engine_rig.save_member takes them)."""
    name, race, cls = TEST_CAST[guid]
    p = replace(make_persona(guid, name, **persona), speech=sheet(data))
    save_persona(store, p, source="seed", ts=1000.0, race=RACES[race], class_name=CLASSES[cls])
    return p
