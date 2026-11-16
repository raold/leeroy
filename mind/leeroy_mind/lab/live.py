"""The lab's prompt pieces while a turn composes (TalkEngine._compose): which texts are in force
for the turn's pieces (talk/pieces.py), and what the learning credit records afterwards.

Two sources, both by name at render time:
- a sandbox mind's overlays (`[lab] overlays = true`; `prompts/lab_<piece>.txt` in its own copied
  data dir, written there by lab/child.py for a variant's arm);
- the live learn bandit's lab arms (`prompt.<piece>` families): with [learn] apply on, a turn
  that may render a piece with an active lab arm draws that family once (bandit.choose_pieces,
  kept on the turn's meta so a guard's rewrite reads the same text), and learn.apply merges the
  draw into the turn's selection. When the composition is done, `settle` keeps a family in the
  recipe only when its piece was rendered, so its credit is a line that read it.

Logs carry ids and piece names only (`lab_piece: guid= piece= source=`)."""
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field, replace

from leeroy_mind.discord.channels import is_discord, is_private
from leeroy_mind.talk import pieces
from leeroy_mind.talk.turns import TurnKind

log = logging.getLogger("leeroy_mind")


@dataclass
class Pick:
    selected: dict = field(default_factory=dict)   # family -> arm id (a control or a lab arm)
    texts: dict = field(default_factory=dict)      # piece -> the arm's text (lab arms only)
    rendered: set = field(default_factory=set)     # pieces any composition of the turn read


def families_for(turn, human: int) -> list[str]:
    """The prompt families whose piece this turn may render (it may still not: the romance lead
    is drawn, situation mode may be off)."""
    from leeroy_mind.learn.arms import PROMPT_PREFIX
    out = []
    if turn.kind is TurnKind.REPLY_HUMAN:
        out += ["reply_human", "situation_reply_human"]
    elif turn.kind is TurnKind.CONTINUE:
        out.append("continue")
    elif turn.kind is TurnKind.GOODNIGHT:
        out.append("goodnight")
    to_him = bool(human) and human in (turn.addressee, turn.to_guid)
    if is_discord(turn.channel) and is_private(turn.channel):
        out.append("discord_dm")
    if to_him:
        out += ["romance_hook", "romance_lead"]
        if turn.channel == "whisper" or is_discord(turn.channel) and is_private(turn.channel):
            out.append("beat_ask")
    return [PROMPT_PREFIX + n for n in out]


def _variant_text(conn, arm) -> str | None:
    piece, variant = arm.params["piece"], arm.params["variant"]
    row = conn.execute("SELECT text FROM prompt_variant WHERE id=? AND piece=?",
                       (variant, piece)).fetchone()
    return row[0] if row is not None and pieces.fits(piece, row[0]) else None


def _pick(engine, turn, meta) -> Pick:
    """The turn's draw over the prompt families with an active lab arm; an empty Pick when
    learning does not apply to the turn or no lab arm is active for its pieces."""
    from leeroy_mind.learn import apply, arms, bandit
    if getattr(meta, "lab", None) is not None:
        return meta.lab
    pick = Pick()
    cfg = apply.active(engine, turn.speaker) if getattr(engine, "learn_enabled", False) else None
    if cfg is None or turn.template is not None:
        return pick
    persona = engine.personas.get(turn.speaker)
    if persona is None:
        return pick
    conn = engine.store.conn
    if not arms.available(conn) or conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='prompt_variant'").fetchone() is None:
        return pick
    families = families_for(turn, engine.human.guid)
    if not families:
        return pick
    if conn.execute("SELECT 1 FROM learn_arm WHERE status='active' AND family IN "
                    f"({','.join('?' * len(families))}) LIMIT 1", families).fetchone() is None:
        return pick  # nothing to draw: no write, no draw from the engine's generator
    context = apply.context_for(engine, turn, persona, cfg)
    with engine.store.transaction():
        selected = bandit.choose_pieces(conn, turn.speaker, context, families, rng=engine.rng,
                                        now=engine.clock.now())
        registry = {a.id: a for a in arms.read(conn)}
        for family, aid in selected.items():
            arm = registry.get(aid)
            if arm is not None and arm.status != "control":
                text = _variant_text(conn, arm)
                if text is None:  # a variant that no longer fits reads as its control
                    selected[family] = arms.control_of(family)
                else:
                    pick.texts[arm.params["piece"]] = text
    pick.selected = selected
    return pick


@contextmanager
def composing(engine, turn, meta):
    """While the block composes a live turn: the texts in force for its pieces, and afterwards
    the pieces it read on the turn's meta and the learning selection settled. With no overlay
    dir and no lab arm in force nothing is set and `pieces.text` returns the built-in wording."""
    overlays = getattr(engine, "lab_overlays", None)
    pick = _pick(engine, turn, meta)
    if meta is not None and pick.selected:
        meta.lab = pick
    texts = dict(pick.texts)
    sources = {k: "arm" for k in texts}
    if overlays is not None:
        found = pieces.read_overlays(overlays)
        texts.update(found)
        sources.update({k: "overlay" for k in found})
    if not texts and not pick.selected:
        yield
        return
    rendered: set[str] = set()
    try:
        with pieces.using(texts, rendered):
            yield
    finally:
        new = rendered - pick.rendered
        pick.rendered |= rendered
        for name in sorted(new & set(texts)):
            log.info("lab_piece: guid=%d piece=%s source=%s", turn.speaker, name, sources[name])
        settle(meta, pick)


def settle(meta, pick: Pick) -> None:
    """Keep a prompt family in the turn's learning selection (and so its recipe and credit) only
    when its piece was rendered."""
    from leeroy_mind.learn.arms import PROMPT_PREFIX
    from leeroy_mind.learn.recipe import Snapshot
    applied = getattr(meta, "learning", None)
    if applied is None or not pick.selected:
        return
    for family in pick.selected:
        if family[len(PROMPT_PREFIX):] not in pick.rendered:
            applied.selected.pop(family, None)
        elif family not in applied.selected:
            applied.selected[family] = pick.selected[family]
    snapshot = getattr(meta, "recipe_snapshot", None)
    if isinstance(snapshot, Snapshot) and snapshot.arm_ids:
        meta.recipe_snapshot = replace(snapshot, arm_ids=tuple(applied.selected.values()))
