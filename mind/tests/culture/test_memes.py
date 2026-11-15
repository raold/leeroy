"""Culture Part C: original memes. The night writes one screened caption for a public running joke,
a plain worker renders a scene with no people from fixed words alone, the worker captions it, and
a member posts it as a `meme` attachment. Fakes only: no model, no GPU, no real worker; every name
here is invented ("Quillon" is the fixture real name)."""
import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import TEST_CAST, run
from nightly.conftest import IDENT
from nightly.test_night_lexicon import term
from selfies.fake_worker import JPEG
from leeroy_mind.culture import memes
from leeroy_mind.culture.config import CultureConfig
from leeroy_mind.selfies.config import SelfieCfg
from leeroy_mind.selfies.worker import Captioned, Rendered

SELFIES = SelfieCfg(enabled=True)
CAPTION = {"top": "when the floor inspector", "bottom": "inspects the floor again", "picture": "tipped_chair"}


def app_for(per_week=3):
    return SimpleNamespace(culture_config=CultureConfig(enabled=True, memes=True, memes_per_week=per_week))


def joke(h, butt=102):
    ident = term(h)
    h.store.conn.execute("UPDATE lexicon SET butt=? WHERE id=?", (butt, ident))
    h.store.conn.commit()
    return ident


def plan(h, reply=CAPTION, per_week=3):
    stage = memes.make_plan_stage(app_for(per_week), SELFIES)
    run(stage(h.ctx({"culture_meme": [reply]} if reply else {})))
    return [dict(r) for r in h.store.conn.execute("SELECT * FROM discord_meme ORDER BY id")]


def planned(h, ident, *, top="when the floor inspector", bottom="strikes", scene="tipped_chair",
            base="scene", snap=None, created=None):
    return h.store.conn.execute(
        "INSERT INTO discord_meme(night,joke,base,snap,scene,top,bottom,status,created) "
        "VALUES ('2026-10-29',?,?,?,?,?,?,'planned',?)",
        (ident, base, snap, scene, top, bottom, h.clock.now() if created is None else created)).lastrowid


class Worker:
    """The selfie worker's surface the meme stage uses, in process: renders and captions write
    the fake JPEG unless flagged, and every request is kept for inspection."""

    def __init__(self, *, flag_render=False, flag_caption=False):
        self.flag_render, self.flag_caption = flag_render, flag_caption
        self.renders, self.captions, self.running, self.stopped = [], [], False, 0

    async def start(self):
        self.running = True

    async def render(self, req, *, rid=None):
        self.renders.append(req)
        if not self.flag_render:
            req.out.write_bytes(JPEG)
            req.thumb.write_bytes(JPEG)
        return Rendered(self.flag_render, 0.95 if self.flag_render else 0.02, "p", 3, 0.1, 1.0, 1024, 1024)

    async def caption(self, base, *, top, bottom, out, nsfw_max, rid=None):
        self.captions.append((base, top, bottom, out))
        if not self.flag_caption:
            out.write_bytes(JPEG)
        return Captioned(self.flag_caption, 0.95 if self.flag_caption else 0.03, 1024, 1024, 0.1)

    async def stop(self):
        self.running = False
        self.stopped += 1


def render(h, worker):
    stage = memes.make_render_stage(app_for(), SELFIES, worker=lambda: worker)
    ctx = h.ctx()
    wanted = memes.wants_gpu(ctx)
    run(stage(ctx))
    return wanted


def names():
    return [n for n, *_ in TEST_CAST.values()] + [IDENT.real_name, IDENT.character_name, IDENT.nickname]


def test_plan_writes_one_screened_caption_for_a_public_joke(h):
    ident = joke(h)
    rows = plan(h)
    assert [(r["joke"], r["base"], r["scene"], r["top"], r["bottom"], r["status"]) for r in rows] == [
        (ident, "scene", "tipped_chair", CAPTION["top"], CAPTION["bottom"], "planned")]
    assert plan(h) == rows  # one a night: the unit is done


