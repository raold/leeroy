"""Vetted scene vocabulary. The model chooses keys; only this code writes scene text.

Pairings describe coherent activities and props, not a substitute for the scene screens.
Lust outfits/colours/expressions still come from kind.py and meet screen_lust unchanged.

Since the photo audit (2026-11-17, safe.py) an activity is said as what is there, never as what
the hands do, and a prop rests beside the subject: a gripped mug, pen, glass, knife, needle or
handlebar was where the hands failed (a mug in hand 23% defective, stick-like things 56%, the
exercise bike always garbled). The keys are unchanged; LEGACY_ACTIVITIES and LEGACY_PROPS read
the rows composed before.
"""
from .kind import LUST_SETTINGS
from .wardrobe import SCENE_MORE
from collections.abc import Mapping

RECIPE = 1  # persisted with composed text; older pending prose is held, never grandfathered
GAME_MONITOR = "monitor displaying a landscape game map"
SHEET_MONITOR = "monitor displaying a spreadsheet"
TILE_FRAMING = "solo candid portrait, head and shoulders"
TILE_DESK = "at a desk wearing a headset"
ONE_FACE_NEGATIVE = "second face, duplicate person, face on screen, webcam, video call, portrait on wall"

PLACES = {
    "desk": "at a desk at home", "kitchen": "in a home kitchen",
    "gym": "in an empty gym", "park": "on a quiet park path",
    "cafe": "at a quiet cafe table", "garden": "in a small garden",
    "porch": "on a front porch", "living_room": "in a living room",
    "workbench": "at a hobby workbench", "library": "in a quiet library alcove",
    "balcony": "on a balcony", "trail": "on a woodland trail",
    "harbour": "on a harbour walkway", "beach": "on a quiet beach",
    "pool_deck": "on an empty pool deck", "dock": "on a lakeside dock",
    "boat": "on a moored boat deck", "picnic": "at a picnic table",
    "studio": "in a craft studio", "dining_room": "at a dining table",
}
# The plain scene's setting list leaves out the water places, which invite swimwear; the lust
# style reaches them only through LUST_PLACES.
PLAIN_PLACES = {k: v for k, v in PLACES.items() if k not in ("beach", "pool_deck")}
# phrase, allowed place keys. Every activity has at least one plain place.
ACTIVITIES = {
    "raiding": ("at the computer on a raid night", ("desk",)),
    "guild_planning": ("planning a game session", ("desk",)),
    "spreadsheet": ("at the computer with a spreadsheet open", ("desk",)),
    "writing": ("an open notebook on the table", ("desk", "cafe", "library", "porch")),
    "reading": ("a book open on the table", ("library", "living_room", "cafe", "porch",
                                              "balcony")),
    "cooking": ("a pot of soup simmering on the stove", ("kitchen",)),
    "baking": ("a loaf of fresh bread on the counter", ("kitchen",)),
    "chopping": ("a board of chopped vegetables on the counter", ("kitchen",)),
    "coffee": ("a cup of coffee on the table", ("cafe", "kitchen", "porch", "balcony")),
    "tea": ("a cup of tea on the table", ("cafe", "kitchen", "living_room", "garden")),
    "exercise": ("resting after a workout", ("gym",)),
    "stretching": ("warming up before a workout", ("gym", "park")),
    "dog_walk": ("out on a walk with a dog alongside", ("park", "trail", "harbour", "beach")),
    "walking": ("taking a quiet walk", ("park", "trail", "harbour", "beach", "garden")),
    "hiking": ("pausing on a hike", ("trail",)),
    "gardening": ("pots of fresh herbs on a bench nearby", ("garden", "balcony", "porch")),
    "watering": ("a freshly watered potted plant nearby", ("garden", "balcony", "porch")),
    "sketching": ("a sketchbook open on the table", ("studio", "desk", "garden", "park")),
    "painting": ("an easel with a still life nearby", ("studio", "workbench")),
    "knitting": ("a half-knitted scarf and yarn on the table", ("living_room", "porch", "cafe")),
    "puzzle": ("a landscape jigsaw laid out on the table", ("dining_room", "desk")),
    "guitar": ("an acoustic guitar on a stand nearby", ("living_room", "studio", "porch")),
    "repairing": ("a small wooden box on the workbench", ("workbench",)),
    "organizing": ("craft supplies sorted on the table", ("workbench", "studio", "desk")),
    "birdwatching": ("watching birds in the distance", ("park", "garden", "trail", "dock")),
    "picnic": ("a packed lunch laid out on the table", ("picnic", "park", "garden")),
    "breakfast": ("a plate of toast on the table", ("kitchen", "dining_room", "cafe")),
    "relaxing": ("taking a quiet break", tuple(PLACES)),
    "sunset": ("watching the horizon", ("beach", "harbour", "dock", "boat", "balcony")),
    "celebrating": ("celebrating, a mug on the table", ("desk", "kitchen", "dining_room",
                                                        "porch")),
}
# The phrases rows composed before the audit hold, by key (channel_for and the critic read them)
LEGACY_ACTIVITIES = {
    "raiding": "playing a raid game", "spreadsheet": "working on a spreadsheet",
    "writing": "writing in a notebook", "reading": "reading a book",
    "cooking": "stirring a pot of soup", "baking": "rolling bread dough",
    "chopping": "chopping vegetables", "coffee": "enjoying a cup of coffee",
    "tea": "enjoying a cup of tea", "exercise": "using an exercise bike",
    "stretching": "stretching both arms overhead", "dog_walk": "walking a dog on a leash",
    "gardening": "planting herbs in a pot", "watering": "watering a potted plant",
    "sketching": "sketching a landscape on paper", "painting": "painting a still life",
    "knitting": "knitting a scarf", "puzzle": "assembling a landscape jigsaw",
    "guitar": "playing an acoustic guitar", "repairing": "repairing a small wooden box",
    "organizing": "sorting craft supplies", "picnic": "enjoying a packed lunch",
    "breakfast": "eating toast", "celebrating": "raising a mug in celebration",
}
EXPRESSIONS = {"smile": "a relaxed smile", "grin": "a cheerful grin",
               "laugh": "laughing", "thoughtful": "a thoughtful expression",
               "focused": "a focused expression", "calm": "a calm expression"}
