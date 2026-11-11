"""Conservative contextual Thompson sampling and replaceable, split reward credit."""
from dataclasses import dataclass, field
import json
import random
import time

from leeroy_mind.discord.channels import is_discord, is_private, channel_id
from leeroy_mind.logs import log
from . import arms
from .config import LearnCfg
from .reward import channel_kind, decayed


@dataclass(frozen=True)
class Context:
    channel: str
    stance: str = "none"
    talkative: int = 5
    settings: LearnCfg = field(default_factory=LearnCfg)

    @property
    def key(self):
        kind = channel_kind(self.channel)
        # Separate Discord rooms as well as mediums. Private content never enters the key.
        if is_discord(self.channel) and not is_private(self.channel):
            kind = "discord:" + channel_id(self.channel)
        if self.stance not in ("none", "fond", "drawn", "lust") or type(self.talkative) is not int or not 0 <= self.talkative <= 10:
            raise ValueError("bandit_context_invalid")
        return json.dumps([kind, self.stance, "quiet" if self.talkative < 4 else
                           "natural" if self.talkative < 7 else "chatty"], separators=(",", ":"))


def valid_key(key):
    try:
        parts = json.loads(key)
        return (type(parts) is list and len(parts) == 3 and all(type(p) is str for p in parts)
                and len(parts[0]) <= 100 and parts[1] in ("none", "fond", "drawn", "lust")
                and parts[2] in ("quiet", "natural", "chatty"))
    except (ValueError, TypeError):
        return False


def enabled(settings, guid):
    return settings.enabled and settings.bandit and not settings.freeze and guid not in settings.frozen_members


def posterior(conn, guid, key, *, now=None):
    """Beta(1,1) priors never decay; only measured evidence does, on a 21-day half-life."""
    now = time.time() if now is None else now
    out = {}
    for aid, ts, success, failure, bad in conn.execute(
            "SELECT arm_id,ts,success,failure,bad FROM bandit_credit WHERE guid=? AND context=?", (guid, key)):
        p = out.setdefault(aid, dict(alpha=1., beta=1., uses=0, bad=0, attempts=0))
        p["alpha"] += decayed(success, age_seconds=max(0, now-ts))
        p["beta"] += decayed(failure, age_seconds=max(0, now-ts))
        p["uses"] += 1
        p["attempts"] += 1
        p["bad"] += bad
    for aid, count in conn.execute("SELECT arm_id,count(*) FROM bandit_rejection WHERE guid=? AND context=? GROUP BY arm_id", (guid,key)):
        p = out.setdefault(aid, dict(alpha=1., beta=1., uses=0, bad=0, attempts=0))
        p["bad"] += count
        p["attempts"] += count
    return out


def choose(conn, guid, context, *, rng=None, now=None):
    """Caller owns the transaction. Task 39 will capture context.key with these arm ids.

    Counts reserve attempted choices, so cancellation cannot buy extra exploration.
    Off/frozen paths neither write, draw randomness nor need the new schema.
    """
    result = arms.controls()
    cfg = context.settings
    if not enabled(cfg, guid) or not arms.available(conn):
        return result
    key = context.key
    rng = rng or random.Random()
    arms.ensure(conn, now=now)
    registry = arms.read(conn)
    p = posterior(conn, guid, key, now=now)
    shares = dict(conn.execute("SELECT arm_id,turns FROM bandit_share WHERE guid=? AND context=?", (guid,key)))
    row = conn.execute("SELECT turns,exploratory FROM bandit_turn WHERE guid=?", (guid,)).fetchone()
    turns, explored = (row[0]+1, row[1]) if row else (1, 0)
    can_explore = explored+1 <= turns//5
    used_exploration = False
    for family, control in result.items():
        if family in cfg.frozen_families:
            continue
        base = p.get(control, dict(alpha=1., beta=1., uses=0, bad=0))
        candidates = [control]
        immature = set()
        for arm in registry:
            if arm.family != family or arm.status != "active" or not arms.valid(family, arm.params):
                continue
            stats = p.get(arm.id, dict(alpha=1., beta=1., uses=0, bad=0))
            family_turns = sum(shares.get(a.id,0) for a in registry if a.family == family)
            if stats["uses"] < 30 and 2*(shares.get(arm.id,0)+1) > family_turns+1:
                continue
            qualified = (stats["uses"] >= 30 and stats["alpha"]/(stats["alpha"]+stats["beta"])
                         > base["alpha"]/(base["alpha"]+base["beta"]))
            if qualified or can_explore:
                candidates.append(arm.id)
                if not qualified:
                    immature.add(arm.id)
        def sample(aid):
            stats = p.get(aid, dict(alpha=1., beta=1.))
            return rng.betavariate(stats["alpha"], stats["beta"])
        selected = max(candidates, key=sample)
        used_exploration |= selected in immature
        result[family] = selected
    conn.execute("INSERT INTO bandit_turn VALUES (?,?,?) ON CONFLICT(guid) DO UPDATE SET "
                 "turns=excluded.turns,exploratory=excluded.exploratory", (guid, turns, explored+used_exploration))
    for aid in result.values():
        conn.execute("INSERT INTO bandit_share VALUES (?,?,?,1) ON CONFLICT(guid,context,arm_id) "
                     "DO UPDATE SET turns=turns+1", (guid, key, aid))
    return result