def test_memes_per_week_caps_planning(h, caplog):
    ident = joke(h)
    now = h.clock.now()
    assert memes.plan_due(h.store.conn, now, 0) == "off"
    planned(h, ident, created=now - 8 * 86400)  # last week's no longer counts
    assert memes.plan_due(h.store.conn, now, 1) is None
    planned(h, ident, created=now - 2 * 86400)
    assert memes.plan_due(h.store.conn, now, 1) == "cap"
    assert memes.plan_due(h.store.conn, now, 2) == "spacing"  # within 0.8 of half a week
    assert memes.plan_due(h.store.conn, now, 3) is None
    with caplog.at_level(logging.INFO):
        rows = plan(h, reply=None, per_week=1)  # no model call is made: none is scripted
    assert len(rows) == 2 and "code=cap" in caplog.text


@pytest.mark.parametrize("reply,code", [
    ({**CAPTION, "top": "Quillon inspects the floor"}, "real_name"),     # his real name
    ({**CAPTION, "top": "when Coros pulls"}, "name"),                     # a member not in the joke
    ({**CAPTION, "bottom": "in her lingerie"}, "explicit"),
    ({**CAPTION, "bottom": "a teenager again"}, "age"),
    ({**CAPTION, "top": "", "bottom": ""}, "empty"),
    ({**CAPTION, "bottom": "the floor again \U0001F602"}, "glyph"),   # the font has no emoji
    ({**CAPTION, "bottom": "\u043f\u043e\u043b"}, "glyph"),          # nor Cyrillic
])
def test_a_screened_caption_stores_nothing(h, caplog, reply, code):
    joke(h)
    with caplog.at_level(logging.INFO):
        assert plan(h, reply=reply) == []
    assert code in caplog.text and "inspect" not in caplog.text.lower()  # codes, never the caption


def test_meme_scene_prompt_has_no_names(h, tmp_path):
    for key in memes.MEME_SCENES:
        phrases, negative = memes.scene_spec(key)
        words = " ".join(t for t, _ in (*phrases, *negative)).lower()
        assert not any(n.lower() in words for n in names()), key
    ident = joke(h)  # its butt is Belra, named in the caption: drawn, never prompted
    planned(h, ident, top="when Belra inspects", bottom="the floor again", scene="burnt_pizza")
    worker = Worker()
    assert render(h, worker) is True
    (req,) = worker.renders
    assert req.phrases == memes.scene_spec("burnt_pizza")[0] and req.negative == memes.SCENE_NEGATIVE
    prompt = " ".join(t for t, _ in (*req.phrases, *req.negative)).lower()
    assert not any(n.lower() in prompt for n in names()) and "inspect" not in prompt
    assert req.identity is None and req.nsfw_max == SELFIES.nsfw_max  # a plain worker, the NSFW gate
    (row,) = h.store.conn.execute("SELECT * FROM discord_meme").fetchall()
    assert row["status"] == "ready" and row["path"] == f"discord/memes/{row['id']}.jpg"
    assert worker.captions[0][1:3] == ("when Belra inspects", "the floor again") and worker.stopped == 1
    assert (tmp_path / row["path"]).is_file() and (tmp_path / f"discord/memes/{row['id']}.base.jpg").is_file()


