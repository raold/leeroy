"""Code-built event facts; the reflection model may supply only a short label."""
from dataclasses import dataclass
from leeroy_mind.clock import SystemClock
import json
import re
from typing import Mapping

EVENT_KINDS = ("wipe_first_death", "close_loot", "his_claim", "laughed_line")
RETIRE_USES = 12
COLD_DAYS = 60
JOKES_TARGET = 8

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


def event_origins(conn, window: tuple[float, float]) -> list[Origin]:
    found, pulls, firsts = [], {}, {}
    human = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
    try:
        human = int(human[0]) if human else 0
    except (TypeError, ValueError):
        human = 0
    for row in conn.execute("SELECT * FROM game_event WHERE ts>=? AND ts<? ORDER BY ts,id", window):
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
    from leeroy_mind.storage.store import Store
    from leeroy_mind.nightly.language_births import find_jokes, _laughs
    store = Store(conn, clock=SystemClock())
    for ident, _ in find_jokes(store, window, limit=1000):
        row = conn.execute("SELECT * FROM chat_line WHERE id=?", (ident,)).fetchone()
        _, replies = _laughs(store, ident)
        found.append(Origin("laughed_line", (f"chat:{ident}", *replies), 0, row["from_guid"], row["ts"]))
    return sorted(found, key=lambda o: (o.when, o.kind, o.refs, o.butt))


def origin_text(conn, origin: Origin, names: Mapping[int, str]) -> str | None:
    try:
        return _origin_text(conn, origin, names)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None


def _origin_text(conn, origin: Origin, names: Mapping[int, str]) -> str | None:
    if origin.kind not in EVENT_KINDS or not origin.refs:
        return None
    if origin.kind == "laughed_line":
        from leeroy_mind.nightly.lexicon import public_line, public_sources
        from leeroy_mind.storage.store import Store
        store = Store(conn, clock=SystemClock())
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
        if len(origin.refs) != 1 or award["guid"] != origin.originator:
            return None
        if origin.kind == "close_loot":
            if award.get("close") is not True or award.get("runner_up") != origin.butt:
                return None
            return f"{names.get(origin.butt, 'A guild member')} narrowly lost item {rec['item_id']} to {names.get(award['guid'], 'a guild member')}."
        human = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
        if not human or int(human[0]) != origin.originator:
            return None
        if rec.get("human_claimed") is not True or award["guid"] != origin.originator or not any(
                b.get("guid") == origin.butt and b.get("wanted") is True for b in rec.get("bidders", [])):
            return None
        return f"{names.get(origin.originator, 'The owner')} claimed item {rec['item_id']}, which {names.get(origin.butt, 'a guild member')} wanted."
    rows = []
    for ref in origin.refs:
        if not re.fullmatch(r"event:[0-9]+", ref):
            return None
        row = conn.execute("SELECT * FROM game_event WHERE id=?", (int(ref[6:]),)).fetchone()
        if row is None:
            return None
        rows.append(row)
    # Re-run the factual rule so changed state, members or death order fails closed.
    candidates = event_origins(conn, (min(r["ts"] for r in rows), max(r["ts"] for r in rows) + .001))
    if origin not in candidates:
        return None
    boss = _object(rows[-1]["payload"]).get("boss_id")
    return f"{names.get(origin.butt, 'A guild member')} died first in {len(rows)//3} wipes on boss {boss}."


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
    names = {g: ctx.name(g) for g in (*ctx.members(include_away=True), ctx.human)}
    count = ctx.store.conn.execute("SELECT count(*) FROM lexicon WHERE kind='joke' AND status='active' AND retired IS NULL").fetchone()[0]
    origins = event_origins(ctx.store.conn, ctx.window)
    completed = ctx.store.conn.execute("SELECT count(*) FROM night_step WHERE night=? AND stage='language' "
        "AND unit LIKE 'event:%'", (ctx.night,)).fetchone()[0]
    remaining = min(max(0, ctx.settings.jokes_per_night-completed), max(0, JOKES_TARGET-count))
    failed = False
    for origin in origins:
        if remaining <= 0:
            break
        if origin.originator not in names or origin.butt not in {*names, 0}:
            continue
        unit = "event:" + origin.kind + ":" + str(origin.butt) + ":" + origin.refs[0]
        if ctx.done("language", unit):
            continue
        if ctx.store.conn.execute("SELECT 1 FROM lexicon WHERE origin_ref=? AND butt=?", (origin.refs[0], origin.butt)).fetchone():
            continue
        text = origin_text(ctx.store.conn, origin, names)
        if text is None:
            continue
        term = await coin(ctx, origin, text)
        def write(store):
            if origin_text(store.conn, origin, names) != text:
                raise ValueError("Culture origin changed")
            if term is not None:
                lexicon.create_term(store, ctx, kind="joke", term=term, origin_ref=origin.refs[0],
                    originator=origin.originator, now=origin.when, refs=origin.refs,
                    butt=origin.butt, origin_kind="event", origin_refs=origin.refs)
        if ctx.commit("language", unit, write):
            remaining -= 1
        else:
            failed = True
    if not failed:
        ctx.skip("language", "events")


def fold_banter_uses(store):
    """Carry historical delivered-thread counts before nightly lifetime decisions."""
    from leeroy_mind.banter.topics import joke_uses
    for row in store.conn.execute("SELECT id FROM lexicon WHERE kind='joke'").fetchall():
        joke_uses(store, f"joke:{row['id']}", durable=True)
