"""Public vocabulary and evidence-backed slang, with immutable ingestion receipts."""
import hashlib
import json
import re

from leeroy_mind.nightly import lexicon
from leeroy_mind.text import strip_wow_markup

SLANG_USERS = 3
SLANG_NEW_DAYS = 14
DAY_S = 86400
_WORDS = re.compile(r"[a-z]{3,}")
_BOOTSTRAP = "nightly:vocab-bootstrap"


def _eligible(ctx):
    return set(ctx.members(include_away=True)) | {ctx.human}


def _excluded(ctx):
    # Vocabulary is lowercase, so names are deliberately screened in every case.
    names = [ctx.name(g) for g in ctx.members(include_away=True)]
    names += [ctx.ident.called, ctx.ident.character_name, ctx.ident.real_name]
    return {word for name in names for word in _WORDS.findall(name.casefold())}


def _words(text, excluded):
    return set(_WORDS.findall(strip_wow_markup(text).casefold())) - excluded


def _source_key(word):
    return f"nightly:vocab-sources:{word}"


def _receipt_key(ident):
    return f"nightly:vocab-line:{ident}"


def _line(ctx, row, eligible):
    """Current public line and its direct recalled refs, or None for unsafe input.

    Resolve the nearest paired output before checking dropped: filtering it away first
    could pair a dropped line with a different, successful use of the same words.
    """
    if row["channel"] == "whisper" or row["from_guid"] not in eligible:
        return None
    if row["from_guid"] == ctx.human:
        if row["origin"] != "human":
            return None
    elif row["origin"] not in {"mind", "game"}:
        return None
    refs = [f"chat:{row['id']}"]
    paired = None
    if row["origin"] == "mind":
        paired = ctx.store.conn.execute(
            "SELECT id,ts,guid,channel,to_guid,text,memory_ids,dropped FROM utterance "
            "WHERE guid=? AND channel=? AND to_guid=? AND text=? AND ts BETWEEN ? AND ? "
            "ORDER BY abs(ts-?),id LIMIT 1", (row["from_guid"], row["channel"], row["to_guid"],
                row["text"], row["ts"] - 2, row["ts"] + 2, row["ts"])).fetchone()
        if paired is None or paired["dropped"] is not None:
            return None
        try:
            ids = json.loads(paired["memory_ids"])
        except (ValueError, TypeError):
            return None
        if not isinstance(ids, list) or any(type(i) is not int or i <= 0 for i in ids):
            return None
        refs.extend(f"memory:{i}" for i in dict.fromkeys(ids))
    refs = tuple(refs)
    # Memory access/status changes need not rewrite a spoken line's identity. Its full
    # ancestry is checked anew when slang is published; its text and pairing stay frozen.
    identity = ([row[k] for k in ("id", "ts", "channel", "from_guid", "from_name", "to_guid",
                                  "text", "origin", "thread_id")], tuple(paired) if paired else None)
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()
    return refs, digest


def _public_source(ctx, row, eligible):
    """Close every nested chat over its own sent-output inputs before admitting a word."""
    first = _line(ctx, row, eligible)
    if first is None:
        return None
    refs = first[0]
    lines = {f"chat:{row['id']}": first[1]}
    while True:
        snapshot = lexicon.source_snapshot(ctx.store, refs)
        if snapshot is None:
            return None
        additions = []
        for ref, _ in snapshot:
            if not ref.startswith("chat:") or ref in lines:
                continue
            nested = ctx.store.conn.execute("SELECT * FROM chat_line WHERE id=?",
                                            (int(ref.split(":", 1)[1]),)).fetchone()
            source = _line(ctx, nested, eligible) if nested else None
            if source is None:
                return None
            lines[ref] = source[1]
            additions.extend(source[0])
        expanded = tuple(dict.fromkeys((*refs, *additions)))
        if expanded == refs:
            digest = hashlib.sha256(json.dumps(sorted(lines.items())).encode("utf-8")).hexdigest()
            return refs, digest
        if len(expanded) > 5000:
            return None
        refs = expanded


