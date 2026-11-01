"""The bounded attraction curve: observable live acts, slow nightly decay, undoable bands.

No model output can subtract attraction. Offline time is not neglect. Persona changes use
the stored version, and callers invalidate their cache only after the transaction commits.
"""
import hashlib
import json
import math
import re
from dataclasses import replace

from leeroy_mind.logs import log
from leeroy_mind.memory.recall import fts_query
from leeroy_mind.nightly.audience import audience_of
from leeroy_mind.nightly.evidence import CONTENT_RE, STOPWORDS, content_words
from leeroy_mind.nightly.knowledge import roots
from leeroy_mind.nightly.reactions import (ANSWERED, LAUGH_RE, LIFE_RE, REPLY_S,
                                          WHISPER_BACK_S, first_after, laughs_at, reactions)
from leeroy_mind.nightly.runner import ATTITUDES
from leeroy_mind.nightly.settings import night_bounds, night_of_ts
from leeroy_mind.nightly.store import ledger_add
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.screens import has_word
from leeroy_mind.personas.stances import is_woman
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.wow import BOSS_NAMES, CAST_MIN_LEVEL, CLASSES, RACES, is_raid_fight

UP = (("lust", 60.0), ("drawn", 40.0))
DOWN = {"lust": 50.0, "drawn": 30.0}
FLOOR, CAP = 20.0, 100.0
INIT = {"lust": 60.0, "drawn": 40.0, "fond": 20.0}
HALF_LIFE_DAYS, HURT_DAYS, HURT_FACTOR, DEEPER = 213.0, 14.0, 2.0, 2.0
ACT_GAIN = {"flirt": 20.0, "public_compliment": 5.0, "remembered": 5.0,
            "answered": 3.0, "quest_together": 2.0}
TASTE, MULT_CAP = 2.0, 2.0
ANSWER_S, ANSWERED_PER_DAY, REMEMBER_MIN_WORDS = 60.0, 3, 2
RUSH, RUSH_S, RUSH_WIPES = 1.5, 1800.0, 3
SIMILARITY_BOOST, SOCIAL_PROOF, SCARCITY, SCIENCE_CAP = .25, 1.2, 1.2, 1.5
TOGETHER_MIN_S, TOGETHER_BLOCK_S = 600.0, 1800.0
REMEMBER_AGE_S = (43200.0, 2592000.0)
FINAL_BOSSES = frozenset({(631, 12), (649, 4), (603, 12), (603, 13), (724, 6)})
DAY_S = 86400.0
ACT_WORDS = {
    "flirt": ("flirting", "flirt", "flirty", "compliments", "being complimented", "attention", "being wanted", "whispers", "teasing back"),
    "public_compliment": ("compliments", "being complimented", "praise", "being praised", "being noticed", "recognition", "attention"),
    "remembered": ("being remembered", "remembering", "being listened to", "listening", "being heard", "details", "thoughtful"),
    "answered": ("replies", "being answered", "answers", "attention", "conversation", "banter", "teasing back", "chat"),
    "quest_together": ("questing together", "questing", "quests", "helping", "adventure", "exploring", "teamwork")}
NEGLECT_WORDS = ("being ignored", "ignored", "no reply", "no replies", "silence", "being left hanging", "being dismissed")
HURT_WORDS = ("rudeness", "rude", "being dismissed", "dismissive", "mean", "insults", "being mocked", "mockery", "cruelty", "yelling")
FLIRT_RE = re.compile(r"\b(you'?re|you are|ur|you look|you sound)\s+(so\s+|really\s+|pretty\s+|kinda\s+)?(cute|gorgeous|beautiful|pretty|sexy|hot|adorable|lovely)\b|\b(cutie|babe|darling|sweetheart)\b|\b(hey|hi|morning|night|gn|thanks|ty)\s+(gorgeous|beautiful)\b|\b(miss(ed)? you(?!\s+(guys|all|both|two|lot|people))|thinking (about|of) you|dream(t|ed)? (about|of) you|can'?t stop thinking about you|wish you were here|kiss(es)?|blush(ing)?|crush on you)\b", re.I)
FLIRT_EMOTE_RE = re.compile(r";-?\)|<3|:\*|\bxoxo\b", re.I)
COMPLIMENT_RE = re.compile(r"\b(gj|good job|great job|nice (job|one|heals?|tanking|dps|pull|work|save)|well done|ty+ for (the )?(heals?|help|carry|save)|carried|mvp|clutch|beast|legend|best (healer|tank|dps|player|raider)|so good|amazing|awesome|nailed it|godlike|insane)\b", re.I)
HURT_RE = re.compile(r"\b(stfu|shut up|go away|leave me alone|stop (whispering|messaging|bugging|pestering) me|nobody asked|no one asked|not interested|you'?re (so )?(annoying|clingy|needy|ugly|stupid|dumb|useless|cringe)|get lost|piss off|f+ off|fuck off|screw you)\b", re.I)
TEASE_RE = re.compile(r"\b(hilarious|funny|jk|kidding|love (you|u|ya)|cutie)\b|:p\b|\bxd\b", re.I)
GAME_WORDS = frozenset("heals healer healing tanks tanking pulls pulled loot drops dungeon heroic raids bosses wiped wipes quest quests level spec talents gear server druid paladin priest shaman warrior warlock hunter rogue mage death knight".split()) | frozenset(
    word for name in BOSS_NAMES.values() for word in name.casefold().split())


