"""The kinds of the human's text emotes the mind tells apart, by the names native sends
(modules/mod-leeroy/src/LeeroyEmotes.cpp). A leaf module: it imports nothing, so the night
(nightly/attraction.py) reads these without the talk engine. talk/emotes.py answers them."""

# Affectionate: a line answers them by whisper, and the night counts one at a woman with an
# attitude toward him as a flirt.
ROMANTIC_EMOTES = frozenset({
    "kiss", "love", "flirt", "cuddle", "hug", "wink", "sexy", "holdhand", "charm",
    "purr", "lick", "beckon", "shimmy", "pounce", "gaze", "bashful", "curtsey"})
# Kind ones: answered warmly, never romantically.
FRIENDLY_EMOTES = frozenset({
    "comfort", "soothe", "apologize", "encourage", "goodluck", "beg", "plead", "kneel", "flex"})
# Rude ones: answered by her temper, and counted as a hurt at night.
RUDE_EMOTES = frozenset({
    "rude", "slap", "spit", "insult", "mock", "smack", "punch", "threaten", "snub", "shoo",
    "taunt", "scold", "growl", "snarl", "hiss", "shakefist", "moon", "fart", "bite", "rolleyes",
    "scoff"})
# Utility and combat emotes: signals to the group, never answered.
UTILITY_EMOTES = frozenset({
    "healme", "oom", "ready", "attackmytarget", "openfire", "incoming", "charge", "flee",
    "helpme", "follow", "wait", "stopattack", "sit", "stand", "laydown", "sleep", "eat", "drink",
    "chug", "brb", "map", "backpack", "mount_special", "talk", "talkex", "talkq", "absent", "go",
    "going", "hurry", "signal", "point", "enemy", "lost", "message", "search", "work"})


def answered_key(event_id: int) -> str:
    """The meta key of the receipt the live side writes once a member's emote back at his emote
    (the game_event row `event_id`) went out; its value is her guid. The night counts only
    emotes with one."""
    return f"emote_answered:{event_id}"