@pytest.mark.parametrize("case", ["render_flagged", "caption_flagged", "screened", "joke_gone", "snap_gone"])
def test_flagged_or_screened_meme_is_never_ready(h, tmp_path, case):
    ident = joke(h)
    if case == "screened":
        mid = planned(h, ident, top="Quillon again")
    elif case == "snap_gone":
        mid = planned(h, ident, base="snap", snap=None, scene=None)
    else:
        mid = planned(h, ident)
    if case == "joke_gone":
        h.store.conn.execute("UPDATE lexicon SET retired=? WHERE id=?", (h.clock.now(), ident))
    h.store.conn.commit()
    worker = Worker(flag_render=case == "render_flagged", flag_caption=case == "caption_flagged")
    render(h, worker)
    row = h.store.conn.execute("SELECT status,path FROM discord_meme WHERE id=?", (mid,)).fetchone()
    assert tuple(row) == ("refused", None)
    assert not (tmp_path / f"discord/memes/{mid}.jpg").exists()
    if case == "render_flagged":
        assert len(worker.renders) == 3 and worker.captions == []  # three seeds, then refused
    if case == "caption_flagged":  # the scene stays on disk: nothing is ever deleted
        assert (tmp_path / f"discord/memes/{mid}.base.jpg").is_file()
    if case in ("screened", "joke_gone", "snap_gone"):
        assert worker.renders == [] and worker.captions == [] and not worker.running
    assert memes.offer(SimpleNamespace(store=h.store, data_dir=tmp_path, clock=h.clock), mid) is None


def posted_snap(h, tmp_path, channel, *, guid=102, line=4321, sent=3.0):
    """Her situational photo as the photo paths post it: a `snap` attachment on her own message in
    `channel`, the snap marked sent on 'discord' (what photos/requests.py does for any layout
    channel an ask came from, officers-only ones included)."""
    from selfies.fakes import ready_snap
    from leeroy_mind.discord import store as dstore
    from leeroy_mind.selfies.snaps import mark_sent
    sid = ready_snap(h.store, tmp_path, guid, audience="anyone",
                     scene={"shows": "an invented kitchen", "audience": "anyone"})
    with h.store.transaction():
        mid = dstore.post(h.store, channel=channel, author=guid, origin="mind", text="here you go", now=sent,
                          attachment=dstore.AttachmentIn("snap", f"selfies/{guid}/snaps/{sid}.jpg", 832, 1216, 100, sid))
    assert mark_sent(h.store, sid, sender=guid, channel="discord", line_id=line, now=sent)
    return sid, mid


def personas_of(h):
    from leeroy_mind.personas.io import load_persona
    return {102: load_persona(h.store, 102)}


def test_her_own_posted_photo_is_captioned_without_a_render(h, tmp_path):
    ident = joke(h)
    sid, _ = posted_snap(h, tmp_path, "pics")
    assert memes.own_snap(h.store.conn, tmp_path, 102, nsfw_max=0.2, personas=personas_of(h)) == sid
    mid = planned(h, ident, base="snap", snap=sid, scene=None)
    newer, _ = posted_snap(h, tmp_path, "food", line=4322, sent=4.0)  # a newer public photo of hers
    worker = Worker()
    render(h, worker)  # still eligible: the newer one does not refuse it
    assert worker.renders == [] and len(worker.captions) == 1
    assert h.store.conn.execute("SELECT status FROM discord_meme WHERE id=?", (mid,)).fetchone()[0] == "ready"
    assert memes.own_snap(h.store.conn, tmp_path, 102, nsfw_max=0.2, personas=personas_of(h)) == newer
    # a photo sent only to him never becomes a public meme
    h.store.conn.execute("UPDATE selfie_snap SET audience='him',channel='dm' WHERE id IN (?,?)", (sid, newer))
    assert memes.own_snap(h.store.conn, tmp_path, 102, nsfw_max=0.2, personas=personas_of(h)) is None


