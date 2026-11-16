"""One grounded, fictional offscreen life beat per member and night (P1c Task 13).

He lives far away (2026-11-15: beats invented his texts, notes in her flat and visits, and those
memories surfaced in chat as real): every life prompt ends on FAR_AWAY, a beat or title that
pairs one of his names with contact off the game (meets_him) is refused before grounding and
never shown as a last beat again, and a beat that names him or touches her secret is stored
private to her and him (private_beat), not open to every turn."""
import json
import re

from leeroy_mind.nightly import wowlife
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.evidence import Evidence, Item, load_facts
from leeroy_mind.nightly.ground import Claim, Verdict, ground, log_verdicts
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.personas.io import load_persona
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.talk.words import COMMON_WORDS
from leeroy_mind.text import strip_wow_markup

STAGE = "life"
DAY = 86400.0
BEAT_SCHEMA = schema("life_beat", {
    "beat": {"type": "string", "maxLength": 200},
    "mood": {"type": "number", "minimum": -1, "maximum": 1},
    "resolved": {"type": "boolean"},
    "next_title": {"type": ["string", "null"], "maxLength": 80},
})
PROMPT = "Write the next small event in {name}'s life away from the game.\n{card}"
FAR_AWAY = ("{human} lives far away: {name} and {human} have never met in person and only talk "
            "in the game and on the guild's Discord. Never put {human} into {name}'s life away "
            "from the game: no texts, calls, notes, gifts or visits from {them}, and {they} "
            "never turns up there.")
# Contact off the game, said with one of his names (meets_him): a phone, a note or gift, a visit
_THINGS = r"(?:note|card|letter|gift|present|flowers|package|parcel|text|message)"
_CONTACT = re.compile(
    r"\b(?:text(?:s|ed|ing)?|call(?:s|ed|ing)?|phon(?:es|ed|ing)|on the phone|rang|ringing|"
    r"voicemail|facetime\w*|speakerphone|inbox|e-?mail\w*|"
    rf"{_THINGS}s? from|(?:sticky|handwritten|little|love) notes?|"
    rf"(?:left|sent|mailed|brought|handed) (?:me |her |him )?(?:a |an |his |her |some )?{_THINGS}s?|"
    r"gifts?|flowers|deliver\w*|visit\w*|(?:came|come|coming) over|(?:stopped|dropped) by|"
    r"showed up|turned up|knock\w*|drove|driv(?:e|es|ing|en)|picked (?:me|her|him) up|"
    r"met (?:up|him|her)|meet(?:ing)? (?:up|him|her)|in person|hug\w*|kiss\w*|shadow|"
    r"used to sit|beside (?:me|her)|next to (?:me|her)|walked (?:in|over))\b", re.I)
_WORD = re.compile(r"[a-z']+")
SECRET_WORDS = 2  # distinct content words a beat shares with her secret to be hers alone
# words too plain or too much the game's to tie a beat to her secret (beside talk.words')
_PLAIN = frozenset("""through finally because doesn't hasn't didn't isn't wasn't won't can't
couldn't wouldn't shouldn't really always never every something nothing anything someone
everyone feel feels felt feeling line empty character guild guilds raid raids raiding game
games gaming playing player players play online quiet quietly little still""".split())


def his_names(ident) -> tuple[str, ...]:
    return tuple(n for n in (ident.nickname, ident.character_name, ident.real_name) if n)


def _names_re(names) -> re.Pattern | None:
    """His names as words; a name of 5 letters or more also with up to two more letters, as the
    model mangles it (a name with a letter added)."""
    alts = [re.escape(n) + (r"\w{0,2}" if len(n) >= 5 else "")
            for n in sorted(set(names), key=len, reverse=True) if n]
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.I) if alts else None


def names_him(text: str, names) -> bool:
    pattern = _names_re(names)
    return pattern is not None and pattern.search(text or "") is not None


def meets_him(text: str, names) -> bool:
    """The text names him and contact off the game (_CONTACT): his text, call, note, gift or
    visit. He lives far away; they only meet online."""
    return names_him(text, names) and _CONTACT.search(text or "") is not None


def _content(text: str) -> set[str]:
    return {w.rstrip("s") for w in _WORD.findall((text or "").casefold().replace("’", "'"))
            if len(w) >= 4 and w not in COMMON_WORDS and w not in _PLAIN}


