"""Vetted scene vocabulary. The model chooses keys; only this code writes scene text.

Pairings describe coherent activities and props, not a substitute for the scene screens.
Lust outfits/colours/expressions still come from kind.py and meet screen_lust unchanged.
"""
from .kind import LUST_SETTINGS

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
    "raiding": ("playing a raid game", ("desk",)),
    "guild_planning": ("planning a game session", ("desk",)),
    "spreadsheet": ("working on a spreadsheet", ("desk",)),
    "writing": ("writing in a notebook", ("desk", "cafe", "library", "porch")),
    "reading": ("reading a book", ("library", "living_room", "cafe", "porch", "balcony")),
    "cooking": ("stirring a pot of soup", ("kitchen",)),
    "baking": ("rolling bread dough", ("kitchen",)),
    "chopping": ("chopping vegetables", ("kitchen",)),
    "coffee": ("enjoying a cup of coffee", ("cafe", "kitchen", "porch", "balcony")),
    "tea": ("enjoying a cup of tea", ("cafe", "kitchen", "living_room", "garden")),
    "exercise": ("using an exercise bike", ("gym",)),
    "stretching": ("stretching both arms overhead", ("gym", "park")),
    "dog_walk": ("walking a dog on a leash", ("park", "trail", "harbour", "beach")),
    "walking": ("taking a quiet walk", ("park", "trail", "harbour", "beach", "garden")),
    "hiking": ("pausing on a hike", ("trail",)),
    "gardening": ("planting herbs in a pot", ("garden", "balcony", "porch")),
    "watering": ("watering a potted plant", ("garden", "balcony", "porch")),
    "sketching": ("sketching a landscape on paper", ("studio", "desk", "garden", "park")),
    "painting": ("painting a still life", ("studio", "workbench")),
    "knitting": ("knitting a scarf", ("living_room", "porch", "cafe")),
    "puzzle": ("assembling a landscape jigsaw", ("dining_room", "desk")),
    "guitar": ("playing an acoustic guitar", ("living_room", "studio", "porch")),
    "repairing": ("repairing a small wooden box", ("workbench",)),
    "organizing": ("sorting craft supplies", ("workbench", "studio", "desk")),
    "birdwatching": ("watching birds in the distance", ("park", "garden", "trail", "dock")),
    "picnic": ("enjoying a packed lunch", ("picnic", "park", "garden")),
    "breakfast": ("eating toast", ("kitchen", "dining_room", "cafe")),
    "relaxing": ("taking a quiet break", tuple(PLACES)),
    "sunset": ("watching the horizon", ("beach", "harbour", "dock", "boat", "balcony")),
    "celebrating": ("raising a mug in celebration", ("desk", "kitchen", "dining_room", "porch")),
}
EXPRESSIONS = {"smile": "a relaxed smile", "grin": "a cheerful grin",
               "laugh": "laughing", "thoughtful": "a thoughtful expression",
               "focused": "a focused expression", "calm": "a calm expression"}
OUTFITS = {"casual": "a cotton shirt and trousers",
           "warm": "a jumper and trousers", "outdoors": "a jacket and trousers",
           "exercise": "a loose tee and full-length leggings"}
PROPS = {"none": "", "mug": "holding a plain mug", "book": "holding a book",
         "notebook": "holding a notebook", "leash": "holding a dog leash",
         "plant": "beside a potted plant"}
PROP_ACTIVITIES = {
    "mug": ("coffee", "tea", "celebrating", "relaxing"),
    "book": ("reading", "relaxing"), "notebook": ("writing", "guild_planning"),
    "leash": ("dog_walk",), "plant": ("gardening", "watering", "relaxing"),
}
# Same vetted settings as the profile photo, except crowd/bathing/mirror contexts.
LUST_PLACES = {
    "a rooftop bar at night": "balcony", "a city street at night": "harbour",
    "a cafe terrace": "cafe", "a park in summer": "park",
    "a hotel balcony at dusk": "balcony", "a nightclub": "nightclub",
    "her living room couch": "living_room", "the front seat of her car": "car",
    "a wine bar": "cafe", "a dance floor": "dance_floor", "a garden party": "garden",
    "a ski lodge terrace": "porch", "the beach at sunset": "beach",
    "a hotel pool deck": "pool_deck", "a lakeside dock": "dock",
    "the deck of a sailing boat": "boat", "a beach bar": "beach", "the gym": "gym",
}
SCENE_LUST_SETTINGS = tuple(s for s in LUST_SETTINGS if s in LUST_PLACES)


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
        out.update(setting=PLACES[data["setting"]], outfit=OUTFITS[data["outfit"]],
                   expression=EXPRESSIONS[data["expression"]])
    out.update(activity=activity, shows=f"{activity}, {out['setting']}.", recipe=RECIPE)
    return out
