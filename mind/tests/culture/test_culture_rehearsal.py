"""Culture Part C, end to end through a real MindApp on a scratch data dir: FakeLLM, the fake
selfie worker as a real subprocess speaking the caption protocol, no bridge, no Ollama, no GPU.
A public running joke gets a meme at night (planned by the reflection model's caption, rendered
from a scene with no people, captioned by the worker); every server bot posts once from the
guild's rows and the signup post takes the members' role reactions; the meme goes out in #memes
as a member's message carrying it, through the real talk pipeline; no game command is sent and no
file is deleted. The guard review is stubbed to send (the guards have their own tests). Every
name here is invented."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

from conftest import TEST_CAST, run
from nightly.conftest import FakeAdmin, generic, scripted
from nightly.test_night_lexicon import term
from selfies.fakes import CLOCK, fake_command, mind_app
from leeroy_mind.culture import memes
from leeroy_mind.culture.bots import BOTS, SINCE_KEY
from leeroy_mind.discord.presence import Presence
from leeroy_mind.guards.types import Verdict
from leeroy_mind.selfies import config as selfies_config
from leeroy_mind.server import engine as server_engine, rhythm, state
from leeroy_mind.storage.rows import Relationship

TOML = ("[server]\nenabled=true\n[guards]\nenabled=true\n[selfies]\nenabled=true\n"
        "[culture]\nenabled=true\nbots=true\nemoji=true\nmemes=true\nmemes_per_week=3\n")
CAPTION = {"top": "when the floor inspector", "bottom": "finds the floor again", "picture": "tipped_chair"}
LINE = "made this one on my lunch break"


def files(root):
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def seed_rows(h):
    """The guild's own rows the bots post from, and a public running joke whose butt (Belra) the
    others like."""
    ident = term(h)
    h.clock.set(CLOCK.timestamp())  # the app's own clock from here on
    store, now = h.store, h.clock.now()
    store.conn.execute("UPDATE lexicon SET butt=102 WHERE id=?", (ident,))
    for g in (101, 103, 104, 105):
        store.put_relationship(Relationship(g, 102, affinity=60))
    store.set_meta(SINCE_KEY, str(now - 86400))
    store.set_meta("guild:loot:7:abc", json.dumps(dict(state="charged", item_id=123, name="Invented Blade",
        award=dict(guid=101, paid=47.0, bid=47.0, priority=0, runner_up=None, runner_up_bid=None, close=False),
        human_claimed=False, bidders=[])))
    store.conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,raid_id,item_id,note) "
                       "VALUES (?,101,-47,'item',7,123,'guild:loot:7:abc')", (now - 900,))
    store.conn.execute("INSERT INTO guild_raid(started,ended,raid_key,size,heroic,offline,lead_guid,tank_guid,"
                       "loot_guid,kills,wipes,outcome) VALUES (?,?,'naxx',25,0,0,101,101,102,15,2,'cleared')",
                       (now - 9000, now - 1800))
    for kind, at, event, title in (("raid", now + 2 * 86400, 501, "Raid night"),
                                   ("raid", now + 1800, 502, "Raid night")):
        store.conn.execute("INSERT INTO realm_calendar(kind,creator,at,title,ref,updated,event_id,state) "
                           "VALUES (?,101,?,?,?,?,?,'posted')", (kind, at, title, f"raid:{event}", now - 86400, event))
    store.conn.executemany("INSERT INTO realm_rsvp(event,guid,status,ts) VALUES (501,?,?,?)",
                           [(101, "signed_up", now), (103, "tentative", now)])
    from culture.test_bots import announcement
    announcement(h)
    store.conn.commit()
    return ident


def test_culture_part_c_rehearsal(h, tmp_path, monkeypatch):
    log = tmp_path / "worker.log"
    monkeypatch.setattr(selfies_config, "worker_command",
                        lambda cfg, identity=False, embedding=False: fake_command(tmp_path)[0])
    monkeypatch.setattr(selfies_config, "worker_env",
                        lambda cfg: {**fake_command(tmp_path)[1], "FAKE_SELFIE_LOG": str(log)})
    for mod in (state, server_engine):  # the invented roster is online whatever its schedule
        monkeypatch.setattr(mod, "presence_of", lambda *a, **k: Presence("online", False, 0))
    ident = seed_rows(h)
    before = files(tmp_path)
    out = {}

    async def go():
        app = mind_app(tmp_path, toml=TOML)
        app.arbiter.admin = FakeAdmin([app.cfg.models.embedding])
        said = []
        app.engine.send = lambda cmd: said.append(cmd)  # nothing may reach the game
        try:
            # -- wiring: the meme stages sit where the night's models are --------------------------
            names = [s.name for s in app.night_runner.stages]
            assert names.index("meme_plan") == names.index("language") + 1
            assert names.index("meme_render") == names.index("selfies") + 1
            render_stage = next(s for s in app.night_runner.stages if s.name == "meme_render")
            assert render_stage.model == "image" and render_stage.wants_gpu is memes.wants_gpu

            # -- the night: a caption from the reflection model, a render and a caption ---------
            app.llm._script = scripted({"culture_meme": [CAPTION]}, default=generic)
            ctx = app.night_runner.make_ctx("2026-10-29", (app.clock.now() - 3600, app.clock.now() + 3600))
            for stage in app.night_runner.stages:
                if stage.name in ("meme_plan", "meme_render"):
                    await stage.fn(ctx)
            (meme,) = [dict(r) for r in app.store.conn.execute("SELECT * FROM discord_meme")]
            assert (meme["joke"], meme["status"], meme["scene"]) == (ident, "ready", "tipped_chair")
            assert (tmp_path / meme["path"]).is_file()
            worker_log = log.read_text().split()
            assert "caption" in worker_log  # the real worker client spoke the caption op

            # -- the bots: each posts once from rows, the signup post takes role reactions -------
            bots = app.server.extras.bots
            await bots.tick(app.clock.now())
            await bots.tick(app.clock.now())  # a second pass posts nothing again
            posted = [dict(r) for r in app.store.conn.execute(
                "SELECT id,bot,channel,text FROM discord_message WHERE origin='bot' ORDER BY id")]
            # two raid nights ahead: two signup posts; no card birthday falls today
            assert sorted(p["bot"] for p in posted) == sorted([*(b for b in BOTS if b != "birthday"), "signups"])
            signup = next(p for p in posted if p["bot"] == "signups" and "Saturday" in p["text"])
            assert signup["text"].endswith("React :tank: :healer: :dps:")
            assert sorted(tuple(r) for r in app.store.conn.execute(
                "SELECT member,emoji FROM discord_reaction WHERE message=?", (signup["id"],))) == [
                (101, ":tank:"), (103, "🤷")]

            # -- posting: a member carries the meme into #memes through the real pipeline -------
            async def send_verdict(turn, meta, text):
                return Verdict("send")
            monkeypatch.setattr(app.engine.live_review, "check", send_verdict)
            app.server.lease._held = True
            app.engine._login_at = float("-inf")
            app.world.human_online = False
            monkeypatch.setattr(rhythm, "length", lambda rng: 1)

            def choose(seeds, **kw):
                (seed,) = [s for s in seeds if s.kind == "meme_image"]
                out["seed"] = seed
                return seed, "memes"
            monkeypatch.setattr(rhythm, "choose_seed_and_channel", choose)
            app.llm._script = lambda model, messages: (
                json.dumps(generic(messages[-1]["content"])) if "matching this schema:" in messages[-1]["content"]
                else LINE)
            await app.server.engine.tick(app.clock.now())
            for _ in range(400):
                if not app.server.engine.live():
                    break
                await asyncio.sleep(0.002)
                await app.clock.advance(2, 2)
            assert not app.server.engine.live()
            row = app.store.conn.execute(
                "SELECT m.id,m.author,m.channel,m.text,a.kind,d.status FROM discord_message m "
                "JOIN discord_attachment a ON a.message=m.id JOIN discord_meme d ON d.posted_message=m.id").fetchone()
            assert row is not None, json.loads(app.store.get_meta("server:recent") or "[]")
            assert (row["channel"], row["kind"], row["status"]) == ("memes", "meme", "posted")
            assert row["author"] in TEST_CAST and row["author"] != 102 and row["text"].startswith(LINE[:20])
            # spent once, on delivery: its joke's use and the butt's teasing allowance
            assert app.store.conn.execute("SELECT uses_total FROM lexicon WHERE id=?", (ident,)).fetchone()[0] == 1
            assert app.store.get_meta(f"server:seed:{out['seed'].key}") is not None
            assert said == []  # no Say, no game command
        finally:
            await app.stop()
    run(go())
    assert before <= files(tmp_path)  # nothing on disk was deleted
