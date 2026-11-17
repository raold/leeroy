"""Invented voice sheets for the tests (Persona.speech): nobody's real sheet, written here."""
from dataclasses import replace

from conftest import TEST_CAST, make_persona
from leeroy_mind.personas.io import save_persona
from leeroy_mind.personas.schema import speech_from_dict
from leeroy_mind.wow import CLASSES, RACES

LEDGER = {
    "register": "dry, exact, ledger-minded",
    "shape": "short flat statements, two to six words",
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
STAGE = {
    "register": "theatrical, arch, a little grand",
    "shape": "one long swooping sentence with an aside in it",
    "says": ["darling", "in the wings", "curtain up", "honestly now", "exit stage left"],
    "never": ["whatever", "k"],
    "brings_up": ["the amateur panto", "the cat who hates the hoover", "the bus home"],
    "with_him": "flirts as if it were a dare and waits to see",
    "stance": "married, and makes a bit of it",
    "samples": {"answer": "well now, that depends who is asking and how nicely",
                "tease": "oh you pulled that on purpose, admit it to the whole room",
                "tired": "i have been on my feet since six and the kettle knows it",
                "flirt": "say that again slower, i want to enjoy it properly",
                "annoyed": "no. absolutely not. curtain down on that idea"},
}


def sheet(data=LEDGER, **over):
    return speech_from_dict({**data, **over})


def give(store, guid: int, data=LEDGER, **persona):
    """Store the test-cast member `guid` with a voice sheet (and persona fields changed, as
    engine_rig.save_member takes them)."""
    name, race, cls = TEST_CAST[guid]
    p = replace(make_persona(guid, name, **persona), speech=sheet(data))
    save_persona(store, p, source="seed", ts=1000.0, race=RACES[race], class_name=CLASSES[cls])
    return p