@pytest.mark.parametrize("where", ["officer-chat", "applications", "bank-requests", "dm:102", "deleted", "unposted"])
def test_an_officers_only_or_private_photo_never_bases_a_public_meme(h, tmp_path, where):
    """Review P1-1: a request photo posted only in an officers' channel carries the same flags as a
    #pics photo (audience anyone, sent on discord); only its actual posting tells them apart."""
    ident = joke(h)
    sid, message = posted_snap(h, tmp_path, "pics" if where in ("deleted", "unposted") else where)
    if where == "deleted":
        h.store.conn.execute("UPDATE discord_message SET deleted=5.0 WHERE id=?", (message,))
    if where == "unposted":
        h.store.conn.execute("DELETE FROM discord_attachment WHERE message=?", (message,))
    assert memes.own_snap(h.store.conn, tmp_path, 102, nsfw_max=0.2, personas=personas_of(h)) is None
    assert not memes.snap_eligible(h.store.conn, tmp_path, 102, sid, nsfw_max=0.2, personas=personas_of(h))
    mid = planned(h, ident, base="snap", snap=sid, scene=None)  # one planned before is refused at render
    worker = Worker()
    render(h, worker)
    assert worker.captions == []
    assert h.store.conn.execute("SELECT status FROM discord_meme WHERE id=?", (mid,)).fetchone()[0] == "refused"