def band_for(a: float, stance: str) -> str:
    if a >= 60 or stance == "lust" and a >= 50:
        return "lust"
    if a >= 40 or stance in ("lust", "drawn") and a >= 30:
        return "drawn"
    return "fond"


def rebase(stance) -> float:
    return INIT[stance]


def matches(items, words) -> bool:
    return any(has_word(item, word) for item in items for word in words)


def taste(p, act) -> float:
    return TASTE if matches(p.likes + p.romance.drawn_to, ACT_WORDS[act]) else 1.0


def deep(p, what) -> bool:
    return matches(p.dislikes + p.romance.turned_off_by,
                   NEGLECT_WORDS if what == "neglect" else HURT_WORDS)


def science(*, rush: bool, similarity: float, social: bool, scarcity: bool) -> float:
    return min(SCIENCE_CAP, (RUSH if rush else 1.0) * (1 + SIMILARITY_BOOST * similarity)
               * (SOCIAL_PROOF if social else 1.0) * (SCARCITY if scarcity else 1.0))


def gain(act, p, *, science: float) -> tuple[float, float]:
    mult = min(MULT_CAP, taste(p, act) * science)
    return round(ACT_GAIN[act] * mult, 1), mult


def decay(a, days, half_life) -> float:
    return a if a <= FLOOR else FLOOR + (a - FLOOR) * 2 ** (-days / half_life)


def night_half_life(p, *, neglected, hurt_tonight, in_hurt_window, hurt_deep) -> float:
    hurt = hurt_tonight or in_hurt_window
    return (HALF_LIFE_DAYS / (HURT_FACTOR if hurt else 1)
            / (DEEPER if (neglected and deep(p, "neglect")) or (hurt and hurt_deep) else 1))


def aimed_at(row, p, guid) -> bool:
    first = p.identity.real_name.split()[0] if p.identity.real_name.split() else ""
    return ((row["channel"] == "whisper" and row["to_guid"] == guid)
            or has_word(row["text"], p.character_name)
            or has_word(row["text"], first.title(), case_sensitive=True))


def _state(store, guid):
    return store.conn.execute("SELECT * FROM attraction_state WHERE guid = ?", (guid,)).fetchone()


def _receipt_key(human, guid, line_id):
    return f"attraction_chat:{human}:{guid}:{line_id}"


def reconcile(store, guid, *, now) -> tuple[str, bool] | None:
    p = load_persona(store, guid)
    if p is None or not is_woman(p) or p.romance.stance not in ATTITUDES:
        return None
    stance, version = p.romance.stance, store.persona_versions()[guid]
    row = _state(store, guid)
    moved = row is None or (row["version"] < version and row["stance"] != stance)
    if moved:
        human = int(store.get_meta("human_guid") or 0)
        rel = store.get_relationship(guid, human)
        seed = rebase(stance)
        store.put_relationship(replace(rel, attraction=seed))
        if row is None:
            store.conn.execute("INSERT INTO attraction_state(guid,stance,version,updated) VALUES (?,?,?,?)",
                               (guid, stance, version, now))
            if rel.attraction != seed:
                ledger_add(store, guid=guid, kind="stance_band", text=f"{stance}: first sight at {int(seed)}",
                           evidence=[], now=now)
        else:
            ledger_add(store, guid=guid, kind="stance_band", text=f"{row['stance']} -> {stance}: hand move",
                       evidence=[], now=now)
    if row is not None and row["version"] < version:
        store.conn.execute("UPDATE attraction_state SET stance=?, version=?, updated=? WHERE guid=?",
                           (stance, version, now, guid))
    return stance, moved


