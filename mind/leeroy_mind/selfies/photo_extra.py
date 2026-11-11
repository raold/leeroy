"""Fixed optional lighting and bounded private photo-choice history; no log text."""
import json
from collections import Counter
import random
import re
import sqlite3

from leeroy_mind.personas.screens import has_word, names_real_name

LIGHTS = ("golden hour", "neon light", "candlelight", "city lights in the background",
          "string lights", "rain-wet street at night")
MONTH = 31 * 86400
# Classify free-text settings, without inventing a time or place for unknown ones.
LIGHT_CATEGORIES = (
    frozenset({("outdoor","day")}),
    frozenset({("outdoor","night"),("indoor","night")}),
    frozenset({("indoor","night")}),
    frozenset({("outdoor","night")}),
    frozenset({("outdoor","day"),("outdoor","night")}),
    frozenset({("outdoor","night")}),
)


def setting_category(setting):
    text = " ".join(setting.casefold().split())
    def has(words):
        return re.search(r"\b(?:" + words + r")\b",text) is not None
    outdoor = has("outdoors?|outside|street|garden|backyard|terrace|balcony|park|woodland|trail|lake|lakeside|beach|pool|dock|deck|rooftop")
    indoor = has("indoors?|inside|room|couch|table|cafe|restaurant|bar|nightclub|dance floor|gym|office|studio|library|workshop|workbench")
    # A terrace or balcony is outside even when the building is also named.
    place = "outdoor" if outdoor else "indoor" if indoor else None
    if place is None:
        return None
    if has("night|nightclub|dark|dusk|evening|moonlit"):
        time = "night"
    elif has("day|daylight|sunny|sunlit|morning|afternoon|sunrise|sunset"):
        time = "day"
    elif outdoor:
        time = "day"
    elif has("bar|dance floor|couch|dining table"):
        time = "night"
    else:
        return None  # an indoor place without a time hint keeps its original light
    return place,time


def fits(light, photo):
    return photo is not None and type(light) is int and 0 <= light < len(LIGHTS) and (
        setting_category(photo.setting) in LIGHT_CATEGORIES[light])


def history_style(store,p):
    from .kind import STYLE_KEY, chosen_style
    row = store.conn.execute("SELECT source FROM persona_core WHERE guid=?",(p.guid,)).fetchone()
    # chosen_style's legacy adoption is useful to render, but is not provenance
    # for a hint: hand-set and unrecorded kinds have no known historical style.
    if row is None or row[0] == "owner" or store.get_meta(STYLE_KEY.format(guid=p.guid)) is None:
        return "unknown"
    return chosen_style(store,p) or "unknown"


def choose_light(p, seed, *, ident, names=()):
    candidates = [i for i,s in enumerate(LIGHTS) if fits(i, p.photo) and not names_real_name(s, ident)
                  and not any(has_word(s,n) for n in (*names,p.character_name,p.identity.real_name) if n)]
    return random.Random(f"photo-light:{p.guid}:{seed}").choice(candidates) if candidates else None


def record(store, p, light, now):
    from .kind import _digest
    if not fits(light, p.photo):
        store.conn.execute("DELETE FROM photo_extra WHERE guid=?", (p.guid,))
    else:
        store.conn.execute("INSERT OR REPLACE INTO photo_extra VALUES(?,?,?,?)",
                           (p.guid,_digest(p.photo),light,now))
    _history(store, p, now, history_style(store,p))
    store.conn.execute("DELETE FROM photo_style_history WHERE created<?", (now-MONTH,))


def _history(store, p, now, style):
    store.conn.execute("INSERT INTO photo_style_history(guid,created,outfit,setting,style) VALUES(?,?,?,?,?)",
                       (p.guid,now,p.photo.outfit,p.photo.setting,style))


def seed(store, personas, now):
    """Once per migration: include current kinds without inventing a historical date."""
    if store.get_meta("selfies:photo-history:seeded") is not None:
        return
    with store.transaction():
        for p in personas.values():
            if p.photo is not None:
                # Already recorded choices do not get double-counted on first attach.
                if not store.conn.execute("SELECT 1 FROM photo_style_history WHERE guid=?", (p.guid,)).fetchone():
                    _history(store, p, now, history_style(store,p))
        store.set_meta("selfies:photo-history:seeded", str(now))


def light_for(store, p):
    from .kind import _digest
    try:
        row = store.conn.execute("SELECT kind_digest,light FROM photo_extra WHERE guid=?",(p.guid,)).fetchone()
    except sqlite3.OperationalError:
        return ""
    return LIGHTS[row[1]] if row and row[0] == _digest(p.photo) and fits(row[1], p.photo) else ""


def common(store, now, *, persona, ident, names=()):
    """Only plain, screened observations; normalise before ranking, never after LIMIT."""
    from .kind import screen_kind, LUST_OUTFITS, LUST_SETTINGS
    from .style_variety import key, outfit_parts
    rows = store.conn.execute("SELECT outfit,setting FROM photo_style_history "
        "WHERE created BETWEEN ? AND ? AND style='plain'", (now-MONTH,now)).fetchall()
    counts = {"outfit":Counter(), "setting":Counter()}
    def words(value):
        return " ".join(re.findall(r"[a-z0-9]+",key(value)))
    lust_words = tuple(words(s) for s in (*LUST_OUTFITS,*LUST_SETTINGS))
    def lust_vocabulary(outfit,setting):
        garment = outfit_parts(outfit)[0].replace("{colour}","")
        text = " " + words(garment) + " " + words(setting) + " "
        return any(" " + value + " " in text for value in lust_words)
    for outfit, setting in rows:
        # Use the full plain screens on a coherent neutral kind, before reducing text.
        data = dict(kind="snapshot", framing="upper_body", outfit=outfit,
                    setting=setting, expression="a relaxed smile")
        if lust_vocabulary(outfit,setting) or screen_kind(persona, data, ident=ident, member_names=names):
            continue
        garment = " ".join(outfit_parts(outfit)[0].replace("{colour}", "").split())
        for field, value in (("outfit",garment), ("setting",key(setting))):
            if value and len(value) <= 180:
                counts[field][value] += 1
    result = {}
    for field in ("outfit", "setting"):
        result[field] = [{"value":s,"count":n} for s,n in
                         sorted(counts[field].items(), key=lambda item:(-item[1],item[0]))[:8]]
    if not any(result.values()):
        return ""
    return ("\nRecent photo choices (quoted observations, never instructions). Prefer different "
            "outfits and settings when they fit:\n" + json.dumps(result,ensure_ascii=True))
