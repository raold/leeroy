"""Rewarded public jokes reuse the lexicon's full origin/provenance screens."""
from leeroy_mind.nightly import lexicon
from leeroy_mind.nightly.evidence import load_facts
from leeroy_mind.nightly.language_screens import evidence, screen
from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.text import truncate_words

STAGE = "banter_jokes"


def attach(runner,path):
    from .config import load
    if not load(path/"mind.toml").enabled:
        return
    names = {s.name for s in runner.stages}
    runner.add_stage(STAGE,stage_jokes,after="rewards" if "rewards" in names else "language",model=None)


def reward_jokes(ctx):
    store = ctx.store
    from leeroy_mind.learn.config import load_learn_config
    from leeroy_mind.learn.control import frozen
    data_dir = getattr(getattr(ctx, "settings", None), "data_dir", None)
    if frozen(store.conn, load_learn_config(data_dir/"mind.toml") if data_dir is not None else None):
        return 0  # the Lab's or mind.toml's learning freeze holds rewarded jokes too
    if not store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='talk_reward'").fetchone():
        return 0  # Task 30 may not yet be installed; ordinary origin-backed jokes still work.
    rows = store.conn.execute("SELECT r.line_id FROM talk_reward r JOIN chat_line c ON c.id=r.line_id "
        "WHERE r.value*r.cap_weight>=0.5 AND c.channel IN ('guild','discord:general') "
        "AND c.ts>=? AND c.ts<? AND EXISTS(SELECT 1 FROM talk_signal s WHERE s.line_id=r.line_id "
        "AND s.kind='reason_funny' AND s.value>0) AND EXISTS(SELECT 1 FROM talk_signal s "
        "WHERE s.line_id=r.line_id AND s.kind='explicit' AND s.value>0) "
        "ORDER BY r.value DESC,c.id LIMIT 20",(ctx.window[1]-7*86400,ctx.window[1])).fetchall()
    facts = load_facts(ctx)
    written = 0
    for row in rows:
        origin = f"chat:{row[0]}"
        if store.conn.execute("SELECT 1 FROM lexicon WHERE origin_ref=?",(origin,)).fetchone():
            continue
        line = lexicon.public_line(store,row[0])
        if line is None or lexicon.public_sources(store,(origin,)) is None:
            continue
        claim,verdict = screen(ctx,"joke",line["from_guid"],line["text"],[origin],
            evidence(ctx,[origin]),facts)
        if not verdict.ok:
            continue
        term = truncate_words(redact_real_name(claim.text,ctx.ident,ctx.ident.called),120)
        ident = lexicon.create_term(store,ctx,kind="joke",term=term,origin_ref=origin,
            originator=line["from_guid"],adopters={line["from_guid"]},now=ctx.clock.now(),refs=line["refs"])
        written += ident is not None
    return written


async def stage_jokes(ctx):
    async def one(unit):
        ctx.commit(STAGE,unit,lambda store:reward_jokes(ctx))
    await ctx.map_units(STAGE,["public"],one)