def _save_stance(store, guid, new, now):
    p, member = load_persona(store, guid), store.cast()[guid]
    version = save_persona(store, replace(p, romance=replace(p.romance, stance=new)),
                           source="generated", ts=now, race=RACES[member.race], class_name=CLASSES[member.class_])
    store.conn.execute("UPDATE attraction_state SET stance=?, version=?, updated=? WHERE guid=?",
                       (new, version, now, guid))
    return version


def set_stance(store, guid, new, *, now, reason, evidence, score_before) -> int:
    old = load_persona(store, guid).romance.stance
    version = _save_stance(store, guid, new, now)
    ledger_add(store, guid=guid, kind="stance_band", text=f"{old} -> {new}: {reason}",
               evidence=evidence, now=now, score_before=score_before)
    return version


def apply_act(store, *, human, guid, act, amount, mult, ref, ts, live, stance, now=None) -> str | None:
    store.conn.execute("INSERT INTO attraction_act(guid,act,gain,mult,ref,ts,live) VALUES (?,?,?,?,?,?,?)",
                       (guid, act, amount, mult, ref, ts, int(live)))
    rel = store.get_relationship(guid, human)
    score = min(CAP, rel.attraction + amount)
    store.put_relationship(replace(rel, attraction=score))
    store.add_rel_delta(guid, human, "attraction", amount, f"act: {act}", [ref], ts, applied=True)
    new = band_for(score, stance)
    if new != stance:
        set_stance(store, guid, new, now=ts if now is None else now,
                   reason=f"act {act}", evidence=[ref], score_before=rel.attraction)
        return new
    return None


def mark_hurt(store, *, guid, ref, ts, live):
    row, p = _state(store, guid), load_persona(store, guid)
    store.conn.execute("INSERT INTO attraction_act(guid,act,gain,mult,ref,ts,live) VALUES (?,'hurt',0,1,?,?,?)",
                       (guid, ref, ts, int(live)))
    held = row["hurt_until"] > ts and row["hurt_deep"]
    store.conn.execute("UPDATE attraction_state SET hurt_until=?,hurt_deep=?,updated=? WHERE guid=?",
                       (max(row["hurt_until"], ts + HURT_DAYS * DAY_S), int(held or deep(p, "hurt")), ts, guid))


def restore_stance(store, ledger_row, *, now) -> bool:
    row = ledger_row
    match = re.fullmatch(r"(fond|drawn|lust) -> (fond|drawn|lust): .+", row["text"])
    if match is None or row["score_before"] is None:
        return False
    guid, old = row["guid"], match.group(1)
    if load_persona(store, guid) is None:
        return False
    _save_stance(store, guid, old, now)
    human = int(store.get_meta("human_guid") or 0)
    store.put_relationship(replace(store.get_relationship(guid, human), attraction=row["score_before"]))
    return True


def science_for(store, guid, *, human, now, local) -> float:
    """Shared current multiplier for chat, party/raid time and optional voice room time.

    `local` is the mind clock's aware datetime, not the host's timezone. The caller has
    reconciled the member in its transaction; this function always reads the stored persona.
    """
    p, state, roster = load_persona(store, guid), _state(store, guid), store.cast()
    laughed = any(human in laughs_at(store, r["id"]) for r in store.conn.execute(
        "SELECT id FROM chat_line WHERE from_guid=? AND ts>=? AND ts<=?",
        (guid, now - 3600, now)).fetchall())
    similarity = (int(guid in roster and human in roster and roster[guid].class_ == roster[human].class_)
                  + int(p.real_life.night_owl and 1 <= local.hour < 6) + int(laughed)) / 3
    others = set()
    for row in store.conn.execute(
            "SELECT guid,ref FROM attraction_act WHERE guid != ? AND ts>=? AND ts<=? "
            "AND act IN ('answered','public_compliment','flirt')", (guid, now - 3600, now)):
        if row["ref"].startswith("chat:") and guid in audience_of(store, int(row["ref"][5:])):
            others.add(row["guid"])
    return science(rush=state["rush_until"] > now, similarity=similarity, social=len(others) >= 2,
                   scarcity=state["bids_7d"] >= 3 and state["answer_rate"] is not None
                   and .3 <= state["answer_rate"] <= .7)