def touches_secret(text: str, persona) -> bool:
    """The text shares SECRET_WORDS content words with her secret or her deep disclosure."""
    if persona is None:
        return False
    secret = " ".join([persona.secret, *(d.text for d in persona.disclosures
                                         if d.depth == "deep")])
    return len(_content(text) & _content(secret)) >= SECRET_WORDS


def private_beat(text: str, title: str, persona, names) -> bool:
    """A beat stored private to her and him: it names him, or it or its thread touches her
    secret."""
    return names_him(text, names) or touches_secret(f"{text} {title}", persona)


def ensure_thread(store, guid, title, now, rng, *, dial=5) -> int:
    """Keep an active thread, or start one and compose its title onto today's mind."""
    with store.transaction():
        row = store.conn.execute(
            "SELECT id FROM life_thread WHERE guid=? AND status='active' ORDER BY id LIMIT 1",
            (guid,)).fetchone()
        if row is not None:
            return row["id"]
        due = now + rng.uniform(1, 3) * DAY
        maximum = rng.randint(3, 8)
        tid = store.conn.execute(
            "INSERT INTO life_thread(guid,title,beat_n,beats_max,next_beat_at,status,started) "
            "VALUES (?,?,0,?,?,'active',?)", (guid, title, maximum, due, now)).lastrowid
        wowlife.refresh_on_mind(store, guid, dial=dial)
        return tid


def due_threads(store, now):
    with store.transaction():
        return store.conn.execute(
            "SELECT * FROM life_thread WHERE status='active' AND next_beat_at<=? "
            "ORDER BY next_beat_at,id", (now,)).fetchall()


def _has_beat(store, night, guid):
    return store.conn.execute(
        "SELECT 1 FROM life_beat b JOIN life_thread t ON t.id=b.thread_id "
        "WHERE b.night=? AND t.guid=? LIMIT 1", (night, guid)).fetchone() is not None