def update(conn, line_id, *, settings=LearnCfg(), now=None):
    """One reward observation, divided equally among recipe families; revisions replace it."""
    if not settings.enabled or not settings.bandit or not arms.available(conn):
        return False
    row = conn.execute("SELECT r.guid,r.value,r.cap_weight,r.revision,c.ts,p.arm_ids,p.bandit_context,"
                       "p.guard_verdict,c.channel,c.from_guid FROM talk_reward r JOIN chat_line c ON c.id=r.line_id "
                       "JOIN talk_recipe p ON p.line_id=r.line_id WHERE r.line_id=?", (line_id,)).fetchone()
    if row is None or not enabled(settings, row[0]) or row[0] != row[9] or not valid_key(row[6]):
        return False
    guid, value, cap, revision, ts, encoded, key, verdict, channel, _ = row
    expected = Context(channel).key
    if json.loads(key)[0] != json.loads(expected)[0]:
        return False  # never relabel a private/Discord observation as game/public credit
    try:
        ids = json.loads(encoded)
        if type(ids) is not list or not 1 <= len(ids) <= len(arms.FAMILIES) or any(type(i) is not str for i in ids) or len(set(ids)) != len(ids):
            return False
    except (ValueError, TypeError):
        return False
    registry = {a.id: a for a in arms.read(conn)}
    if any(i not in registry for i in ids) or len({registry[i].family for i in ids}) != len(ids):
        return False
    old = list(conn.execute("SELECT arm_id,revision FROM bandit_credit WHERE line_id=?", (line_id,)))
    if {tuple(r) for r in old} == {(i, revision) for i in ids}:
        return False
    # Positive credit is capped; mute/downvote penalties are never diluted by volume.
    effective = value * cap if value > 0 else value
    success, failure = (1+effective)/2/len(ids), (1-effective)/2/len(ids)
    conn.execute("DELETE FROM bandit_credit WHERE line_id=?", (line_id,))
    for aid in ids:
        conn.execute("INSERT INTO bandit_credit VALUES (?,?,?,?,?,?,?,?,?)",
                     (line_id, aid, guid, key, revision, ts, success, failure,
                      int(verdict in ("rewrite", "react", "drop", "failed_open"))))
    retire(conn, guid, key, now=now)
    return True


def retire(conn, guid, key, *, now=None):
    stats = posterior(conn, guid, key, now=now)
    for arm in arms.read(conn):
        p = stats.get(arm.id)
        control = stats.get(arms.controls()[arm.family])
        if arm.status == "active" and p and control and p["bad"]/p["attempts"] > control["bad"]/control["attempts"]:
            conn.execute("UPDATE learn_arm SET status='retired' WHERE id=?", (arm.id,))
            log("bandit_guard_retired", arm_id=arm.id, guid=guid, uses=p["uses"], bad=p["bad"])


def reject(conn, guid, context, selected, *, attempt_id, reason):
    """Task 39's send guards can report unsent drafts without inventing a chat/reward row."""
    if not enabled(context.settings, guid) or not arms.available(conn):
        return False
    if reason not in ("guard", "content") or not isinstance(attempt_id,str) or not 1 <= len(attempt_id) <= 64:
        raise ValueError("bandit_rejection_invalid")
    registry = {a.id:a for a in arms.read(conn)}
    if not selected or any(f not in arms.FAMILIES or a not in registry or registry[a].family != f for f,a in selected.items()):
        raise ValueError("bandit_rejection_invalid")
    for aid in selected.values():
        conn.execute("INSERT OR IGNORE INTO bandit_rejection VALUES (?,?,?,?,?)", (attempt_id,aid,guid,context.key,reason))
    retire(conn,guid,context.key)
    return True