class LiveAttraction:
    def __init__(self, store, personas, clock, ident):
        self.store, self.personas, self.clock, self.ident = store, personas, clock, ident
        self._women = None
        self._women_at = float("-inf")
        self._women_versions = {}

    def women(self):
        now = self.clock.now()
        versions = self.store.persona_versions()
        if self._women is None or now - self._women_at >= 60 or versions != self._women_versions:
            self._women = {g: p for g, m in self.store.cast().items()
                           if g != self.ident().guid and m.level >= CAST_MIN_LEVEL
                           and (p := load_persona(self.store, g)) is not None
                           and is_woman(p) and p.romance.stance in ATTITUDES}
            self._women_at = now
            self._women_versions = versions
        return dict(self._women)

    def _invalidate(self, changed):
        for guid in changed:
            self.personas.invalidate(guid)
        if changed:
            self._women = None

    def _used(self, guid, act, ref):
        return self.store.conn.execute("SELECT 1 FROM attraction_act WHERE guid=? AND act=? AND ref=? LIMIT 1",
                                       (guid, act, ref)).fetchone() is not None

    def _remembered(self, row, p, now):
        query = fts_query(row["text"])
        if not query:
            return None
        human = self.ident()
        names = [m.name for m in self.store.cast().values()] + [human.called, human.real_name]
        words = content_words(row["text"], names) - GAME_WORDS
        for mid, _ in self.store.fts(p.guid, query, 10):
            memory = self.store.get_memories([mid])[0]
            ref = f"memory:{mid}"
            if (memory.status != "active" or not REMEMBER_AGE_S[0] <= now - memory.t_created <= REMEMBER_AGE_S[1]
                    or self._used(p.guid, "remembered", ref)
                    or len(words & (content_words(memory.text, names) - GAME_WORDS)) < REMEMBER_MIN_WORDS):
                continue
            for ev in memory.evidence:
                if not ev.startswith("chat:") or not ev[5:].isdigit():
                    continue
                line = self.store.conn.execute("SELECT * FROM chat_line WHERE id=?", (int(ev[5:]),)).fetchone()
                if line is None or line["from_guid"] != p.guid:
                    continue
                visible = ((line["channel"] == "whisper" and line["to_guid"] == human.guid)
                           or (line["channel"] != "whisper" and
                               any(has_word(line["text"], n) for n in (human.called, human.character_name))))
                disclosed = self.store.conn.execute(
                    "SELECT 1 FROM disclosure WHERE guid=? AND other=? AND ref=? LIMIT 1",
                    (p.guid, human.guid, ev)).fetchone() is not None
                if visible and (LIFE_RE.search(line["text"]) or disclosed):
                    return ref
        return None

    def _answered(self, row, p, now):
        start, end = night_bounds(night_of_ts(now, self.clock.local().tzinfo), self.clock.local().tzinfo)
        if self.store.conn.execute(
                "SELECT count(*) FROM attraction_act WHERE guid=? AND act='answered' AND ts>=? AND ts<?",
                (p.guid, start, end)).fetchone()[0] >= ANSWERED_PER_DAY:
            return None
        human = self.ident()
        names = {g: m.name for g, m in self.store.cast().items()}
        lines = self.store.conn.execute(
            "SELECT * FROM chat_line WHERE origin='mind' AND from_guid=? AND ts>=? "
            "AND (ts<? OR (ts=? AND id<?)) ORDER BY ts DESC,id DESC",
            (p.guid, now - WHISPER_BACK_S, row["ts"], row["ts"], row["id"])).fetchall()
        for line in lines:
            ref = f"chat:{line['id']}"
            age = row["ts"] - line["ts"]
            whisper = row["channel"] == "whisper" and row["to_guid"] == p.guid
            named = (row["channel"] != "whisper" and row["channel"] == line["channel"]
                     and aimed_at(row, p, p.guid) and age <= REPLY_S)
            bid = ((line["channel"] == "whisper" and line["to_guid"] == human.guid)
                   or (line["channel"] != "whisper" and any(has_word(line["text"], n)
                       for n in (human.called, human.character_name))))
            unnamed = (row["channel"] != "whisper" and row["channel"] == line["channel"]
                       and bid and age <= ANSWER_S and first_after(self.store, line, row)
                       and not any(has_word(row["text"], name) for g, name in names.items()
                                   if g not in (p.guid, human.guid)))
            if whisper or named or unnamed:
                return None if self._used(p.guid, "answered", ref) else ref
        return None

    def classify(self, row, women, *, now):
        acts = []
        for guid in women:
            if row["channel"] == "whisper" and row["to_guid"] != guid:
                continue
            # Candidate caching must not make an imported stance or name stale.
            p = load_persona(self.store, guid)
            if p is None or not is_woman(p) or p.romance.stance not in ATTITUDES:
                continue
            aimed, text, ref = aimed_at(row, p, guid), row["text"], f"chat:{row['id']}"
            if aimed and HURT_RE.search(text) and not (TEASE_RE.search(text) or LAUGH_RE.search(text)):
                act = "hurt"
            elif (aimed and FLIRT_RE.search(text)) or (
                    row["channel"] == "whisper" and row["to_guid"] == guid and FLIRT_EMOTE_RE.search(text)):
                act = "flirt"
            elif aimed and (memory := self._remembered(row, p, now)):
                act, ref = "remembered", memory
            elif row["channel"] != "whisper" and aimed and COMPLIMENT_RE.search(text):
                act = "public_compliment"
            elif answer := self._answered(row, p, now):
                act, ref = "answered", answer
            else:
                continue
            if not self._used(guid, act, ref):
                acts.append((guid, act, ref))
        return acts

    async def on_chat(self, ev):
        if ev.from_guid != self.ident().guid or ev.via_bridge:
            return
        now, human = self.clock.now(), self.ident().guid
        # The server timestamp is milliseconds, while chat_line.ts is the receipt clock.
        # Remember the original event identity; guessing timestamp equality would lose real
        # lines under network latency, and text alone could select a later identical line.
        fingerprint = hashlib.sha256(json.dumps(
            [ev.ts, ev.channel, ev.to_guid, ev.text, ev.map, ev.zone],
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        women, previous = self.women(), {}
        for guid in list(women):
            key = f"attraction_chat_last:{human}:{guid}"
            last = json.loads(self.store.get_meta(key) or "null")
            previous[guid] = last
            if last is not None and (
                    (ev.ts > 0 and ev.ts < last["event_ms"])
                    or (ev.ts == last["event_ms"] and fingerprint == last["fingerprint"]
                        and self.store.get_meta(_receipt_key(human, guid, last["line_id"])) is not None)):
                women.pop(guid)
        if not women:
            return
        row = self.store.conn.execute(
            "SELECT * FROM chat_line WHERE origin='human' AND from_guid=? AND channel=? AND to_guid=? AND text=? "
            "ORDER BY ts DESC,id DESC LIMIT 1", (human, ev.channel, ev.to_guid, ev.text)).fetchone()
        if row is None:
            return
        women = {g: p for g, p in women.items()
                 if (previous[g] is None or row["id"] > previous[g]["line_id"])
                 and self.store.get_meta(_receipt_key(human, g, row["id"])) is None}
        for guid, act, ref in self.classify(row, women, now=now):
            changed, amount = set(), 0.
            with self.store.transaction():
                reconciled = reconcile(self.store, guid, now=now)
                if reconciled is None:
                    continue
                stance, moved = reconciled
                if moved:
                    changed.add(guid)
                if act == "hurt":
                    mark_hurt(self.store, guid=guid, ref=ref, ts=now, live=True)
                else:
                    amount, mult = gain(act, load_persona(self.store, guid), science=science_for(
                        self.store, guid, human=human, now=now, local=self.clock.local()))
                    if apply_act(self.store, human=human, guid=guid, act=act, amount=amount,
                                 mult=mult, ref=ref, ts=now, live=True, stance=stance):
                        changed.add(guid)
                # Source receipts preserve the contract ref (her bid / her memory) while
                # making callback replay and later extraction of his line idempotent.
                self.store.set_meta(_receipt_key(human, guid, row["id"]), str(ev.ts))
                self.store.set_meta(f"attraction_chat_last:{human}:{guid}", json.dumps({
                    "line_id": row["id"], "event_ms": ev.ts, "fingerprint": fingerprint}))
            self._invalidate(changed)
            log("attraction_act", guid=guid, act=act, gain=amount)

    def _close_together(self, guid, since, now, changed):
        self.store.conn.execute("UPDATE attraction_state SET together_since=NULL,updated=? WHERE guid=?",
                               (now, guid))
        elapsed = now - since
        if elapsed < TOGETHER_MIN_S:
            return
        reconciled = reconcile(self.store, guid, now=now)
        if reconciled is None:
            return
        stance, moved = reconciled
        if moved:
            changed.add(guid)
        human = self.ident().guid
        amount, mult = gain("quest_together", load_persona(self.store, guid), science=science_for(
            self.store, guid, human=human, now=now, local=self.clock.local()))
        amount *= max(1, math.floor(elapsed / TOGETHER_BLOCK_S))
        if apply_act(self.store, human=human, guid=guid, act="quest_together", amount=amount,
                     mult=mult, ref=f"session:{self.store.open_session() or 0}", ts=now,
                     live=True, stance=stance):
            changed.add(guid)

    async def on_group(self, ev):
        # Later voice-plan ruling NP2: party AND raid time counts. Voice rooms exclude both.
        now, changed = self.clock.now(), set()
        wanted = set(ev.members) & self.women().keys() if self.ident().guid in ev.members else set()
        with self.store.transaction():
            for row in self.store.conn.execute(
                    "SELECT guid,together_since FROM attraction_state WHERE together_since IS NOT NULL").fetchall():
                if row["guid"] not in wanted:
                    self._close_together(row["guid"], row["together_since"], now, changed)
            for guid in wanted:
                reconciled = reconcile(self.store, guid, now=now)
                if reconciled is None:
                    continue
                if reconciled[1]:
                    changed.add(guid)
                self.store.conn.execute("UPDATE attraction_state SET together_since=coalesce(together_since,?), "
                                        "updated=? WHERE guid=?", (now, now, guid))
        self._invalidate(changed)

    async def on_boss(self, ev):
        human, now = self.ident().guid, self.clock.now()
        if ev.state != "killed" or human not in ev.members or not is_raid_fight(ev.map, ev.boss_id):
            return
        start, _ = night_bounds(night_of_ts(now, self.clock.local().tzinfo), self.clock.local().tzinfo)
        wipes = sum(1 for row in self.store.conn.execute(
            "SELECT payload FROM game_event WHERE type='boss' AND ts>=? AND ts<=?", (start, now))
            if (p := json.loads(row["payload"])).get("map") == ev.map
            and p.get("boss_id") == ev.boss_id and p.get("state") == "wiped")
        if (ev.map, ev.boss_id) not in FINAL_BOSSES and wipes < RUSH_WIPES:
            return
        changed = set()
        with self.store.transaction():
            for guid in set(ev.members) & self.women().keys():
                reconciled = reconcile(self.store, guid, now=now)
                if reconciled is not None:
                    if reconciled[1]:
                        changed.add(guid)
                    self.store.conn.execute("UPDATE attraction_state SET rush_until=?,updated=? WHERE guid=?",
                                            (now + RUSH_S, now, guid))
        self._invalidate(changed)

    async def on_logout(self, ev):
        if ev.guid != self.ident().guid:
            return
        now, changed = self.clock.now(), set()
        with self.store.transaction():
            for row in self.store.conn.execute(
                    "SELECT guid,together_since FROM attraction_state WHERE together_since IS NOT NULL").fetchall():
                self._close_together(row["guid"], row["together_since"], now, changed)
        self._invalidate(changed)

    async def on_hello(self, ev):
        with self.store.transaction():
            self.store.conn.execute("UPDATE attraction_state SET together_since=NULL")
        self._women = None


def reflection_row(owner, text, *, now, actors, evidence, key, importance, valence, event=None):
    """A deterministic nightly reflection; MemoryWriter derives its privacy from the roots."""
    return MemoryRow(id=None, owner=owner, kind="reflection", text=text,
                     t_event=int(now if event is None else event), t_created=int(now), t_accessed=int(now),
                     access_n=0, actors=tuple(sorted(set(actors))), place=None, boss_id=None,
                     raid_id=None, importance=importance, valence=valence, source="reflection",
                     evidence=tuple(dict.fromkeys(evidence)), key=key)


def _nights(window, tz, active_dates):
    """Only observed neglect/hurt dates can decay; an initial window may start at epoch."""
    for night in sorted(set(active_dates)):
        start, end = night_bounds(night, tz)
        if start < window[1] and end > window[0]:
            yield night, max(start, window[0]), min(end, window[1]), end


def _rival_line(store, refs, owner, human, attitudes):
    for ref in refs:
        if not ref.startswith("chat:") or not ref[5:].isdigit():
            continue
        row = store.conn.execute("SELECT * FROM chat_line WHERE id=?", (int(ref[5:]),)).fetchone()
        if row is None or row["from_guid"] != human or row["origin"] != "human":
            continue
        if any(g != owner and (row["channel"] != "whisper" or row["to_guid"] == g)
               and aimed_at(row, load_persona(store, g), g) for g in attitudes):
            return ref
    return None


def _noticed_at(store, refs, human, recorded, window):
    """Extraction is stamped when processed; the interaction belongs to its source night.

    Prefer cited human chat, then other dated evidence. Preserve known timestamps even
    outside this window (such delayed gains are not this window's attraction_log.gained).
    Only undated evidence uses a logical application time clipped inside the closed window.
    """
    evidence = set(refs) | roots(store, refs)
    chats, other = [], []
    for ref in evidence:
        kind, _, ident = ref.partition(":")
        if not ident.isdigit():
            continue
        if kind == "chat":
            row = store.conn.execute("SELECT ts,from_guid,origin FROM chat_line WHERE id=?", (int(ident),)).fetchone()
            if row is not None:
                chats.append(row)
        elif kind in ("event", "memory"):
            table, col = ("game_event", "ts") if kind == "event" else ("memory", "t_event")
            row = store.conn.execute(f"SELECT {col} FROM {table} WHERE id=?", (int(ident),)).fetchone()
            if row is not None:
                other.append(row[0])
    his = [r["ts"] for r in chats if r["from_guid"] == human and r["origin"] == "human"]
    if his:
        return max(his)
    if chats or other:
        return max([r["ts"] for r in chats] + other)
    return max(window[0], min(recorded, math.nextafter(window[1], window[0])))


async def stage_attraction(ctx):
    if ctx.done("attraction", "all"):
        return
    store, now, human = ctx.store, ctx.clock.now(), ctx.human
    attitudes = ctx.attitudes(fresh=True)
    changed = set()

    def write(store):
        ledger_start = store.conn.execute("SELECT coalesce(max(id),0) FROM persona_ledger").fetchone()[0]
        before, stance_before = {}, {}
        for guid in attitudes:
            stance, moved = reconcile(store, guid, now=now)
            attitudes[guid] = stance
            before[guid] = store.get_relationship(guid, human).attraction
            stance_before[guid] = stance
            if moved:
                changed.add(guid)
        for row in store.pending_rel_deltas():
            if row["dim"] != "attraction" or row["b"] != human:
                continue
            guid, amount, reason = row["a"], row["delta"], row["reason"]
            refs = json.loads(row["evidence"])
            if guid not in attitudes:
                amount, reason = 0., reason + " (not applied: no attitude)"
            elif amount > 0:
                counted = any(ref.startswith("chat:") and (
                    store.get_meta(_receipt_key(human, guid, ref[5:])) is not None or store.conn.execute(
                    "SELECT 1 FROM attraction_act WHERE guid=? AND ref=? LIMIT 1", (guid, ref)).fetchone())
                    for ref in refs)
                if counted:
                    reason += " (already counted live)"
                else:
                    ref = refs[0] if refs else f"rule:noticed:{row['id']}"
                    moved = apply_act(store, human=human, guid=guid, act="noticed", amount=amount,
                                      mult=1., ref=ref, ts=_noticed_at(store, refs, human, row["ts"], ctx.window),
                                      live=False, stance=attitudes[guid], now=now)
                    if moved:
                        attitudes[guid] = moved
                        changed.add(guid)
            elif amount <= -2 and (rival := _rival_line(store, refs, guid, human, attitudes)):
                reason += f" (rivalry, not hurt: {rival})"
            store.conn.execute("UPDATE rel_delta SET applied=1,delta=?,reason=? WHERE id=?",
                               (amount, reason, row["id"]))
        names = {g: m.name for g, m in store.cast().items()}
        names[human] = ctx.ident.called
        rs = reactions(store, ctx.window, human, names)
        recent_rs = reactions(store, (now - 7 * DAY_S, now + .001), human, names)
        for guid in attitudes:
            p = load_persona(store, guid)
            score = store.get_relationship(guid, human).attraction
            decayed, neglected_n, hurt_n, half = 0., 0, 0, HALF_LIFE_DAYS
            acts = store.conn.execute("SELECT * FROM attraction_act WHERE guid=? AND ts>=? AND ts<?",
                                      (guid, *ctx.window)).fetchall()
            active_dates = {night_of_ts(r.ts, ctx.tz) for r in rs
                            if r.speaker == guid and r.reaction == "ignored"}
            active_dates.update(night_of_ts(r["ts"], ctx.tz) for r in acts if r["act"] == "hurt")
            for night, start, end, night_end in _nights(ctx.window, ctx.tz, active_dates):
                bids = [r for r in rs if r.speaker == guid and start <= r.ts < end]
                ignored = [r for r in bids if r.reaction == "ignored"]
                night_acts = [r for r in acts if start <= r["ts"] < end]
                hurt = [r for r in night_acts if r["act"] == "hurt"]
                neglected = bool(ignored) and not any(r.reaction in ANSWERED for r in bids) and not night_acts
                if not neglected and not hurt:
                    continue
                in_window = store.conn.execute(
                    "SELECT 1 FROM attraction_act WHERE guid=? AND act='hurt' AND ts>=? AND ts<? LIMIT 1",
                    (guid, night_end - HURT_DAYS * DAY_S, night_end)).fetchone() is not None
                half = night_half_life(p, neglected=neglected, hurt_tonight=bool(hurt),
                                       in_hurt_window=in_window, hurt_deep=bool(_state(store, guid)["hurt_deep"]))
                old, score = score, decay(score, 1, half)
                decayed += old - score
                neglected_n += int(neglected)
                hurt_n += int(bool(hurt))
                refs = [r["ref"] for r in hurt] if hurt else [f"chat:{r.line_id}" for r in ignored]
                text = (f"{ctx.ident.called} was harsh with me tonight." if hurt else
                        f"{ctx.ident.called} didn't answer me tonight.")
                ctx.writer.put(reflection_row(guid, text, now=now, actors=[human], evidence=refs,
                    key=f"hurt:{night}", importance=5 if hurt else 4, valence=-.6 if hurt else -.4,
                    event=min(end, now)))
                store.conn.execute(
                    "INSERT INTO emotion(guid,night,type,target,intensity,why,source,evidence,ts) "
                    "VALUES (?,?,'distress',?,?,?,'attraction',?,?)",
                    (guid, night, human, 2 if hurt else 1, text, json.dumps(refs), now))
                new = band_for(score, attitudes[guid])
                if new != attitudes[guid]:
                    set_stance(store, guid, new, now=now, reason="decay", evidence=refs, score_before=old)
                    attitudes[guid] = new
                    changed.add(guid)
            store.put_relationship(replace(store.get_relationship(guid, human), attraction=score))
            recent = [r for r in recent_rs if r.speaker == guid and r.addressed]
            answered = sum(r.reaction in ANSWERED for r in recent)
            ignored = sum(r.reaction == "ignored" for r in recent)
            n = answered + ignored
            store.conn.execute("UPDATE attraction_state SET answer_rate=?,bids_7d=?,updated=? WHERE guid=?",
                               (answered / n if n else None, n, now, guid))
            store.conn.execute(
                "INSERT INTO attraction_log(night,guid,gained,decayed,neglected,hurt,half_life,before,after,"
                "stance_before,stance_after) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ctx.night, guid, sum(r["gain"] for r in acts), decayed, neglected_n, hurt_n, half,
                 before[guid], score, stance_before[guid], attitudes[guid]))
        # These ids identify the observation window's band writes even when execution's
        # wall-clock timestamp lies in the following night's natural window.
        band_ids = [r[0] for r in store.conn.execute(
            "SELECT id FROM persona_ledger WHERE id>? AND kind='stance_band' AND score_before IS NOT NULL",
            (ledger_start,))]
        store.set_meta(f"attraction_band_changes:{ctx.night}", json.dumps(band_ids))

    if ctx.commit("attraction", "all", write):
        for guid in changed:
            ctx.personas.invalidate(guid)
