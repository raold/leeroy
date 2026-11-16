"""The render-parameter bandit ([selfies] param_bandit, with critic on): per context
('<shape>:<framing>', a square profile set's or a portrait scene's), Thompson sampling over
identity scale {0.3, 0.45, 0.6} x steps {30, 40}, the learn bandit's way (learn/bandit.py):
Beta(1, 1) priors, credit that decays with a 21-day half-life (learn.reward.decayed), one
replaceable credit row per source. The bounds are the grid: a draw never leaves it.

Credit comes from the critic (each settled candidate: its judge's overall / 3, or 1 clean and 0
flagged on the detectors alone) and from his picks (selfie_pick rows: the picked candidate's
parameters succeed, the set's other candidates' fail a third each; an undo takes the pick's
credit back). Off, or with learning frozen (the Lab's freeze or mind.toml's [learn] freeze), it
draws nothing and writes nothing: every render takes today's parameters. Logs
selfie_param_chosen framing= scale= steps= (the context and the draw)."""
import random
import time
from dataclasses import dataclass

from leeroy_mind.logs import log

SCALES = (0.3, 0.45, 0.6)
STEPS = (30, 40)
SHAPES = ("square", "portrait")
PICK_KEY = "selfies:param:picks"  # the newest selfie_pick id credited
PICK_FAIL = 1 / 3  # each candidate he passed over, against his pick's 1


@dataclass(frozen=True)
class Params:
    scale: float
    steps: int
    arm: bool = False  # the bandit drew these


def context(shape: str, framing: str) -> str:
    if shape not in SHAPES or type(framing) is not str or not 1 <= len(framing) <= 30:
        raise ValueError("param_context_invalid")
    return f"{shape}:{framing}"


def frozen(store, data_dir) -> bool:
    from leeroy_mind.learn.config import load_learn_config
    from leeroy_mind.learn.control import frozen as learn_frozen
    cfg = load_learn_config(data_dir / "mind.toml") if data_dir is not None else None
    return learn_frozen(store.conn, cfg)


def active(cfg, store, data_dir) -> bool:
    return (getattr(cfg, "critic", False) and getattr(cfg, "param_bandit", False)
            and not frozen(store, data_dir))


def _posterior(conn, ctx: str, now: float) -> dict[tuple[float, int], list[float]]:
    from leeroy_mind.learn.reward import decayed
    out = {(s, n): [1.0, 1.0] for s in SCALES for n in STEPS}
    for scale, steps, ts, success, failure in conn.execute(
            "SELECT scale, steps, ts, success, failure FROM selfie_param_credit WHERE context=?",
            (ctx,)):
        key = (round(scale, 2), steps)
        if key in out:
            age = max(0.0, now - ts)
            out[key][0] += decayed(success, age_seconds=age)
            out[key][1] += decayed(failure, age_seconds=age)
    return out


def choose(store, cfg, data_dir, *, shape: str, framing: str, control: tuple[float, int],
           rng: random.Random | None = None, now: float | None = None) -> Params:
    """The parameters of one render: the bandit's draw for this context while it is active,
    else `control` (today's scale and steps) with nothing written."""
    if not active(cfg, store, data_dir):
        return Params(*control)
    ctx = context(shape, framing)
    now = time.time() if now is None else now
    rng = rng or random.Random()
    with store.transaction():
        post = _posterior(store.conn, ctx, now)
        scale, steps = max(sorted(post), key=lambda k: rng.betavariate(*post[k]))
        store.conn.execute(
            "INSERT INTO selfie_param_arm(context, scale, steps, drawn) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(context, scale, steps) DO UPDATE SET drawn = drawn + 1",
            (ctx, scale, steps))
    log("selfie_param_chosen", framing=ctx, scale=scale, steps=steps)
    return Params(scale, steps, True)


def _on_grid(scale, steps) -> bool:
    return (scale is not None and steps is not None and round(scale, 2) in SCALES
            and steps in STEPS)


def credit_row(store, cfg, data_dir, row: dict, verdict, judged, *, shape: str,
               now: float) -> bool:
    """The critic's credit for one settled candidate (critic.settle's on_settled): its judge's
    overall / 3, else 1 clean and 0 flagged. Only for parameters on the grid, while active."""
    if (not active(cfg, store, data_dir) or not _on_grid(row.get("scale"), row.get("steps"))
            or not row.get("framing")):
        return False
    reward = judged.overall / 3 if judged is not None else (1.0 if verdict.clean else 0.0)
    with store.transaction():
        store.conn.execute(
            "INSERT OR REPLACE INTO selfie_param_credit(source, context, scale, steps, ts, "
            "success, failure) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"critic:{row['id']}", context(shape, row["framing"]), round(row["scale"], 2),
             row["steps"], now, reward, 1 - reward))
    return True


def credit_picks(store, cfg, data_dir, *, now: float) -> int:
    """His picks since the last pass credited (and undone ones taken back): the picked
    candidate's parameters 1, each other candidate of that set PICK_FAIL of a failure. Only
    sets the critic measured (their rows carry the parameters). Returns the picks read."""
    if not active(cfg, store, data_dir):
        return 0
    with store.transaction():
        conn = store.conn
        seen = int(store.get_meta(PICK_KEY) or 0)
        picks = conn.execute("SELECT id, guid, undoes, after_set, after_idx FROM selfie_pick "
                             "WHERE id > ? ORDER BY id", (seen,)).fetchall()
        for pick_id, guid, undoes, set_no, idx in picks:
            if undoes is not None:
                conn.execute("DELETE FROM selfie_param_credit WHERE source GLOB ?",
                             (f"pick:{int(undoes)}:*",))
                continue
            rows = conn.execute(
                "SELECT idx, framing, scale, steps FROM selfie_critic WHERE guid=? AND set_no=? "
                "AND purpose='set'", (guid, set_no)).fetchall()
            for c_idx, framing, scale, steps in rows:
                if not framing or not _on_grid(scale, steps):
                    continue
                hit = c_idx == idx
                conn.execute(
                    "INSERT OR REPLACE INTO selfie_param_credit(source, context, scale, steps, "
                    "ts, success, failure) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (f"pick:{pick_id}:{c_idx}", context("square", framing), round(scale, 2),
                     steps, now, 1.0 if hit else 0.0, 0.0 if hit else PICK_FAIL))
        if picks:
            store.set_meta(PICK_KEY, str(picks[-1][0]))
    return len(picks)


def arms(conn) -> list[dict]:
    """Read-only: every context's arms with draws and posterior means (now)."""
    now = time.time()
    contexts = sorted({r[0] for r in conn.execute(
        "SELECT context FROM selfie_param_arm UNION SELECT context FROM selfie_param_credit")})
    out = []
    for ctx in contexts:
        post = _posterior(conn, ctx, now)
        drawn = {(round(s, 2), n): d for s, n, d in conn.execute(
            "SELECT scale, steps, drawn FROM selfie_param_arm WHERE context=?", (ctx,))}
        for key, (a, b) in sorted(post.items()):
            out.append({"context": ctx, "scale": key[0], "steps": key[1],
                        "drawn": drawn.get(key, 0), "mean": round(a / (a + b), 3)})
    return out
