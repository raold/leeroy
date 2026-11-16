"""`personas clean-contamination`: retire what chat stored about members that is not theirs
(identity audit, 2026-11-15). Three things leaked into their stored state:

- echoes: a member saying his line back as hers ("single, no kids" was his answer), stored as
  her claim, a disclosure to him and memories;
- persona contradictions: an invented family, partner or pet ("has two daughters" for a married
  woman without kids), stored the same way;
- gender slips: a woman speaking with his body or role ("I told him I'm not some proper
  gentleman"), stored as her memory; and coach lessons that call a man "She".

A line is contaminated when it echoes his last line in its conversation, slips, contradicts its
speaker's persona, or is the source of a claim retired for contradicting the persona. The plan
retires (never deletes):

- live claims cited to a contaminated line or contradicting the persona: superseded by
  themselves (`superseded_by = id`), their search vector removed;
- disclosures whose `ref` is a contaminated line: `ref` becomes `retired:<ref>` and `response`
  `none`, so no ladder, depth or "remembered" check reads them;
- active memories (never the owner's own, source `owner`) whose text slips or contradicts the
  persona, or all of whose owner's own evidence lines are contaminated: status `archived`;
- live coach lessons opening with the other sex's pronoun: expired.

The run writes one receipt (meta `personas:clean-contamination:<stamp>`) holding every row's
prior state, which `--undo <stamp>` puts back. Trust a retired disclosure once added stays: it
was applied to a relationship row and is not undone here."""
import json
from collections import Counter
from dataclasses import dataclass, field

from leeroy_mind.guards.identity import slips
from leeroy_mind.learn.lessons import misgendered

from .facts import Facts, conflicts, facts_of_json

RECEIPT = "personas:clean-contamination:"
ECHO_SECONDS = 600  # his line a member's line may be saying back (claims.extract's)
MEMORY_SOURCES = ("chat", "extract", "reflection", "offscreen", "hearsay")


@dataclass(frozen=True)
class Item:
    kind: str  # claim, disclosure, memory or lesson
    id: int
    guid: int
    why: str   # echo, slip, persona:<code>, evidence or misgendered


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)
    lines: dict[int, str] = field(default_factory=dict)  # contaminated chat line id -> why

    def counts(self) -> Counter:
        return Counter((i.kind, i.why.split(":")[0]) for i in self.items)


def _table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _personas(conn) -> dict[int, dict]:
    from .tiers import cast_sql
    out = {}
    for guid, raw in conn.execute("SELECT guid, json FROM persona_core WHERE " + cast_sql(conn)):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            out[guid] = data
    return out


def _gender(data: dict) -> str:
    identity = data.get("identity")
    return identity.get("player_gender", "") if isinstance(identity, dict) else ""


def contaminated_lines(conn, human: int, personas: dict[int, dict],
                       facts: dict[int, Facts]) -> dict[int, str]:
    """Each cast member's line (origin mind) that echoes his last line in its conversation,
    slips or contradicts her persona, with why (the first that holds)."""
    from leeroy_mind.guards.review import echoes  # guards.review reads the personas package
    last: dict = {}
    out = {}
    for lid, ts, channel, frm, to, text, origin in conn.execute(
            "SELECT id, ts, channel, from_guid, to_guid, text, origin FROM chat_line "
            "WHERE origin IN ('human', 'mind') ORDER BY id"):
        if origin == "human":
            if frm == human:
                last[(channel, to) if channel == "whisper" else channel] = (ts, text)
            continue
        data = personas.get(frm)
        if data is None:
            continue
        his = last.get((channel, frm) if channel == "whisper" else channel)
        if his is not None and 0 <= ts - his[0] <= ECHO_SECONDS and echoes(text, his[1]):
            out[lid] = "echo"
        elif slips(text, _gender(data)):
            out[lid] = "slip"
        elif codes := conflicts(text, facts[frm]):
            out[lid] = "persona:" + codes[0]
    return out


def plan(conn, *, human: int, now: float) -> Plan:
    """What the run would retire; reads only (works on a read-only connection)."""
    personas = _personas(conn)
    facts = {g: facts_of_json(d) for g, d in personas.items()}
    result = Plan(lines=contaminated_lines(conn, human, personas, facts))
    if _table(conn, "member_claim"):
        claims = [r for r in conn.execute("SELECT id, guid, text, source_line FROM member_claim "
                                          "WHERE superseded_by IS NULL ORDER BY id")
                  if r[1] in facts]
        wrong = {}
        for cid, guid, text, source_line in claims:  # first: the line a wrong claim came from
            if codes := conflicts(text, facts[guid], claim=True):
                wrong[cid] = "persona:" + codes[0]
                if source_line is not None:
                    result.lines.setdefault(source_line, "persona:claim")
        for cid, guid, text, source_line in claims:
            if cid in wrong:
                result.items.append(Item("claim", cid, guid, wrong[cid]))
            elif source_line in result.lines:
                result.items.append(Item("claim", cid, guid,
                                         result.lines[source_line].split(":")[0]))
    if _table(conn, "disclosure"):
        for did, guid, ref in conn.execute("SELECT id, guid, ref FROM disclosure "
                                           "WHERE ref GLOB 'chat:[0-9]*' ORDER BY id"):
            line = int(ref[5:]) if ref[5:].isdigit() else None
            if line in result.lines:
                result.items.append(Item("disclosure", did, guid,
                                         result.lines[line].split(":")[0]))
    marks = ",".join("?" * len(MEMORY_SOURCES))
    for mid, owner, text, evidence in conn.execute(
            f"SELECT id, owner, text, evidence FROM memory WHERE status = 'active' "
            f"AND source IN ({marks}) ORDER BY id", MEMORY_SOURCES).fetchall():
        if owner not in personas:
            continue
        codes = conflicts(text, facts[owner])
        why = ("slip" if slips(text, _gender(personas[owner])) else
               "persona:" + codes[0] if codes else "")
        if not why and result.lines:
            mine = _own_lines(conn, owner, _chat_refs(evidence))
            if mine and all(x in result.lines for x in mine):
                why = "evidence"  # every line of hers it rests on is contaminated
        if why:
            result.items.append(Item("memory", mid, owner, why))
    if _table(conn, "talk_lesson"):
        for lid, guid, text in conn.execute("SELECT id, guid, text FROM talk_lesson "
                                            "WHERE expires > ? ORDER BY id", (now,)):
            if guid in personas and misgendered(text, _gender(personas[guid])):
                result.items.append(Item("lesson", lid, guid, "misgendered"))
    return result


