"""Code-built event facts; the reflection model may supply only a short label."""
from dataclasses import dataclass
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping

EVENT_KINDS = ("wipe_first_death", "close_loot", "his_claim", "laughed_line")
RETIRE_USES = 12
COLD_DAYS = 60
JOKES_TARGET = 8
WIPE_RECHECK_ROWS = 4096

@dataclass(frozen=True, slots=True)
class Origin:
    kind: str
    refs: tuple[str, ...]
    butt: int
    originator: int
    when: float


def _object(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def loot_record(conn, ref):
    """A receipt is public game evidence only with its unique matching debit."""
    if not isinstance(ref, str) or not re.fullmatch(r"loot:(close_loot|his_claim):guild:loot:[0-9]+:[a-f0-9]+", ref):
        return None
    key = ref.split(":", 2)[2]
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    record = _object(row[0])
    try:
        award = record["award"]
        if record["state"] != "charged" or type(award["guid"]) is not int or award["guid"] <= 0:
            return None
        debits = conn.execute("SELECT ts,delta FROM dkp_ledger WHERE reason='item' AND note=? "
            "AND raid_id=? AND item_id=? AND guid=?", (key, int(key.split(":")[2]),
            record["item_id"], award["guid"])).fetchall()
        if len(debits) != 1 or debits[0][1] != -award["paid"] or award["paid"] < 0:
            return None
        return record, debits[0][0]
    except (KeyError, TypeError, ValueError):
        return None


def _wipe_origins(conn, window, *, limit=None):
    """Only game evidence, with an indexed bounded read for use-time rechecks."""
    found, pulls, firsts = [], {}, {}
    rows = conn.execute("SELECT * FROM game_event WHERE ts>=? AND ts<? ORDER BY ts,id LIMIT ?",
                        (*window, -1 if limit is None else limit + 1)).fetchall()
    if limit is not None and len(rows) > limit:
        return []  # Too much evidence to recheck promptly never becomes a guessed fact.
    for row in rows:
        p = _object(row["payload"])
        try:
            if row["type"] == "boss":
                key = (row["session_id"], p["map"], p["instance_id"], p["boss_id"])
                if p["state"] in ("started", "engaged"):
                    pulls[key] = (row, p, [])
                elif p["state"] in ("wiped", "killed"):
                    pull = pulls.pop(key, None)
                    if p["state"] == "wiped" and pull and pull[2]:
                        death = pull[2][0]
                        guid = death[1]["guid"]
                        if guid in p["members"]:
                            firsts.setdefault((p["map"], p["boss_id"], guid), []).extend((pull[0], death[0], row))
            elif row["type"] == "death":
                candidates = [v for k,v in pulls.items() if k[0] == row["session_id"]
                    and k[1] == p["map"] and p["guid"] in v[1]["members"]]
                # A death without an unambiguous active pull cannot establish first death.
                if len(candidates) == 1:
                    candidates[0][2].append((row, p))
        except (KeyError, TypeError):
            continue
    for (_, _, guid), rows in firsts.items():
        if len(rows) >= 6:
            found.append(Origin("wipe_first_death", tuple(f"event:{r['id']}" for r in rows),
                                guid, guid, rows[-1]["ts"]))
    return found


def event_origins(store, window: tuple[float, float]) -> list[Origin]:
    conn = store.conn
    found = _wipe_origins(conn, window)
    human = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
    try:
        human = int(human[0]) if human else 0
    except (TypeError, ValueError):
        human = 0
    for row in conn.execute("SELECT key FROM meta WHERE key GLOB 'guild:loot:*' ORDER BY key"):
        ref = "loot:close_loot:" + row[0]
        receipt = loot_record(conn, ref)
        if receipt is None or not window[0] <= receipt[1] < window[1]:
            continue
        rec, when = receipt
        award = rec["award"]
        runner = award.get("runner_up")
        if award.get("close") is True and type(runner) is int and runner > 0 and runner != award["guid"]:
            found.append(Origin("close_loot", (ref,), runner, award["guid"], when))
        if rec.get("human_claimed") is True and award["guid"] == human:
            bidders = rec.get("bidders", [])
            for bid in bidders if isinstance(bidders, list) else []:
                if not isinstance(bid, dict):
                    continue
                if bid.get("wanted") is True and type(bid.get("guid")) is int and bid["guid"] != human:
                    found.append(Origin("his_claim", ("loot:his_claim:" + row[0],), bid["guid"], human, when))
    # Keep precisely the existing public, paired-output, distinct-laugher rule.
    from leeroy_mind.nightly.language_births import find_jokes, _laughs
    for ident, _ in find_jokes(store, window, limit=1000):
        row = conn.execute("SELECT * FROM chat_line WHERE id=?", (ident,)).fetchone()
        _, replies = _laughs(store, ident)
        found.append(Origin("laughed_line", (f"chat:{ident}", *replies), 0, row["from_guid"], row["ts"]))
    return sorted(found, key=lambda o: (o.when, o.kind, o.refs, o.butt))


def origin_text(store, origin: Origin, names: Mapping[int, str], *, library=None, ident=None) -> str | None:
    try:
        return _origin_text(store, origin, names, library=library, ident=ident)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None


def _safe_name(value, *, ident=None):
    from leeroy_mind.personas.screens import names_real_name
    from leeroy_mind.text import has_meta, strip_wow_markup
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        return None
    # Screen the full raw label: hidden link/texture payloads must not hide a name.
    raw = re.sub(r"\|([TH])", r"|\1 ", value)
    if has_meta(raw) or ident is not None and names_real_name(raw, ident):
        return None
    clean = " ".join(strip_wow_markup(value).split())
    if not clean or len(clean) > 100 or has_meta(clean) or ident is not None and names_real_name(clean, ident):
        return None
    return clean


def _item_name(conn, record, *, library=None, ident=None):
    name = None
    try:
        if library is not None and hasattr(library, "item_info"):
            row = library.item_info((record["item_id"],)).get(record["item_id"])
            name = row["name"] if row is not None else None
        elif library is not None:
            path = Path(library)
            if path.is_file():
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                    row = source.execute("SELECT name FROM item WHERE id=?", (record["item_id"],)).fetchone()
                    name = row[0] if row else None
        else:
            row = conn.execute("SELECT name FROM library.item WHERE id=?", (record["item_id"],)).fetchone()
            name = row[0] if row else None
    except sqlite3.OperationalError as exc:
        if not str(exc).startswith(("no such table", "no such column")):
            raise
    return _safe_name(name, ident=ident) or _safe_name(record.get("name"), ident=ident) or "an item"


def _origin_text(store, origin: Origin, names: Mapping[int, str], *, library=None, ident=None) -> str | None:
    conn = store.conn
    if origin.kind not in EVENT_KINDS or not origin.refs:
        return None
    if origin.kind == "laughed_line":
        from leeroy_mind.nightly.lexicon import public_line, public_sources
        if public_sources(store, origin.refs) is None:
            return None
        row = public_line(store, int(origin.refs[0].split(":")[1]))
        return None if row is None else f"{names.get(row['from_guid'], 'A guild member')} said: " + json.dumps(row["text"])
    if origin.kind in ("close_loot", "his_claim"):
        if not origin.refs[0].startswith("loot:" + origin.kind + ":"):
            return None
        receipt = loot_record(conn, origin.refs[0])
        if receipt is None:
            return None
        rec, _ = receipt
        award = rec["award"]
        item = _item_name(conn, rec, library=library, ident=ident)
        if len(origin.refs) != 1 or award["guid"] != origin.originator:
            return None
        if origin.kind == "close_loot":
            if award.get("close") is not True or award.get("runner_up") != origin.butt:
                return None
            return f"{names.get(origin.butt, 'A guild member')} narrowly lost {item} to {names.get(award['guid'], 'a guild member')}."
        human = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
        if not human or int(human[0]) != origin.originator:
            return None
        if rec.get("human_claimed") is not True or award["guid"] != origin.originator or not any(
                b.get("guid") == origin.butt and b.get("wanted") is True for b in rec.get("bidders", [])):
            return None
        return f"{names.get(origin.originator, 'The owner')} claimed {item}, which {names.get(origin.butt, 'a guild member')} wanted."
    if len(origin.refs) > WIPE_RECHECK_ROWS:
        return None
    rows = []
    for ref in origin.refs:
        if not re.fullmatch(r"event:[0-9]+", ref):
            return None
        row = conn.execute("SELECT * FROM game_event WHERE id=?", (int(ref[6:]),)).fetchone()
        if row is None:
            return None
        rows.append(row)
    # Re-run the factual rule so changed state, members or death order fails closed.
    candidates = _wipe_origins(conn, (min(r["ts"] for r in rows), max(r["ts"] for r in rows) + .001),
                               limit=WIPE_RECHECK_ROWS)
    if origin not in candidates:
        return None
    from leeroy_mind.wow import boss_name
    payload = _object(rows[-1]["payload"])
    boss = boss_name(payload.get("map"), payload.get("boss_id"), "")
    boss = _safe_name(boss, ident=ident) if boss != "the boss" else None
    return f"{names.get(origin.butt, 'A guild member')} died first in {len(rows)//3} wipes on {boss or 'a boss'}."


def term_origin(row):
    try:
        refs = tuple(json.loads(row["origin_refs"]))
        if not all(isinstance(ref, str) for ref in refs):
            raise ValueError("Invalid origin refs")
    except (ValueError, TypeError):
        return Origin("invalid", (), row["butt"], row["originator"], row["born"])
    kind = ("laughed_line" if refs and refs[0].startswith("chat:") else
            "close_loot" if refs and refs[0].startswith("loot:close_loot:") else
            "his_claim" if refs and refs[0].startswith("loot:his_claim:") else
            "wipe_first_death")
    return Origin(kind, refs, row["butt"], row["originator"], row["born"])


async def coin(ctx, origin: Origin, text: str) -> str | None:
    from leeroy_mind.nightly.llm import schema
    from leeroy_mind.nightly.evidence import Evidence, Item, load_facts
    from leeroy_mind.nightly.language_screens import screen
    from leeroy_mind.nightly.lexicon import era_ok
    from leeroy_mind.personas.screens import redact_real_name
    reply = await ctx.reasoner.json("Coin a short guild joke label from the recorded fact. Return only a term.",
        redact_real_name(text, ctx.ident, ctx.ident.called), schema("culture_term", {"term": {"type": "string", "maxLength": 60}}))
    raw = reply.get("term")
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 60:
        return None
    ev = Evidence()
    for ref in origin.refs:
        ev.add(Item(ref, text, frozenset((origin.butt, origin.originator)), origin.when))
    claim, verdict = screen(ctx, "joke", origin.originator, raw, origin.refs, ev, load_facts(ctx))
    return claim.text if verdict.ok and era_ok(claim.text) else None


async def events(ctx):
    from leeroy_mind.culture.config import enabled
    from leeroy_mind.nightly import lexicon
    if not enabled(ctx) or ctx.done("language", "events"):
        return
    names, origins, remaining = {}, [], 0
    scanned = False
    async def scan(unit):
        nonlocal names, origins, remaining, scanned
        names = {g: ctx.name(g) for g in (*ctx.members(include_away=True), ctx.human)}
        count = ctx.store.conn.execute("SELECT count(*) FROM lexicon WHERE kind='joke' AND status='active' AND retired IS NULL").fetchone()[0]
        completed = ctx.store.conn.execute("SELECT count(*) FROM night_step WHERE night=? AND stage='language' "
            "AND unit LIKE 'event:%'", (ctx.night,)).fetchone()[0]
        remaining = min(max(0, ctx.settings.jokes_per_night-completed), max(0, JOKES_TARGET-count))
        if remaining:
            origins = event_origins(ctx.store, ctx.window)
        scanned = True
    # Discovery can fail too. No checkpoint on this read: retry it with failed origins.
    await ctx.map_units("language", ["events:scan"], scan)
    if not scanned:
        return
    failures = ctx.failures["language"]
    for origin in origins:
        if remaining <= 0:
            break
        unit = "event:" + origin.kind + ":" + str(origin.butt) + ":" + origin.refs[0]
        async def one(unit):
            nonlocal remaining
            if origin.originator not in names or origin.butt not in {*names, 0}:
                return
            if ctx.store.conn.execute("SELECT 1 FROM lexicon WHERE origin_ref=? AND butt=?", (origin.refs[0], origin.butt)).fetchone():
                return
            library = ctx.settings.data_dir / 'library.db'
            text = origin_text(ctx.store, origin, names, library=library, ident=ctx.ident)
            if text is None:
                return
            term = await coin(ctx, origin, text)
            def write(store):
                if origin_text(store, origin, names, library=library, ident=ctx.ident) != text:
                    raise ValueError("Culture origin changed")
                if term is not None:
                    lexicon.create_term(store, ctx, kind="joke", term=term, origin_ref=origin.refs[0],
                        originator=origin.originator, now=origin.when, refs=origin.refs,
                        butt=origin.butt, origin_kind="event", origin_refs=origin.refs)
            if ctx.commit("language", unit, write):
                remaining -= 1
        # Sequential guarded units keep the nightly cap exact, including on resume.
        await ctx.map_units("language", [unit], one)
    if ctx.failures["language"] == failures:
        ctx.skip("language", "events")


def fold_banter_uses(store):
    """Carry historical delivered-thread counts before nightly lifetime decisions."""
    from leeroy_mind.banter.topics import joke_uses
    from leeroy_mind.nightly.lexicon import frozen_origin
    for row in store.conn.execute("SELECT * FROM lexicon WHERE kind='joke'").fetchall():
        if frozen_origin(store, row):
            continue  # Historical restricted terms stay frozen, including wear.
        joke_uses(store, f"joke:{row['id']}", durable=True)