def vocab(ctx):
    """Ingest history once, then this immutable night window. Joins the unit transaction."""
    store = ctx.store
    key = f"nightly:vocab-night:{ctx.night}"
    bounds = list(ctx.window)
    with store.transaction():
        done = store.get_meta(key)
        if done is not None:
            if json.loads(done) != bounds:
                raise ValueError("Vocabulary night window changed after ingestion")
            return
        bootstrapping = store.get_meta(_BOOTSTRAP) is None
        sql = "SELECT * FROM chat_line WHERE ts<?"
        params = [ctx.window[1]]
        if not bootstrapping:
            sql += " AND ts>=?"
            params.append(ctx.window[0])
        lines = store.conn.execute(sql + " ORDER BY ts,id", params).fetchall()
        eligible, excluded = _eligible(ctx), _excluded(ctx)
        for row in lines:
            receipt = _receipt_key(row["id"])
            if store.get_meta(receipt) is not None:
                continue
            source = _public_source(ctx, row, eligible)
            store.set_meta(receipt, source[1] if source else "rejected")
            if source is None:
                continue
            for word in sorted(_words(row["text"], excluded)):
                store.conn.execute("INSERT OR IGNORE INTO vocab(word,first_seen,origin,users) "
                    "VALUES (?,?,?,?)", (word, row["ts"], row["from_guid"], "[]"))
                current = store.conn.execute("SELECT users FROM vocab WHERE word=?", (word,)).fetchone()
                users = set(json.loads(current["users"]))
                users.add(row["from_guid"])
                store.conn.execute("UPDATE vocab SET users=? WHERE word=?", (json.dumps(sorted(users)), word))
                sources = json.loads(store.get_meta(_source_key(word)) or "{}")
                sources.setdefault(str(row["from_guid"]), row["id"])
                store.set_meta(_source_key(word), json.dumps(sources, sort_keys=True))
        if bootstrapping:
            store.set_meta(_BOOTSTRAP, json.dumps({"night": ctx.night, "end": ctx.window[1]}))
        store.set_meta(key, json.dumps(bounds))


def _sources(ctx, word, eligible, excluded):
    """Revalidate the first ingested public use by each user, never an invented replacement."""
    recorded = json.loads(ctx.store.get_meta(_source_key(word)) or "{}")
    if len(recorded) < SLANG_USERS:
        return {}
    sources = {}
    for guid, ident in recorded.items():
        if not guid.isdecimal() or int(guid) not in eligible or type(ident) is not int:
            continue
        row = ctx.store.conn.execute("SELECT * FROM chat_line WHERE id=?", (ident,)).fetchone()
        if (row is None or row["from_guid"] != int(guid) or row["ts"] >= ctx.window[1]
                or word not in _words(row["text"], excluded)):
            continue
        source = _public_source(ctx, row, eligible)
        if source is not None and source[1] == ctx.store.get_meta(_receipt_key(ident)):
            sources[int(guid)] = (row, source[0])
    return sources


def slang(ctx):
    """Promote at most the night's cap of recent, publicly attested vocabulary words."""
    store = ctx.store
    receipt = f"nightly:slang-created:{ctx.night}"
    with store.transaction():
        created = json.loads(store.get_meta(receipt) or "[]")
        room = max(0, ctx.settings.slang_per_night - len(created))
        if not room:
            return
        eligible, excluded = _eligible(ctx), _excluded(ctx)
        end = ctx.window[1]
        rows = store.conn.execute("SELECT * FROM vocab WHERE slang=0 AND first_seen>=? "
            "AND first_seen<? ORDER BY first_seen,word", (end - SLANG_NEW_DAYS * DAY_S, end)).fetchall()
        for row in rows:
            if not room:
                break
            word = row["word"]
            if _WORDS.fullmatch(word) is None or word in excluded or not lexicon.era_ok(word):
                continue
            sources = _sources(ctx, word, eligible, excluded)
            origin = row["origin"]
            if (len(sources) < SLANG_USERS or origin not in sources
                    or sources[origin][0]["ts"] != row["first_seen"]):
                continue
            # A prior or vetoed term is not a new birth, nor permission to revive it.
            if store.conn.execute("SELECT 1 FROM lexicon WHERE kind='slang' AND term=? AND target=0",
                                  (word,)).fetchone() is not None:
                store.conn.execute("UPDATE vocab SET slang=1 WHERE word=?", (word,))
                continue
            origin_row, origin_refs = sources[origin]
            all_refs = tuple(dict.fromkeys(ref for _, refs in sources.values() for ref in refs))
            now = ctx.clock.now()
            ident = lexicon.create_term(store, ctx, kind="slang", term=word,
                origin_ref=f"chat:{origin_row['id']}", originator=origin, now=now, refs=all_refs)
            if ident is None:
                continue
            for guid in sorted(set(sources) - {origin}):
                refs = tuple(dict.fromkeys((*origin_refs, *sources[guid][1])))
                if not lexicon.adopt(store, ctx, ident, guid, via=origin, now=now, refs=refs):
                    raise ValueError("Slang adopter could not retain its public source evidence")
            store.conn.execute("UPDATE vocab SET slang=1 WHERE word=?", (word,))
            created.append(ident)
            store.set_meta(receipt, json.dumps(created))
            room -= 1