async def advance(ctx, thread):
    guid, tid = thread["guid"], thread["id"]
    unit = str(guid)
    if ctx.done(STAGE, unit):
        return
    with ctx.store.transaction():
        current = ctx.store.conn.execute("SELECT * FROM life_thread WHERE id=?", (tid,)).fetchone()
        already = _has_beat(ctx.store, ctx.night, guid)
        recent = ctx.store.conn.execute(
            "SELECT text FROM life_beat WHERE thread_id=? ORDER BY ts DESC,id DESC LIMIT 3",
            (tid,)).fetchall()
    if already:
        ctx.skip(STAGE, unit)
        return
    if (current is None or current["guid"] != guid or current["status"] != "active"
            or current["next_beat_at"] > ctx.clock.now()):
        return
    thread = current
    names = his_names(ctx.ident)
    they, them, _ = ctx.ident.forms
    system = (system_prompt("life", PROMPT, ctx.settings.data_dir, human=ctx.ident,
                            name=ctx.name(guid), card=ctx.card(guid))
              + "\n" + FAR_AWAY.format(human=ctx.ident.called, name=ctx.name(guid), they=they,
                                       them=them))
    recent = [r for r in recent if not meets_him(r["text"], names)]  # never primes the next
    user = (f"Life thread: {thread['title']}\nLast three beats:\n"
            + "\n".join(r["text"] for r in reversed(recent))
            + "\nOne sentence: what happened next in their life away from the game. "
            "Give its mood, whether the thread has resolved, and an optional next thread title.")
    reply = await ctx.reasoner.json(system, user, BEAT_SCHEMA, temperature=0.8)
    reply["beat"] = " ".join(strip_wow_markup(reply["beat"]).split())
    if reply["next_title"] is not None:
        reply["next_title"] = " ".join(strip_wow_markup(reply["next_title"]).split())
    count = thread["beat_n"] + 1
    resolved = (reply["resolved"] and count >= 3) or count >= thread["beats_max"]
    ref = f"life:{tid}"
    ev = Evidence()
    ev.add(Item(ref, thread["title"], frozenset({guid}), thread["started"]))
    claims = [Claim("life", guid, reply["beat"], [ref], valence=reply["mood"])]
    # A replacement title is persisted generated text too. It must pass the same
    # name and fiction invariants as the beat, even though it is not a memory.
    if resolved and reply["next_title"] is not None:
        claims.append(Claim("life", guid, reply["next_title"], [ref]))
    if any(meets_him(c.text, names) for c in claims):  # refused before the judge is asked
        verdicts = [(c, Verdict(False, "deterministic", "meets him offline")
                     if meets_him(c.text, names) else Verdict(True, "deterministic", ""))
                    for c in claims]
        ctx.commit(STAGE, unit, lambda store: log_verdicts(
            store, ctx.night, STAGE, verdicts, ctx.clock.now(), ctx.ident))
        return
    verdicts = await ground(ctx, claims, ev, load_facts(ctx))
    if not all(v.ok for _, v in verdicts):
        ctx.commit(STAGE, unit, lambda store: log_verdicts(
            store, ctx.night, STAGE, verdicts, ctx.clock.now(), ctx.ident))
        return
    vector = (await ctx.embed([reply["beat"]]))[0]
    now = ctx.clock.now()
    rng = ctx.rng("life", guid, tid)
    next_due = now + rng.uniform(2, 5) * DAY
    hidden = private_beat(reply["beat"], thread["title"], ctx.persona(guid), names)
    scope, scope_with = ("private", ctx.ident.guid) if hidden else ("open", 0)

    def write(store):
        latest = store.conn.execute("SELECT * FROM life_thread WHERE id=?", (tid,)).fetchone()
        if (latest is None or latest["status"] != "active"
                or any(latest[k] != thread[k] for k in
                       ("guid", "title", "beat_n", "beats_max", "next_beat_at"))
                or _has_beat(store, ctx.night, guid)):
            raise ValueError("Life thread changed while its next beat was generated")
        bid = store.conn.execute(
            "INSERT INTO life_beat(thread_id,night,text,ts) VALUES (?,?,?,?)",
            (tid, ctx.night, reply["beat"], now)).lastrowid
        evidence = (f"beat:{bid}",)
        ctx.writer.put(MemoryRow(
            id=None, owner=guid, kind="event", text=reply["beat"], t_event=int(now),
            t_created=int(now), t_accessed=int(now), access_n=0, actors=(), place=None,
            boss_id=None, raid_id=None, importance=6 if resolved else 5,
            valence=reply["mood"], source="offscreen", evidence=evidence, embedding=vector,
            scope=scope, scope_with=scope_with))
        mood = reply["mood"]
        if abs(mood) >= .2:
            store.conn.execute(
                "INSERT INTO emotion(guid,night,type,target,intensity,why,source,evidence,ts) "
                "VALUES (?,?,?,0,?,?,'life',?,?)",
                (guid, ctx.night, "joy" if mood > 0 else "distress",
                 2 if abs(mood) >= .6 else 1, reply["beat"][:80], json.dumps(evidence), now))
        store.conn.execute(
            "UPDATE life_thread SET beat_n=?,next_beat_at=?,status=?,resolved=? WHERE id=?",
            (count, next_due, "resolved" if resolved else "active", now if resolved else None, tid))
        if resolved:
            persona = load_persona(store, guid)
            title = reply["next_title"] if reply["next_title"] is not None else persona.real_life.life_thread
            ensure_thread(store, guid, title, now, rng, dial=ctx.settings.wow_vs_life)
        wowlife.refresh_on_mind(store, guid, dial=ctx.settings.wow_vs_life)
        log_verdicts(store, ctx.night, STAGE, verdicts, now, ctx.ident)

    ctx.commit(STAGE, unit, write)


async def stage_life(ctx):
    members = set(ctx.members(include_away=True))
    now = ctx.clock.now()
    for guid in sorted(members):
        ensure_thread(ctx.store, guid, ctx.persona(guid).real_life.life_thread, now,
                      ctx.rng("life_thread", guid), dial=ctx.settings.wow_vs_life)
    by_guid = {}
    for thread in due_threads(ctx.store, now):
        if thread["guid"] in members:
            by_guid.setdefault(thread["guid"], thread)
    await ctx.map_units(STAGE, [str(guid) for guid in by_guid],
                        lambda unit: advance(ctx, by_guid[int(unit)]))


def _count(ctx):
    # Include overdue threads: a catch-up run advances one beat, never the entire
    # backlog. Counting only threads born within the window would underbudget it.
    members = set(ctx.members(include_away=True))
    return len({row["guid"] for row in due_threads(ctx.store, ctx.window[1])
                if row["guid"] in members and not ctx.done(STAGE, str(row["guid"]))})


register_counter(STAGE, _count)
