"""A stable interview panel, resumable owner-only answers, and measured weekly change."""
import asyncio
import hashlib
import json
import random

from leeroy_mind.eval.persona import _card, _vectors
from leeroy_mind.nightly.critic import _members, _provenance, _safe
from leeroy_mind.nightly.llm import ReflectionError
from leeroy_mind.storage.rows import cosine

QUESTIONS = (
    ("trait", "What do you do for a living?"),
    ("trait", "How do you like to spend an evening in game?"),
    ("trait", "What do you want most from the game right now?"),
    ("trait", "What would make you take a break from WoW?"),
    ("trait", "What's going on in your life outside the game?"),
    ("people", "Who do you trust most in the guild?"),
    ("people", "What's your best guild memory?"),
    ("people", "Who annoys you?"),
    ("people", "Who would you want next to you on a hard boss?"),
    ("people", "What's something you've changed your mind about lately?"),
)
ANSWER_MAX = 4000


def interview_panel(ctx):
    members = sorted(set(ctx.members()) & _members(ctx.store))
    size = min(max(0, ctx.settings.interview_members), len(members))
    rng = random.Random("interview-panel")
    with ctx.store.transaction():
        raw = ctx.store.get_meta("interview_panel")
        old = json.loads(raw) if raw is not None else []
        if not isinstance(old, list) or any(type(g) is not int for g in old):
            raise ValueError("Invalid saved interview panel")
        kept = list(dict.fromkeys(g for g in old if g in members))[:size]
        remaining = [g for g in members if g not in kept]
        kept.extend(rng.sample(remaining, size - len(kept)))
        ctx.store.set_meta("interview_panel", json.dumps(kept))
        return kept


def _row(store, week, guid, qkey):
    return store.conn.execute("SELECT * FROM eval_answer WHERE week=? AND kind='interview' "
                              "AND guid=? AND qkey=?", (week, guid, qkey)).fetchone()


def _memory_ids(raw):
    values = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(values, list) or any(type(i) is not int or i <= 0 for i in values):
        raise ValueError("Invalid interview recalled-memory IDs")
    return list(dict.fromkeys(values))


def _complete(row):
    return row is not None and isinstance(row["answer"], str) and bool(row["answer"].strip())


def _matches(row, guid, n):
    kind, question = QUESTIONS[n]
    return (row is not None and row["kind"] == "interview" and row["guid"] == guid
            and row["qkey"] == f"q{n}" and row["category"] == kind
            and row["question"] == question and row["truth"] == "")


def _accepted(row, guid, n, ident):
    if not _matches(row, guid, n) or not _complete(row):
        return False
    try:
        _memory_ids(row["memory_ids"])
        return len(row["answer"]) <= ANSWER_MAX and _safe(row["answer"], ident) == row["answer"]
    except (ValueError, TypeError):
        return False


async def interview(store, asker, week, members, *, ident=None):
    ident = ident or getattr(asker, "ident", None)
    if ident is None:
        raise ValueError("Interview requires the human identity before asking or writing")
    key = f"eval:interview-plan:{week}"
    with store.transaction():
        raw = store.get_meta(key)
        panel = json.loads(raw) if raw is not None else list(dict.fromkeys(
            g for g in members if type(g) is int and g in _members(store) and g != ident.guid))
        if (not isinstance(panel, list) or any(type(g) is not int or g <= 0 or g == ident.guid for g in panel)
                or len(set(panel)) != len(panel)):
            raise ValueError("Invalid frozen interview panel")
        if raw is None:
            store.set_meta(key, json.dumps(panel))
        for guid in panel:
            for n, (kind, question) in enumerate(QUESTIONS):
                store.conn.execute("INSERT OR IGNORE INTO eval_answer(week,kind,guid,qkey,category,question,truth,memory_ids) "
                    "VALUES (?,'interview',?,?,?,?,?,'[]')",
                    (week, guid, f"q{n}", kind, _safe(question, ident), ""))
    asked = failed = skipped = 0
    for guid in panel:
        for n, (_, question) in enumerate(QUESTIONS):
            qkey = f"q{n}"
            before = _row(store, week, guid, qkey)
            if _accepted(before, guid, n, ident):
                skipped += 1
                continue
            try:
                if not _matches(before, guid, n) or _complete(before):
                    raise ValueError("Interview row differs from the frozen question or accepted answer contract")
                card = _card(store, guid)
                if card is None:
                    raise ValueError("Interview member is no longer eligible")
                copied = getattr(asker, "store", store)
                if _card(copied, guid) != card:
                    raise ValueError("Interview scratch persona is stale")
                result = await asker.ask(guid, question)
                text = _safe(result.text, ident)
                ids = _memory_ids(result.memory_ids)
                if not text or len(text) > ANSWER_MAX:
                    raise ValueError("Invalid interview answer length")
                refs = [f"memory:{i}" for i in ids]
                source = _provenance(copied, refs)
                with store.transaction():
                    fresh = _row(store, week, guid, qkey)
                    if _accepted(fresh, guid, n, ident):
                        skipped += 1
                        continue
                    if (fresh is None or tuple(fresh) != tuple(before) or _card(store, guid) != card
                            or _provenance(store, refs) != source):
                        raise ValueError("Interview sources changed during the answer")
                    memories = store.get_memories(ids)
                    if len(memories) != len(ids) or any(m.owner not in (0, guid) for m in memories):
                        raise ValueError("Interview recalled another member's memory")
                    store.conn.execute("UPDATE eval_answer SET answer=?,memory_ids=? WHERE id=?",
                                       (text, json.dumps(ids), fresh["id"]))
                asked += 1
            except asyncio.CancelledError:
                raise
            except ReflectionError as error:
                if error.fatal:
                    raise
                failed += 1
            except Exception:
                failed += 1  # no private text/error payload enters the receipt
    answered = sum(_accepted(_row(store, week, guid, f"q{n}"), guid, n, ident)
                   for guid in panel for n in range(len(QUESTIONS)))
    return {"expected": len(panel) * len(QUESTIONS), "answered": answered,
            "pending": len(panel) * len(QUESTIONS) - answered,
            "asked": asked, "failed": failed, "skipped": skipped}


