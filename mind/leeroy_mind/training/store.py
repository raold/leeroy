"""Training storage. Public writes own one connection transaction.

Do not call these inside ``Store.transaction()``. Pulls and observations keep
their first committed receipt; a replay cannot duplicate or revise their facts.
Rows are mappings (or dataclasses with ``to_row``); JSON fields accept decoded
values or valid JSON. Only expired positional tracks are hard-deleted.

Metric consumers must inspect each recorded basis. ``requested_pre_ai`` is not
effective damage or health loss; ``effective_gain_with_pre_absorb_raw`` permits
actual healing gain but not exact overheal efficiency. ``best_effort_thread_pair``
cannot prove a damage/heal spell or death cause. ``successful_interrupt_spell_cast``
counts completed interrupt-spell casts, not confirmed interruptions. ``unknown``
establishes none of those stronger claims. These restrictions do not invalidate
independent observed deaths, cast callbacks, positions or elapsed time.
"""

import dataclasses
import json
import math
from collections.abc import Mapping

from leeroy_mind.wow import is_raid_fight


def _plain(value):
    if dataclasses.is_dataclass(value):
        return {f.name.rstrip("_"): _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _json(value):
    return json.dumps(_plain(value), ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _row(value):
    if hasattr(value, "to_row"):
        value = value.to_row()
    if dataclasses.is_dataclass(value):
        return {f.name.rstrip("_"): getattr(value, f.name) for f in dataclasses.fields(value)}
    return {key.rstrip("_"): item for key, item in dict(value).items()}


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _insert(conn, table, row, columns, json_fields=()):
    """Internal insert with a caller-owned transaction and fixed table allowlists."""
    values = _row(row)
    if values.get("id") is None:
        values.pop("id", None)
    if values.keys() - set(columns):
        raise ValueError("unknown training row fields")
    for name in json_fields:
        if name in values:
            value = values[name]
            values[name] = _json(json.loads(value) if isinstance(value, str) else value)
    keys = list(values)
    return conn.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                        [values[k] for k in keys]).lastrowid


def put_pull(conn, rec, ctx, roles, *, received, scoreable=None) -> None:
    """Atomically save one assembled pull. Explicit False supports ledger vetoes.

    An explicit True still cannot score incomplete, aborted, or non-fight data.
    Replay keeps the original context and receipt, including pruned tracks.
    """
    h = rec.header
    eligible = bool(rec.complete and h.outcome != "aborted" and is_raid_fight(h.map, h.boss_id))
    eligible = eligible and scoreable is not False
    context = f"{h.map}:{h.boss_id}:{h.difficulty}"
    with conn:
        if conn.execute("SELECT 1 FROM tg_pull WHERE id=?", (h.id,)).fetchone():
            return
        conn.execute("INSERT INTO tg_pull(id,context,map,instance_id,boss_id,difficulty,size,outcome,"
            "start_ms,end_ms,members,assisted,boss_entry,upd_mean_ms,kind,lead_guid,raid_id,card_id,arm_id,"
            "change,complete,scoreable,deaths,covariates,received,damage_basis,healing_basis,spell_basis,"
            "interrupt_basis) VALUES (" + ",".join("?" * 29) + ")",
            (h.id, context, h.map, h.instance_id, h.boss_id, h.difficulty, h.size, h.outcome,
             h.start_ms, h.end_ms, h.members, int(h.assisted), h.boss_entry, h.upd_mean_ms,
             ctx.kind, ctx.lead_guid, ctx.raid_id, ctx.card_id, ctx.arm_id, ctx.change,
             int(rec.complete), int(eligible), h.deaths, _json(ctx.covariates), received,
             h.damage_basis, h.healing_basis, h.spell_basis, h.interrupt_basis))
        for member in rec.members:
            if member.pull != h.id:
                raise ValueError("member belongs to another pull")
            detail = {k: getattr(member, k) for k in ("tank", "done", "taken_by", "debuffs", "casts")}
            conn.execute("INSERT INTO tg_member(pull,guid,class,spec_tab,role,ilvl,damage,damage_boss,"
                "healing,overheal,taken,deaths,interrupts,active_ms,detail) VALUES (" + ",".join("?" * 15) + ")",
                (h.id, member.guid, member.class_, member.spec_tab, roles[member.guid], member.ilvl,
                 member.damage, member.damage_boss, member.healing, member.overheal, member.taken,
                 member.deaths, member.interrupts, member.active_ms, _json(detail)))
        for death in rec.deaths:
            if death.pull != h.id:
                raise ValueError("death belongs to another pull")
            conn.execute("INSERT INTO tg_death(pull,guid,at_ms,spell,source,x,y,hits) VALUES (?,?,?,?,?,?,?,?)",
                         (h.id, death.guid, death.at_ms, death.spell, death.source, death.x, death.y, _json(death.hits)))
        for (who, entry), data in rec.tracks.items():
            data = dict(data)
            if who == 0:
                data["casts"] = rec.casts
            conn.execute("INSERT INTO tg_track(pull,who,entry,data) VALUES (?,?,?,?)",
                         (h.id, who, entry, _json(data)))


def pull_rows(conn, *, context=None, kinds=None, since_ms=None, scoreable=None):
    where, params = [], []
    for key, value, op in (("context", context, "="), ("start_ms", since_ms, ">="),
                           ("scoreable", scoreable, "=")):
        if value is not None:
            where.append(f"{key}{op}?")
            params.append(value)
    if kinds is not None:
        kinds = (kinds,) if isinstance(kinds, str) else tuple(kinds)
        if not kinds:
            return []
        where.append("kind IN (" + ",".join("?" * len(kinds)) + ")")
        params.extend(kinds)
    return conn.execute("SELECT * FROM tg_pull" + (" WHERE " + " AND ".join(where) if where else "")
                        + " ORDER BY start_ms,id", params).fetchall()


def member_rows(conn, pull_id):
    return conn.execute("SELECT * FROM tg_member WHERE pull=? ORDER BY guid", (pull_id,)).fetchall()


def death_rows(conn, pull_id):
    # Arrival order matches CheckResult.causes and is stable across rechecks.
    return conn.execute("SELECT * FROM tg_death WHERE pull=? ORDER BY id", (pull_id,)).fetchall()


def track_rows(conn, pull_id):
    return conn.execute("SELECT * FROM tg_track WHERE pull=? ORDER BY who,entry", (pull_id,)).fetchall()


def set_checks(conn, pull_id, failures, causes, *, deaths, failed, version):
    failures, causes = [_row(f) for f in failures], tuple(causes)
    keys = [(f["kind"], f["key"], f["guid"]) for f in failures]
    if len(set(keys)) != len(keys) or type(version) is not int or version < 1:
        raise ValueError("invalid check result")
    with conn:
        old = conn.execute("SELECT checks_version FROM tg_pull WHERE id=?", (pull_id,)).fetchone()
        if old is None:
            raise ValueError("unknown pull")
        if old[0] >= version:
            return
        detail = death_rows(conn, pull_id)
        if len(detail) != len(causes):
            raise ValueError("death causes do not match recorded deaths")
        # Keep historical row identities. A no-longer-present failure becomes
        # zero rather than deleting an audit row during a new checks version.
        conn.execute("UPDATE tg_failure SET count=0,deaths=0,damage_share=0 WHERE pull=?", (pull_id,))
        for f in failures:
            key = (pull_id, f["kind"], f["key"], f["guid"])
            old_failure = conn.execute("SELECT id FROM tg_failure WHERE pull=? AND kind=? AND key=? AND guid=?", key).fetchone()
            if old_failure:
                conn.execute("UPDATE tg_failure SET count=?,deaths=?,damage_share=? WHERE id=?",
                             (f["count"], f["deaths"], f["damage_share"], old_failure[0]))
            else:
                conn.execute("INSERT INTO tg_failure(pull,kind,key,guid,count,deaths,damage_share) VALUES (?,?,?,?,?,?,?)",
                             (*key, f["count"], f["deaths"], f["damage_share"]))
        conn.executemany("UPDATE tg_death SET cause=? WHERE id=?",
                         [(cause, row["id"]) for row, cause in zip(detail, causes)])
        conn.execute("UPDATE tg_pull SET deaths=?,failed_mechanics=?,checks_version=?,score=NULL WHERE id=?",
                     (deaths, failed, version, pull_id))


def set_score(conn, pull_id, score):
    # This is the death/mechanic/duration pull score, not a damage/DPS score.
    # Producers must gate each damage-derived mechanic/spec metric on its basis;
    # requested_pre_ai and unknown are not actual damage or taken-health loss.
    if score is not None and not _finite(score):
        raise ValueError("score must be finite")
    with conn:
        conn.execute("UPDATE tg_pull SET score=? WHERE id=? AND scoreable=1", (score, pull_id))


def prune_tracks(conn, before_ms):
    with conn:
        return conn.execute("DELETE FROM tg_track WHERE pull IN (SELECT id FROM tg_pull WHERE end_ms<?)",
                            (before_ms,)).rowcount


_CARD = "id context version lineage status origin directives call assignment reason evidence vetoes prev_champion embedding created changed".split()


def put_card(conn, card) -> int:
    with conn:
        return _insert(conn, "tg_card", card, _CARD, ("directives", "assignment", "evidence", "vetoes"))


def card_rows(conn, context, statuses=None):
    sql, params = "SELECT * FROM tg_card WHERE context=?", [context]
    if statuses is not None:
        statuses = (statuses,) if isinstance(statuses, str) else tuple(statuses)
        if not statuses:
            return []
        sql += " AND status IN (" + ",".join("?" * len(statuses)) + ")"
        params.extend(statuses)
    return conn.execute(sql + " ORDER BY version,id", params).fetchall()


def champion(conn, context):
    return conn.execute("SELECT * FROM tg_card WHERE context=? AND status='champion'", (context,)).fetchone()


def set_card_status(conn, card_id, status, *, ts):
    with conn:
        conn.execute("UPDATE tg_card SET status=?,changed=? WHERE id=?", (status, ts, card_id))


def put_lesson(conn, context, night, text, evidence):
    with conn:
        conn.execute("INSERT INTO tg_lesson(context,night,text,evidence) VALUES (?,?,?,?) "
                     "ON CONFLICT(context,night) DO NOTHING", (context, night, text, _json(evidence)))


def lessons(conn, context, n=3):
    return conn.execute("SELECT * FROM tg_lesson WHERE context=? ORDER BY night DESC LIMIT ?",
                        (context, max(0, n))).fetchall()


def put_points(conn, context, night, points):
    """Points are {fact, text} mappings; identical nightly facts are idempotent."""
    with conn:
        for point in points:
            row = _row(point)
            if not conn.execute("SELECT 1 FROM tg_point WHERE context=? AND night=? AND fact=?",
                                (context, night, row["fact"])).fetchone():
                conn.execute("INSERT INTO tg_point(context,night,fact,text) VALUES (?,?,?,?)",
                             (context, night, row["fact"], row["text"]))


def unused_points(conn, context):
    return conn.execute("SELECT * FROM tg_point WHERE context=? AND used IS NULL ORDER BY night,id", (context,)).fetchall()


def open_trial(conn, scope, lever, metric, *, opened, arms=()) -> int:
    """Create a trial plus {card, champion} arm mappings in one transaction."""
    with conn:
        ident = conn.execute("INSERT INTO tg_trial(scope,lever,metric,status,opened) VALUES (?,?,?,'open',?)",
                             (scope, lever, metric, opened)).lastrowid
        for arm in arms:
            row = _row(arm)
            conn.execute("INSERT INTO tg_arm(trial,card,champion,status) VALUES (?,?,?,'live')",
                         (ident, row.get("card"), int(row.get("champion", False))))
        return ident


def trial_rows(conn, scope=None, *, status=None):
    return conn.execute("SELECT * FROM tg_trial WHERE (? IS NULL OR scope=?) AND (? IS NULL OR status=?) "
                        "ORDER BY opened,id", (scope, scope, status, status)).fetchall()


def put_obs(conn, arm, pull, context, value, *, fails=0.0):
    if not _finite(value) or not _finite(fails):
        raise ValueError("observations must be finite")
    with conn:
        conn.execute("INSERT INTO tg_obs(arm,pull,context,value,fails) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(arm,pull) DO NOTHING", (arm, pull, context, value, fails))


def obs_rows(conn, arm=None, *, context=None):
    return conn.execute("SELECT * FROM tg_obs WHERE (? IS NULL OR arm=?) AND (? IS NULL OR context=?) "
                        "ORDER BY pull,arm", (arm, arm, context, context)).fetchall()


def close_trial(conn, trial_id, status, *, ts, verdict=""):
    if status == "open":
        raise ValueError("closed trial needs a terminal status")
    with conn:
        conn.execute("UPDATE tg_trial SET status=?,closed=?,verdict=? WHERE id=? AND status='open'",
                     (status, ts, verdict, trial_id))


def log_applied(conn, run, layer, context, cmd, inverse, ts) -> int:
    with conn:
        return conn.execute("INSERT INTO tg_applied(ts,run,layer,context,command,inverse) VALUES (?,?,?,?,?,?)",
                            (ts, run, layer, context, _json(cmd), _json(inverse))).lastrowid


def open_applied(conn, since_ts):
    return conn.execute("SELECT * FROM tg_applied WHERE undone IS NULL AND ts>=? ORDER BY ts,id", (since_ts,)).fetchall()


def mark_undone(conn, ids, ts):
    with conn:
        conn.executemany("UPDATE tg_applied SET undone=? WHERE id=? AND undone IS NULL", [(ts, i) for i in ids])


def put_rotation(conn, row) -> int:
    with conn:
        return _insert(conn, "tg_rotation", row,
                       "id name class spec_tab lines origin parent status created".split(), ("lines",))


def rotation_rows(conn, *, class_=None, spec_tab=None, status=None):
    return conn.execute("SELECT * FROM tg_rotation WHERE (? IS NULL OR class=?) AND (? IS NULL OR spec_tab=?) "
                        "AND (? IS NULL OR status=?) ORDER BY created,id",
                        (class_, class_, spec_tab, spec_tab, status, status)).fetchall()


def put_dummy(conn, row) -> int:
    with conn:
        return _insert(conn, "tg_dummy", row,
                       "id rotation guid round seconds dps stock_dps dropped ts damage_basis spell_basis".split())


def put_setup(conn, row) -> int:
    with conn:
        return _insert(conn, "tg_setup", row,
                       "id guid since rotation toggles vals premade copied_from explored applied".split(), ("toggles", "vals"))


def latest_setups(conn):
    return {row["guid"]: row for row in conn.execute("SELECT * FROM tg_setup ORDER BY since,id")}


def put_benchmark(conn, row) -> int:
    row = _row(row)
    with conn:
        old = conn.execute("SELECT id FROM tg_benchmark WHERE week=?", (row["week"],)).fetchone()
        if old:
            return old[0]
        return _insert(conn, "tg_benchmark", row,
                       "id week raid_key guild_id members bosses outcome kills wipes ts".split(), ("members", "bosses"))


def put_perf(conn, ts, fight, context, kind, armed, mean_ms):
    with conn:
        # A fight's armed state is fixed; a replay must not inflate gate samples.
        if not conn.execute("SELECT 1 FROM tg_perf WHERE fight=? AND context=? AND kind=? AND armed=?",
                            (fight, context, kind, int(armed))).fetchone():
            conn.execute("INSERT INTO tg_perf(ts,fight,context,kind,armed,mean_ms) VALUES (?,?,?,?,?,?)",
                         (ts, fight, context, kind, int(armed), mean_ms))


def perf_rows(conn, *, kind="offline"):
    return conn.execute("SELECT * FROM tg_perf WHERE kind=? ORDER BY ts,fight", (kind,)).fetchall()
