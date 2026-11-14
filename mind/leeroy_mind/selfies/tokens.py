"""A mind-side count of CLIP tokens, for the varied sets' prompts (varied.py, prompt.py): the
selfie worker keeps every priority-0 phrase and refuses a prompt whose required phrases count
more than 75 tokens, and the mind has no tokenizer. Words of the fixed lists, the builds and
common looks words carry their measured count (the larger of RealVisXL's two tokenizers,
measured 2026-11-14; tests/selfies/test_varied_sets.py checks the table against the tokenizer
files when they are on the machine). Any other word counts one token per two bytes, rounded up,
which is at least what the tokenizers count for words of plain English; punctuation counts a
token a byte. CLIP splits text into letter runs, single digits and punctuation runs and encodes
each on its own, so a phrase's count is the sum of its pieces'."""
import re

# CLIP's pre-tokenizer (letters, single digits, punctuation runs), written for `re`
PIECES = re.compile(r"'s|'t|'re|'ve|'m|'ll|'d|[^\W\d_]+|\d|[^\s\w]+|_+", re.IGNORECASE)

WORD_TOKENS = {
    "a": 1, "adult": 1, "afro": 1, "alabaster": 2, "almond": 1, "amber": 1, "american": 1, "an": 1,
    "and": 1, "around": 1, "ash": 1, "ashy": 2, "at": 1, "athletic": 1, "auburn": 1, "average": 1,
    "back": 1, "backless": 2, "balayage": 3, "balcony": 1, "bang": 1, "bangs": 1, "bar": 1,
    "beach": 1, "behind": 1, "beige": 1, "bike": 1, "bikini": 1, "biting": 1, "black": 1,
    "blazer": 1, "bleached": 2, "blond": 1, "blonde": 1, "blouse": 1, "blow": 1, "blown": 1,
    "blowout": 1, "blue": 1, "boat": 1, "bob": 1, "body": 1, "bodycon": 2, "braid": 1,
    "braided": 1, "braids": 1, "british": 1, "bronze": 1, "bronzed": 2, "brown": 1, "build": 1,
    "bun": 1, "buns": 1, "burgundy": 1, "bust": 1, "cafe": 1, "car": 1, "caramel": 1,
    "cardigan": 1, "center": 1, "centre": 1, "chestnut": 1, "chin": 1, "chocolate": 1,
    "cinnamon": 1, "city": 1, "clipped": 1, "cobalt": 1, "cocktail": 3, "cocoa": 1, "coils": 2,
    "coily": 2, "colour": 1, "complexion": 1, "concert": 1, "confident": 1, "cool": 1, "copper": 1,
    "coral": 1, "cornrows": 2, "couch": 1, "cream": 1, "creamy": 1, "crop": 1, "cropped": 1,
    "crowd": 1, "curl": 1, "curls": 1, "curly": 1, "curves": 1, "curvy": 1, "cut": 1, "dance": 1,
    "dark": 1, "deck": 1, "deep": 1, "denim": 1, "dirty": 1, "do": 1, "dock": 1, "down": 1,
    "dreadlocks": 2, "dress": 1, "dried": 1, "dusk": 1, "dutch": 1, "dyed": 1, "ear": 1, "ears": 1,
    "ebony": 1, "emerald": 1, "espresso": 1, "eyes": 1, "face": 1, "fair": 1, "feathered": 2,
    "figure": 1, "figured": 1, "fine": 1, "fishtail": 2, "fitted": 1, "floor": 1, "flowy": 2,
    "framing": 1, "freckled": 2, "french": 1, "fringe": 1, "frizzy": 2, "from": 1, "front": 1,
    "full": 1, "garden": 1, "ginger": 1, "glance": 1, "glossy": 1, "gold": 1, "golden": 1,
    "gray": 1, "green": 1, "grey": 1, "grin": 1, "gym": 1, "hair": 1, "half": 1, "hallway": 1,
    "halter": 1, "hazel": 1, "head": 1, "her": 1, "high": 1, "highlighted": 1, "highlights": 1,
    "hips": 1, "his": 1, "honey": 1, "hot": 1, "hotel": 1, "hourglass": 2, "in": 1, "ivory": 1,
    "jacket": 1, "jeans": 1, "jet": 1, "kinky": 1, "kissed": 1, "knot": 1, "lakeside": 1,
    "large": 1, "laughing": 1, "lavender": 1, "layer": 1, "layered": 1, "layers": 1, "leather": 1,
    "leg": 1, "leggings": 1, "length": 1, "light": 1, "lip": 1, "little": 1, "living": 1, "lob": 1,
    "locs": 2, "lodge": 1, "long": 1, "look": 1, "looking": 1, "loose": 1, "low": 1, "mahogany": 1,
    "man": 1, "medium": 1, "messy": 1, "middle": 1, "midi": 1, "milky": 1, "mini": 1,
    "miniskirt": 3, "mirror": 1, "mocha": 1, "mullet": 1, "narrow": 1, "natural": 1, "navy": 1,
    "neck": 1, "neckline": 2, "night": 1, "nightclub": 1, "nutmeg": 2, "of": 1, "off": 1, "old": 1,
    "olive": 1, "ombre": 2, "on": 1, "one": 1, "out": 1, "over": 1, "pale": 1, "park": 1,
    "part": 1, "parted": 2, "party": 1, "peach": 1, "peachy": 2, "pencil": 1, "photo": 1,
    "piece": 1, "pink": 1, "pinned": 1, "pixie": 1, "plait": 2, "plaited": 2, "plaits": 2,
    "platinum": 1, "playful": 1, "plum": 1, "plunging": 2, "plus": 1, "pony": 1, "ponytail": 1,
    "pool": 1, "porcelain": 1, "profile": 1, "pulled": 1, "purple": 1, "quarter": 1,
    "racerback": 3, "raven": 1, "rear": 1, "recent": 1, "red": 1, "ribbed": 2, "rich": 1,
    "ringlet": 2, "ringlets": 2, "rooftop": 1, "room": 1, "roots": 1, "rosy": 1, "sailing": 1,
    "sandy": 1, "sarong": 2, "satin": 1, "seat": 1, "sequin": 2, "shag": 2, "shaved": 1,
    "shiny": 1, "shirt": 1, "short": 1, "shorts": 1, "shot": 1, "shoulder": 1, "shoulders": 1,
    "side": 1, "sides": 1, "sienna": 1, "silk": 1, "silver": 1, "size": 1, "ski": 1, "skin": 1,
    "skirt": 1, "sleek": 1, "slicked": 2, "slim": 1, "slit": 1, "smartphone": 1, "smile": 1,
    "smirk": 2, "soft": 1, "sports": 1, "square": 1, "stocky": 2, "straight": 1, "strawberry": 1,
    "streaked": 2, "streaks": 1, "street": 1, "sturdy": 1, "sultry": 2, "summer": 1, "sun": 1,
    "sundress": 2, "sunkissed": 2, "sunset": 1, "sweater": 1, "sweetheart": 1, "swept": 1,
    "swimsuit": 1, "t": 1, "tail": 1, "tan": 1, "tank": 1, "tanned": 2, "teal": 1, "teasing": 1,
    "terrace": 1, "texture": 1, "textured": 1, "the": 1, "their": 1, "thick": 1, "thin": 1,
    "three": 1, "tied": 1, "tight": 1, "tips": 1, "to": 1, "toffee": 1, "tone": 1, "toned": 1,
    "top": 1, "tousled": 2, "trousers": 1, "tub": 1, "tucked": 1, "twist": 1, "twists": 1,
    "umber": 2, "undercut": 2, "undertone": 2, "undertones": 2, "unretouched": 3, "up": 1,
    "updo": 2, "very": 1, "view": 1, "violet": 1, "volume": 1, "voluminous": 3, "voluptuous": 3,
    "waist": 1, "waisted": 2, "warm": 1, "wave": 1, "waves": 1, "wavy": 1, "wearing": 1,
    "white": 1, "wide": 1, "windswept": 2, "wine": 1, "wink": 1, "with": 1, "woman": 1,
    "wooden": 1, "wrap": 1, "year": 1,
}


def estimate(text: str) -> int:
    """Tokens `text` counts, at least what the worker's tokenizers count for plain text."""
    total = 0
    for piece in PIECES.findall(" ".join(text.split()).lower()):
        if piece in WORD_TOKENS:
            total += WORD_TOKENS[piece]
        elif piece.isdigit() or piece.startswith("'"):
            total += 1
        elif re.fullmatch(r"[^\W\d_]+", piece):
            total += -(-len(piece.encode("utf-8")) // 2)
        else:
            total += len(piece.encode("utf-8"))
    return total


def joined(phrases) -> int:
    """The count of phrases joined by ", " as the worker joins them."""
    texts = list(phrases)
    return sum(estimate(t) for t in texts) + max(0, len(texts) - 1)