def _answers(store, week):
    out = {}
    panel = json.loads(store.get_meta(f"eval:interview-plan:{week}") or "null")
    for row in store.conn.execute("SELECT * FROM eval_answer WHERE week=? AND kind='interview' ORDER BY guid,qkey", (week,)):
        if not _complete(row) or row["qkey"] not in {f"q{i}" for i in range(len(QUESTIONS))}:
            continue
        kind, question = QUESTIONS[int(row["qkey"][1:])]
        if (not _matches(row, row["guid"], int(row["qkey"][1:]))
                or row["guid"] not in (panel if panel is not None else _members(store))):
            continue
        try:
            _memory_ids(row["memory_ids"])
        except (ValueError, TypeError):
            continue
        out[row["guid"], row["qkey"]] = dict(row)
    return out


async def stability(ctx, week, prev_week):
    current, previous = _answers(ctx.store, week), _answers(ctx.store, prev_week)
    signature = hashlib.sha256(json.dumps([list(current.values()), list(previous.values())], sort_keys=True).encode()).hexdigest()
    key = f"eval:stability:{week}:{prev_week}"
    cached = ctx.store.get_meta(key)
    if cached is not None:
        saved = json.loads(cached)
        if saved["signature"] == signature:
            return saved["metrics"]
    paired = sorted(current.keys() & previous.keys())
    values = {}
    if paired:
        texts = [_safe(row["answer"], ctx.ident) for pair in paired for row in (current[pair], previous[pair])]
        vectors = _vectors(await ctx.embed(texts), len(texts))
        values = {pair: max(-1.0, min(1.0, cosine(vectors[i * 2], vectors[i * 2 + 1])))
                  for i, pair in enumerate(paired)}
    traits = [v for pair, v in values.items() if current[pair]["category"] == "trait"]
    people = [v for pair, v in values.items() if current[pair]["category"] == "people"]
    people_answers = [r for r in current.values() if r["category"] == "people"]
    panel = json.loads(ctx.store.get_meta(f"eval:interview-plan:{week}") or "null")
    expected = len(panel) * 5 if panel is not None else len({g for g, _ in current}) * 5
    prior_panel = json.loads(ctx.store.get_meta(f"eval:interview-plan:{prev_week}") or "null")
    shared = (set(panel if panel is not None else (g for g, _ in current))
              & set(prior_panel if prior_panel is not None else (g for g, _ in previous)))
    metrics = {"trait_similarity": sum(traits) / len(traits) if traits else None,
        "people_changed": sum(v < 0.8 for v in people) / len(people) if people else None,
        "people_grounded": sum(bool(_memory_ids(r["memory_ids"])) for r in people_answers) / len(people_answers) if people_answers else None,
        "trait_pairs": len(traits), "people_pairs": len(people), "people_answers": len(people_answers),
        "expected_trait_answers": expected, "expected_people_answers": expected,
        "expected_trait_pairs": len(shared) * 5, "expected_people_pairs": len(shared) * 5}
    with ctx.store.transaction():
        if _answers(ctx.store, week) != current or _answers(ctx.store, prev_week) != previous:
            raise ValueError("Interview answers changed during stability embedding")
        ctx.store.set_meta(key, json.dumps({"signature": signature, "metrics": metrics}))
    return metrics