def test_the_caption_characters_are_the_workers(h):
    """The mind's caption alphabet is the worker's (selfie/leeroy_selfie/protocol.py, read by path:
    the mind never imports that package)."""
    import importlib.util
    path = Path(memes.__file__).parents[3] / "selfie" / "leeroy_selfie" / "protocol.py"
    spec = importlib.util.spec_from_file_location("selfie_protocol_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.CAPTION_CHARS == memes.CAPTION_CHARS and module.MAX_CAPTION == memes.MAX_CAPTION


def test_the_meme_stages_are_in_the_night_budget(h):
    from leeroy_mind.nightly import budget
    ident = joke(h)
    memes.make_plan_stage(app_for(per_week=1), SELFIES)
    memes.make_render_stage(app_for(), SELFIES, worker=lambda: Worker())
    assert {"meme_plan", "meme_render"} <= budget.EXTENSION_STAGES
    ctx = h.ctx()
    assert budget._counts(ctx, set(), frozenset()).keys().isdisjoint({"meme_plan", "meme_render"})  # off: none
    counts = budget._counts(ctx, set(), frozenset({"meme_plan", "meme_render"}))
    assert (counts["meme_plan"], counts["meme_render"]) == (1, 0)
    planned(h, ident)
    counts = budget._counts(ctx, set(), frozenset({"meme_plan", "meme_render"}))
    assert (counts["meme_plan"], counts["meme_render"]) == (0, 1)  # capped at one a week; one to render


def test_no_meme_left_takes_no_gpu(h):
    joke(h)
    worker = Worker()
    assert render(h, worker) is False and worker.renders == [] and worker.stopped == 0


def recorded(app, message):
    """The chat line the pipeline recorded for a Discord message (its discord:line receipt)."""
    line = app.store.get_meta(f"discord:line:{message}")
    return app.store.conn.execute("SELECT text FROM chat_line WHERE id=?", (int(line),)).fetchone()[0]


def test_meme_image_posts_as_an_attachment(tmp_path):
    """The opener's MemeOffer through the real talk pipeline: one message carrying a `meme`
    attachment, the meme posted in the message's own transaction, and its caption a note for the
    thread that follows."""
    from photos.test_pipeline_photo import finish
    from selfies.fakes import mind_app
    from leeroy_mind.photos.notes import by_messages
    from leeroy_mind.talk.pipeline import TurnMeta
    from leeroy_mind.talk.turns import Turn, TurnKind
    from leeroy_mind.llm.scheduler import Priority

    async def go():
        app = mind_app(tmp_path, toml="[server]\nenabled=true\n[culture]\nenabled=true\nmemes=true\n")
        try:
            with app.store.transaction():
                app.store.conn.execute("INSERT INTO lexicon(id,kind,term,origin_ref,originator,born,last_used,butt) "
                                       "VALUES (5,'joke','floor inspector','chat:1',101,0,0,102)")
                mid = app.store.conn.execute(
                    "INSERT INTO discord_meme(night,joke,base,scene,top,bottom,status,path,nsfw,created) "
                    "VALUES ('2026-10-29',5,'scene','tipped_chair','when the floor inspector','strikes again',"
                    "'ready',?,0.03,?)", ("discord/memes/1.jpg", app.clock.now())).lastrowid
                app.store.set_meta(memes.SIZE_KEY.format(id=mid), "1024x1024")
            (tmp_path / "discord/memes").mkdir(parents=True)
            (tmp_path / "discord/memes/1.jpg").write_bytes(JPEG)
            app.llm._script = lambda *a: 'made this one at lunch'
            turn = Turn(TurnKind.OPEN, 101, 'discord:memes', instruction='Post your meme.',
                        priority=Priority.BACKGROUND, max_lines=1)
            meta = TurnMeta('', why='server:meme_image', single_line=True, keep_channel=True)
            meta.photo, meta.photo_asked = memes.offer(app, mid), True
            lines = await finish(app, asyncio.create_task(app.engine.speak_meta(turn, meta)))
            assert len(lines) == 1
            row = app.store.conn.execute(
                "SELECT m.id,m.channel,m.author,m.text,a.kind,a.path,a.snap,a.width,d.status,d.posted_message "
                "FROM discord_message m JOIN discord_attachment a ON a.message=m.id "
                "JOIN discord_meme d ON d.posted_message=m.id").fetchone()
            assert (row["channel"], row["author"], row["kind"], row["path"], row["snap"], row["width"],
                    row["status"]) == ("memes", 101, "meme", "discord/memes/1.jpg", None, 1024, "posted")
            # her line as the pipeline delivered it (her typing style may add a typo), posted with the
            # meme and recorded as her chat line
            assert row["text"] == lines[0] == recorded(app, row["id"])
            assert 'strikes again' in str(app.llm.calls)  # the caption was in her instruction
            assert by_messages(app.store.conn, [row["id"]], memes=True) == {
                row["id"]: "[meme picture captioned: when the floor inspector / strikes again]"}
            assert by_messages(app.store.conn, [row["id"]]) == {}  # memes off: no note
            assert memes.offer(app, mid) is None  # posted once
        finally:
            await app.stop()
    run(go())


# -- text memes --------------------------------------------------------------------------------

def text_seeds(h, now):
    from leeroy_mind.culture.seeds import meme_seeds
    ctx = h.ctx()
    return meme_seeds(h.store, now=now, local=h.clock.local(), personas={g: ctx.personas.get(g) for g in ctx.members()},
                      human=1)


def test_text_meme_seeds_ride_the_jokes_and_the_last_raid(h):
    ident = joke(h)
    now = h.T + 4 * 86400  # past the joke's own three-day rest
    rid = h.store.conn.execute(
        "INSERT INTO guild_raid(started,ended,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid,kills,wipes,"
        "outcome) VALUES (?,?,'naxx',25,0,0,101,101,102,2,4,'cleared')", (now - 9000, now - 3600)).lastrowid
    seeds = text_seeds(h, now)
    assert [(s.key, s.kind, s.channels, s.about, s.weight) for s in seeds] == [
        (f"meme:joke:{ident}", "meme", ("memes",), (102, 101), 0.5),
        (f"meme:raid:{rid}", "meme", ("memes",), (), 0.5)]
    assert "floor inspector" in seeds[0].fact and seeds[1].fact == "Last raid: Naxxramas 25, 4 wipes and 2 kills"
    from leeroy_mind.culture.seeds import joke_of
    assert [joke_of(s) for s in seeds] == [ident, None]


def test_a_private_origin_never_becomes_a_meme_and_groups_never_take_one(h):
    from leeroy_mind.groups.conversations import PUBLIC_KINDS
    ident = joke(h)
    line = h.store.conn.execute("SELECT origin_ref FROM lexicon WHERE id=?", (ident,)).fetchone()[0]
    h.store.conn.execute("UPDATE chat_line SET channel='whisper',to_guid=102 WHERE id=?", (int(line[5:]),))
    assert text_seeds(h, h.T + 4 * 86400) == []
    assert not {"meme", "meme_image"} & PUBLIC_KINDS  # never in a group DM


def test_gather_without_memes_never_reads_them(h, monkeypatch):
    from leeroy_mind.culture import seeds as culture_seeds
    from leeroy_mind.server import seeds as server_seeds
    for name in ("meme_seeds", "meme_image_seeds"):
        monkeypatch.setattr(culture_seeds, name, lambda *a, **k: pytest.fail("memes read while off"))
    ctx = h.ctx()
    out = server_seeds.gather(h.store.conn, now=h.T, local=h.clock.local(),
                              cast={g: ctx.personas.get(g) for g in ctx.members()}, officers=frozenset(), human=1,
                              human_name='', trial_rank=4, culture_enabled=True, culture_store=h.store)
    assert not any(s.kind.startswith("meme") for s in out)


def test_a_text_meme_teases_kindly_and_spends_its_joke_on_delivery(tmp_path, monkeypatch):
    from server.test_engine import finished
    from culture.test_culture_seeds import joke_app
    from leeroy_mind.culture import seeds as culture_seeds
    from leeroy_mind.culture.tease import TEASED_KEY
    from leeroy_mind.server.seeds import Seed
    from selfies.fakes import mind_app
    import random
    from leeroy_mind.server import engine as se, runtime
    from leeroy_mind.discord import store as dstore
    from leeroy_mind.server import records

    app = mind_app(tmp_path, toml='[server]\nenabled=true\n[guards]\nenabled=true\n')
    try:
        app.server.lease._held = True
        app.engine._login_at = float('-inf')
        app.world.human_online = False
        app.server.engine.rng = random.Random(19)
        app.server.engine.rng.random = lambda: .5
        observed = run(runtime.snapshot(app, app.clock.now()))

        async def snapshot(*a, **k):
            await asyncio.sleep(0)
            return observed
        monkeypatch.setattr(runtime, 'snapshot', snapshot)
        monkeypatch.setattr(se.rhythm, 'length', lambda rng: 3)
        monkeypatch.setattr(se, 'answer_delay', lambda state, rng: 0)
        app.observed, app.turns = observed, []

        async def speak(turn, meta):
            app.turns.append((turn, meta))
            meta.turn_id = 'test-' + str(len(app.turns))
            with app.store.transaction():
                message = dstore.post(app.store, channel=turn.channel.removeprefix('discord:'), author=turn.speaker,
                                      origin='mind', text='An invented meme ' + str(len(app.turns)), turn=meta.turn_id,
                                      reply_to=meta.discord.reply_to, now=app.clock.now())
                records.posted(app.store, app.clock.local().date().isoformat(), app.clock.now(),
                               conversation=meta.server_conversation)
                if meta.on_discord_record:
                    meta.on_discord_record(message, message, app.clock.now())
            return ['An invented meme']
        monkeypatch.setattr(app.engine, 'speak_meta', speak)
        joke_seed = joke_app(app, monkeypatch)
        meme = Seed('meme:joke:1', 'meme', ('memes',), joke_seed.fact, joke_seed.about)
        app.observed.seeds = [meme]
        app.culture_config = replace(app.culture_config, memes=True)
        async def go():
            await app.server.engine.tick(app.clock.now())
            await finished(app.server.engine)
        run(go())
        first = app.turns[0][0].instruction
        assert 'text meme or a two-line copypasta' in first and 'keep it kind.' in first
        assert app.store.get_meta(TEASED_KEY.format(date=app.clock.local().date(), guid=102))
        assert app.store.conn.execute('SELECT uses_total FROM lexicon WHERE id=1').fetchone()[0] == 1
        assert app.store.get_meta('server:seed:meme:joke:1') is not None
        assert app.store.get_meta('culture:wear-counted:joke:1:1') == '1'  # nightly spread counts it once
    finally:
        run(app.stop())
