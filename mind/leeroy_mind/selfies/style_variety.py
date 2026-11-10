"""Deterministic photo choices balanced against the whole cast, with hard new-use caps."""
from collections import Counter
from dataclasses import asdict, dataclass
import math
import random
import re

from leeroy_mind.personas.schema import ProfilePhoto
from leeroy_mind.personas.screens import has_word, names_real_name
from . import kind
from .photo_extra import choose_light
from .scene_choices import EXPRESSIONS, OUTFITS, PLAIN_PLACES
from .style import LUST, style_of

# Explicit date-night subset of the already screened outfits; no swim/gym/workwear.
DATE_OUTFITS = ("fitted little dress", "low-cut cocktail dress", "bodycon mini dress",
    "halter top and a miniskirt", "satin midi dress", "plunging wrap dress",
    "fitted blazer dress", "one-shoulder top and leather trousers", "sequin party dress",
    "square-neck top and a pencil skirt", "silk blouse and a leather miniskirt")
DATE_SETTINGS = tuple(s for s in kind.LUST_SETTINGS
    if s not in kind.WATER_SETTINGS + kind.GYM_SETTINGS and s != "a park in summer")
KINDS = ("snapshot", "mirror_selfie", "selfie")
FRAMINGS = ("three_quarter", "upper_body", "head_and_shoulders", "face")
PAIR_CAP = 2
MAX_CAST = 1024


def key(text):
    return re.sub(r"^(?:a|an|the) ",""," ".join(text.casefold().split()))


def outfit_parts(text):
    text = key(text)
    for colour in sorted(kind.LUST_COLOURS,key=len,reverse=True):
        pattern = r"\b" + re.escape(colour) + r"\b"
        if re.search(pattern,text):
            return re.sub(pattern,"{colour}",text,count=1),colour
    return text,""


@dataclass(frozen=True)
class Assignment:
    photo: ProfilePhoto
    light: int | None
    codes: dict


class Counts:
    def __init__(self, personas):
        self.pairs,self.settings,self.kinds,self.framings,self.expressions = (Counter() for _ in range(5))
        self.outfits,self.colours = Counter(),Counter()
        for p in personas:
            self.add(p.photo,1)

    def add(self, photo, delta):
        if photo is not None:
            for counter,value in ((self.pairs,key(photo.outfit)),(self.settings,key(photo.setting)),
                (self.kinds,photo.kind),(self.framings,photo.framing),(self.expressions,key(photo.expression))):
                counter[value] += delta
            outfit,colour = outfit_parts(photo.outfit)
            self.outfits[outfit] += delta
            self.colours[colour] += delta


def _safe(p, photo, data, ident, names):
    if p.identity.age < 25:
        return False
    if data is not None:
        if kind.screen_lust(p,data):
            return False
    elif kind.screen_kind(p,asdict(photo),ident=ident,member_names=names):
        return False
    text = " ".join(asdict(photo).values())
    return not names_real_name(text,ident) and not any(has_word(text,n) for n in names if n)


def _choose(p, counts, cap, rng, *, seed, ident, names):
    lust = style_of(p) == LUST
    settings = DATE_SETTINGS if lust else tuple(PLAIN_PLACES.values())
    outfits = DATE_OUTFITS if lust else tuple(OUTFITS.values())
    expressions = kind.LUST_EXPRESSIONS if lust else tuple(EXPRESSIONS.values())
    frames = FRAMINGS[:2] if lust else FRAMINGS
    options = []
    for oi,outfit in enumerate(outfits):
        for ci,colour in enumerate(kind.LUST_COLOURS):
            if lust:
                phrase = kind.LUST_OUTFITS[outfit].format(colour=colour)
                phrase = ("an " if phrase[0].lower() in "aeiou" else "a ") + phrase
            else:
                phrase = ("an " if colour[0] in "aeiou" else "a ") + colour + " " + outfit.removeprefix("a ")
            if counts.pairs[key(phrase)] < PAIR_CAP:
                options.append((oi,ci,outfit,colour,phrase))
    options.sort(key=lambda x:(counts.pairs[key(x[4])],counts.outfits[outfit_parts(x[4])[0]],
                               counts.colours[x[3]],rng.random()))
    settings = sorted(enumerate(settings),key=lambda x:(counts.settings[key(x[1])],rng.random()))
    settings = [(i,s) for i,s in settings if counts.settings[key(s)] < cap]
    expressions = sorted(enumerate(expressions),key=lambda x:(counts.expressions[key(x[1])],rng.random()))
    kinds = sorted(KINDS,key=lambda x:(counts.kinds[x],rng.random()))
    frames = sorted(frames,key=lambda x:(counts.framings[x],rng.random()))
    # Name collisions are removed before products, bounding work even for hostile names.
    forbidden = tuple(names) + (p.character_name,p.identity.real_name)
    def clear(s):
        return not names_real_name(s,ident) and not any(has_word(s,n) for n in forbidden if n)
    options = [o for o in options if clear(o[4])]
    settings = [(i,s) for i,s in settings if clear(s)]
    expressions = [(i,s) for i,s in expressions if clear(s)]
    kinds = [s for s in kinds if clear(s)]
    frames = [s for s in frames if clear(s)]
    if p.identity.age < 25 or not options or not settings or not expressions or not kinds or not frames:
        return None
    for oi,ci,outfit,colour,phrase in options:
        for si,setting in settings:
            ei,expression = expressions[0]
            photo = ProfilePhoto(kinds[0],frames[0],setting,phrase,expression)
            data = dict(kind=photo.kind,framing=photo.framing,setting=setting,outfit=outfit,
                        colour=colour,expression=expression) if lust else None
            if _safe(p,photo,data,ident,forbidden):
                return Assignment(photo,choose_light(p,seed,ident=ident,names=names),
                    dict(outfit=oi,colour=ci,setting=si,expression=ei,kind=photo.kind,framing=photo.framing))
    return None


def assign(personas, selected, *, seed="v1", ident, names=()):
    if len(personas) > MAX_CAST:
        raise ValueError("too_many_members")
    counts = Counts(personas.values())
    cap = max(1,math.ceil(len(personas)/8))
    picked,rejected = {},[]
    order = sorted(set(selected) & personas.keys())
    random.Random(f"photo-order:{seed}").shuffle(order)
    for guid in order:
        p = personas[guid]
        counts.add(p.photo,-1)
        choice = _choose(p,counts,cap,random.Random(f"photo-variety:{seed}:{guid}"),
                         seed=seed,ident=ident,names=names)
        if choice is None:
            rejected.append(guid)
            counts.add(p.photo,1)
        else:
            picked[guid] = choice
            counts.add(choice.photo,1)
    # Existing fixed choices may already exceed a cap. Never silently edit them.
    violations = {"outfit_pairs":sum(n>PAIR_CAP for n in counts.pairs.values()),
                  "settings":sum(n>cap for n in counts.settings.values())}
    return picked,rejected,cap,violations