# Every outfit names a top and a bottom (safe.is_complete): a jacket alone is no top
OUTFITS = {"casual": "a cotton shirt and trousers",
           "warm": "a jumper and trousers", "outdoors": "a jacket over a t-shirt and trousers",
           "exercise": "a loose tee and full-length leggings"}
# with the outfits only a wardrobe offers (wardrobe.scene_outfits): never in the base schema
ALL_OUTFITS = {**OUTFITS, **SCENE_MORE}
# Nothing is held: a prop rests beside the subject
PROPS = {"none": "", "mug": "a plain mug on the table", "book": "a closed book on the table",
         "notebook": "a notebook on the table", "leash": "the dog looking up at the camera",
         "plant": "beside a potted plant"}
LEGACY_PROPS = {"mug": "holding a plain mug", "book": "holding a book",
                "notebook": "holding a notebook", "leash": "holding a dog leash"}
LEGACY_OUTFITS = {"outdoors": "a jacket and trousers"}
PROP_ACTIVITIES = {
    "mug": ("coffee", "tea", "celebrating", "relaxing"),
    "book": ("reading", "relaxing"), "notebook": ("writing", "guild_planning"),
    "leash": ("dog_walk",), "plant": ("gardening", "watering", "relaxing"),
}

# Channel classification for every composed activity, setting and prop. Content enums
# remain unchanged; food takes precedence, then pets, then real-life places/activities.
ACTIVITY_CHANNEL = {
    'raiding':'pics', 'guild_planning':'pics', 'spreadsheet':'irl', 'writing':'pics',
    'reading':'pics', 'cooking':'food', 'baking':'food', 'chopping':'food', 'coffee':'food',
    'tea':'food', 'exercise':'irl', 'stretching':'irl', 'dog_walk':'pets', 'walking':'irl',
    'hiking':'irl', 'gardening':'irl', 'watering':'irl', 'sketching':'pics', 'painting':'pics',
    'knitting':'pics', 'puzzle':'pics', 'guitar':'pics', 'repairing':'irl', 'organizing':'irl',
    'birdwatching':'irl', 'picnic':'food', 'breakfast':'food', 'relaxing':'pics', 'sunset':'irl',
    'celebrating':'pics',
}
PLACE_CHANNEL = {
    'desk':'pics', 'kitchen':'food', 'gym':'irl', 'park':'irl', 'cafe':'food', 'garden':'irl',
    'porch':'irl', 'living_room':'pics', 'workbench':'irl', 'library':'pics', 'balcony':'irl',
    'trail':'irl', 'harbour':'irl', 'beach':'irl', 'pool_deck':'irl', 'dock':'irl', 'boat':'irl',
    'picnic':'food', 'studio':'pics', 'dining_room':'food',
}
PROP_CHANNEL = {'none':'pics', 'mug':'pics', 'book':'pics', 'notebook':'pics',
                'leash':'pets', 'plant':'irl'}