def _chat_refs(evidence) -> list[int]:
    try:
        refs = json.loads(evidence)
    except (TypeError, ValueError):
        return []
    return [int(r[5:]) for r in refs if isinstance(r, str) and r.startswith("chat:")
            and r[5:].isdigit()] if isinstance(refs, list) else []


def _own_lines(conn, owner: int, lines: list[int]) -> list[int]:
    """Which of `lines` the memory's owner said herself."""
    if not lines:
        return []
    return [r[0] for r in conn.execute(
        "SELECT id FROM chat_line WHERE from_guid = ? AND id IN (SELECT value FROM json_each(?))",
        (owner, json.dumps(lines)))]


def apply(conn, p: Plan, *, now: float, stamp: str) -> dict:
    """Retire the plan's rows in the caller's transaction and store their prior state as the
    receipt `RECEIPT + stamp`; returns the receipt. Needs sqlite-vec loaded (claim vectors)."""
    receipt = {"at": now, "claims": [], "disclosures": [], "memories": [], "lessons": []}
    for item in p.items:
        if item.kind == "claim":
            had = conn.execute("SELECT 1 FROM member_claim_vec WHERE claim_id = ?",
                               (item.id,)).fetchone() is not None
            if conn.execute("UPDATE member_claim SET superseded_by = id WHERE id = ? "
                            "AND superseded_by IS NULL", (item.id,)).rowcount:
                conn.execute("DELETE FROM member_claim_vec WHERE claim_id = ?", (item.id,))
                receipt["claims"].append([item.id, had])
        elif item.kind == "disclosure":
            row = conn.execute("SELECT ref, response FROM disclosure WHERE id = ?",
                               (item.id,)).fetchone()
            if row is not None and not row[0].startswith("retired:"):
                conn.execute("UPDATE disclosure SET ref = 'retired:' || ref, response = 'none' "
                             "WHERE id = ?", (item.id,))
                receipt["disclosures"].append([item.id, row[0], row[1]])
        elif item.kind == "memory":
            if conn.execute("UPDATE memory SET status = 'archived' WHERE id = ? "
                            "AND status = 'active'", (item.id,)).rowcount:
                receipt["memories"].append([item.id, "active"])
        elif item.kind == "lesson":
            row = conn.execute("SELECT expires FROM talk_lesson WHERE id = ?", (item.id,)).fetchone()
            if row is not None and row[0] > now:
                conn.execute("UPDATE talk_lesson SET expires = ? WHERE id = ?", (now, item.id))
                receipt["lessons"].append([item.id, row[0]])
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (RECEIPT + stamp, json.dumps(receipt)))
    return receipt


def undo(conn, stamp: str, *, now: float) -> Counter:
    """Put back what the receipt `RECEIPT + stamp` retired, row by row, where the row is still
    as the run left it; marks the receipt undone (never deletes it). Raises KeyError for an
    unknown receipt and ValueError for one already undone."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (RECEIPT + stamp,)).fetchone()
    if row is None:
        raise KeyError(stamp)
    receipt = json.loads(row[0])
    if receipt.get("undone"):
        raise ValueError("already undone")
    done = Counter()
    for cid, had in receipt["claims"]:
        if conn.execute("UPDATE member_claim SET superseded_by = NULL WHERE id = ? "
                        "AND superseded_by = id", (cid,)).rowcount:
            done["claims"] += 1
            if had:
                conn.execute("INSERT INTO member_claim_vec(claim_id, guid, embedding) "
                             "SELECT id, guid, embedding FROM member_claim WHERE id = ? "
                             "AND (expires IS NULL OR expires > ?)", (cid, now))
    for did, ref, response in receipt["disclosures"]:
        done["disclosures"] += conn.execute(
            "UPDATE disclosure SET ref = ?, response = ? WHERE id = ? AND ref = ?",
            (ref, response, did, "retired:" + ref)).rowcount
    for mid, status in receipt["memories"]:
        done["memories"] += conn.execute("UPDATE memory SET status = ? WHERE id = ? "
                                         "AND status = 'archived'", (status, mid)).rowcount
    for lid, expires in receipt["lessons"]:
        done["lessons"] += conn.execute("UPDATE talk_lesson SET expires = ? WHERE id = ?",
                                        (expires, lid)).rowcount
    receipt["undone"] = now
    conn.execute("UPDATE meta SET value = ? WHERE key = ?", (json.dumps(receipt), RECEIPT + stamp))
    return done


__all__ = ["ECHO_SECONDS", "Item", "Plan", "RECEIPT", "apply", "contaminated_lines", "plan",
           "undo"]