def channel_for(scene: Mapping) -> str:
    """Read the composed enums stored with the image, never classify model prose."""
    activity, setting = scene.get('activity', ''), scene.get('setting', '')
    found = {ACTIVITY_CHANNEL[key] for key in activity_keys(activity)}
    found.update(PLACE_CHANNEL[key] for key, phrase in PLACES.items() if setting == phrase)
    found.update({PROP_CHANNEL[prop_key(activity)]} - {PROP_CHANNEL["none"]})
    return next((channel for channel in ('food', 'pets', 'irl') if channel in found), 'pics')
# Same vetted settings as the profile photo, except crowd/bathing/mirror contexts.
LUST_PLACES = {
    "a rooftop bar at night": "balcony", "a city street at night": "harbour",
    "a cafe terrace": "cafe", "a park in summer": "park",
    "a hotel balcony at dusk": "balcony", "a nightclub": "nightclub",
    "her living room couch": "living_room", "her hallway at home": "living_room",
    "a sunny window seat at home": "living_room", "a bookshop": "library",
    "a wine bar": "cafe", "a dance floor": "dance_floor", "a garden party": "garden",
    "a ski lodge terrace": "porch", "the beach at sunset": "beach",
    "a hotel pool deck": "pool_deck", "a lakeside dock": "dock",
    "the deck of a sailing boat": "boat", "a beach bar": "beach", "the gym": "gym",
}
SCENE_LUST_SETTINGS = tuple(s for s in LUST_SETTINGS if s in LUST_PLACES)


def activity_keys(activity: str) -> list[str]:
    """The ACTIVITIES keys a composed activity text stands for, as it is said now or as a row
    composed before the audit says it (LEGACY_ACTIVITIES)."""
    said = [(key, phrase) for key, (phrase, _) in ACTIVITIES.items()]
    said += list(LEGACY_ACTIVITIES.items())
    return list(dict.fromkeys(key for key, phrase in said
                              if activity == phrase or activity.startswith(phrase + ", ")))


def prop_key(activity: str) -> str:
    """The PROPS key a composed activity text ends with ("none" for none), now or before the
    audit (LEGACY_PROPS)."""
    said = [(k, v) for k, v in PROPS.items() if v] + list(LEGACY_PROPS.items())
    return next((k for k, v in said if activity.endswith(", " + v)), "none")


def pairing_codes(data: dict) -> list[str]:
    """Called after schema validation; codes never contain model text."""
    activity, place = data["activity"], data["setting"]
    place = LUST_PLACES[place] if "colour" in data else place
    codes = []
    if activity != "relaxing" and place not in ACTIVITIES[activity][1]:
        codes.append("scene.activity: place pairing")
    prop = data.get("prop", "none")
    if prop != "none" and activity not in PROP_ACTIVITIES[prop]:
        codes.append("scene.prop: activity pairing")
    if "colour" not in data and data["outfit"] == "exercise" and place != "gym":
        codes.append("scene.outfit: gym pairing")
    return codes


def compose_choices(data: dict) -> dict:
    """Compose only validated, paired picks into the existing stored scene shape."""
    activity = ACTIVITIES[data["activity"]][0]
    if data["activity"] in ("raiding", "guild_planning"):
        activity += ", " + GAME_MONITOR
    elif data["activity"] == "spreadsheet":
        activity += ", " + SHEET_MONITOR
    prop = PROPS[data.get("prop", "none")]
    if prop:
        activity += ", " + prop
    out = {k: v for k, v in data.items() if k != "prop"}
    if "colour" not in data:
        out.update(setting=PLACES[data["setting"]], outfit=ALL_OUTFITS[data["outfit"]],
                   expression=EXPRESSIONS[data["expression"]])
    out.update(activity=activity, shows=f"{activity}, {out['setting']}.", recipe=RECIPE)
    return out
