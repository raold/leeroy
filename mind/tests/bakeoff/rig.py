"""Invented data for the bake-off tests: whisper conversations between him (guid 1, Testmage)
and the test cast, every member line a captured turn, a FakeLLM whose candidates answer
deterministically and whose judge calls a speaker AI only for one candidate's tell, and a fake
Ollama admin that tracks what is loaded. Nothing here is real guild data."""
import hashlib
import json
import re
from pathlib import Path

from conftest import TEST_CAST, seed_test_cast
from leeroy_mind.config import load_config
from leeroy_mind.protocol import Chat, Member
from leeroy_mind.talk.prompt import COMMS_RULES
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.turing import judge

HIS = ["u on?", "ok sure", "lol yeah", "wanna run icc", "Arkon you there", "nah im good", "gg",
       "brb"]
MEMBER = ["That's lovely, I'm glad you asked.", "I'd love to, honestly.",
          "Oh, absolutely! Count me in.", "I'm here, what's up?"]
SYSTEM = ("You are Belra, an invented test member.\n\nIn your guild: Zan, the guild master.\n"
          + COMMS_RULES.format(human="Zan") + "\n- You don't flirt with anyone in the guild.")
OPTIONS = {"temperature": 0.9, "min_p": 0.05, "top_p": 1.0, "num_predict": 80, "num_ctx": 4096,
           "stop": ["\n\n"]}
ANSWERS = {"polite:1": "Certainly! I would be delighted to help you with that today.",
           "terse:1": "ok sure lol",
           "deep:1": "sure thing, see you at 8\n\nShe smiles and waits for his answer."}
HINT = "Answer Zan's whisper in a line, in your own voice."
RETRY_EVERY = 4  # every 4th captured turn was asked twice live: a silence, then the hinted retry
CONFIG = """
[[candidate]]
name = "polite"
model = "polite:1"
think = false

[[candidate]]
name = "terse"
model = "terse:1"
think = false
prompt_variant = "situation"

[[candidate]]
name = "terse-think"
model = "terse:1"
think = true
think_budget = 100

[[candidate]]
name = "deep"
model = "deep:1"

[[candidate]]
name = "absent"
model = "missing:1"
"""


def capture(store, *, line_id, ts, guid, text, channel="whisper", discord_id=None, words=0,
            options=OPTIONS, first_reply=None):
    """A captured turn through capture.record; with `first_reply`, the live pipeline first got
    that (a silence) and sent its hinted retry."""
    from leeroy_mind.bakeoff import capture as cap
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": "Chat:\nZan (whisper to you): hi\n\nAnswer Zan."}]

    def req(reply, msgs, index):
        r = cap.request("live-model", msgs, options, False, reply, max_lines=1, words=words,
                        two_lines=False)
        r["index"] = index
        return r
    if first_reply is None:
        sent, first, calls = req(text, messages, 0), None, 1
    else:
        hinted = [messages[0], {**messages[1], "content": messages[1]["content"] + "\n" + HINT}]
        first, sent, calls = req(first_reply, messages, 0), req(text, hinted, 1), 2
    cap.record(store.conn, sent, first, calls=calls, ts=ts, line_id=line_id, discord_id=discord_id,
               guid=guid, channel=channel, kind="reply_human")


def seed(store, now, *, members=(101, 102, 103, 104, 105), per=8) -> Path:
    """His whispers with each member, answered every time; the data dir."""
    seed_test_cast(store)
    store.upsert_cast([Member(guid=1, name="Testmage", race=1, class_=8, gender=0, level=80,
                              rank=0, online=False, map=0, zone=10, spec_tab=-1)], 1000.0)
    store.set_meta("human_guid", "1")
    t = now - 3 * 86400
    with store.transaction():
        for m in members:
            for i in range(per):
                t += 40
                store.insert_chat_line(Chat(0, "whisper", 1, "Testmage", m, HIS[i % len(HIS)], 0, 0),
                                       session_id=None, origin="human", ts=t)
                t += 20
                text = MEMBER[(i + m) % len(MEMBER)]
                lid = store.insert_chat_line(Chat(0, "whisper", m, TEST_CAST[m][0], 1, text, 0, 0),
                                             session_id=None, origin="mind", ts=t)
                retried = i % RETRY_EVERY == RETRY_EVERY - 1
                capture(store, line_id=lid, ts=t, guid=m, text=text,
                        first_reply="(nothing)" if retried else None)
            t += 3600
    root = Path(store.conn.execute("PRAGMA database_list").fetchone()[2]).parent
    (root / "mind.toml").write_text("")
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return root


def seed_discord(store, now, *, channel: str, members, per: int, start_days: float = 2.0) -> None:
    """His posts and the members' answers in a Discord channel ("general") or group DM
    ("gdm:<id>"), each member post a captured turn of one message (a 90-word band)."""
    from leeroy_mind.discord import store as discord
    talk = channel if channel.startswith("gdm:") else "discord:" + channel
    t = now - start_days * 86400
    for m in members:
        for i in range(per):
            t += 40
            discord.post(store, channel=channel, author=1, origin="human", text=HIS[i % len(HIS)],
                         now=t)
            t += 20
            text = MEMBER[(i + m) % len(MEMBER)]
            mid = discord.post(store, channel=channel, author=m, origin="mind", text=text, now=t)
            lid = store.insert_chat_line(Chat(0, talk, m, TEST_CAST[m][0], 0, text, 0, 0),
                                         session_id=None, origin="mind", ts=t)
            capture(store, line_id=lid, ts=t, guid=m, text=text, channel=talk, discord_id=mid,
                    words=90, options=dict(OPTIONS, num_predict=168))
        t += 3600
    store.conn.commit()
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


_ROW = re.compile(r"^\[\d\d:\d\d\] ([A-Z]): (.*)$")


def judge_reply(messages) -> str:
    said: dict[str, list[str]] = {}
    for line in messages[-1]["content"].split("\n"):
        m = _ROW.match(line)
        if m:
            said.setdefault(m[1], []).append(m[2])
    out = []
    for letter, lines in said.items():
        bot = any("certainly" in x.casefold() for x in lines)
        out.append({"speaker": letter, "human": not bot,
                    "tells": [{"code": "polish", "phrase": "too tidy by half"}] if bot else []})
    return json.dumps({"speakers": out})


class BakeLLM(FakeLLM):
    """The candidates' fixed answers, the judge's verdicts, and what Ollama holds (`resident`)."""

    def __init__(self, resident: set, *, models, fail_model=None, fail_text="fake timed out"):
        super().__init__(self.answer, models=models)
        self.resident = resident
        self.fail_model = fail_model
        self.fail_text = fail_text

    def answer(self, model, messages):
        if messages[0]["content"] == judge.SYSTEM:
            return judge_reply(messages)
        from leeroy_mind.talk import beat as beats
        if beats.MARK in messages[-1]["content"]:  # a beat candidate's beat
            return json.dumps({"read": "he asks if she is around", "lands": "fine",
                               "want": "react", "use": None})
        if model == "terse:1":  # types as he does
            self.terse = getattr(self, "terse", -1) + 1
            return HIS[self.terse % len(HIS)]
        return ANSWERS[model]

    async def _chat(self, model, messages, **kw):
        self.resident.add(model)
        if model == self.fail_model:
            self.calls.append({"model": model, "messages": messages, "options": kw["options"],
                               "think": kw.get("think")})
            from leeroy_mind.llm.client import LLMError
            raise LLMError(self.fail_text)
        return await super()._chat(model, messages, **kw)


class FakeAdmin:
    def __init__(self, resident: set):
        self.resident = resident
        self.unloaded: list[str] = []

    async def loaded_models(self):
        return sorted(self.resident)

    async def unload(self, model):
        self.unloaded.append(model)
        self.resident.discard(model)


def ollama(root: Path, *, loaded=(), pulled=None, fail_model=None, fail_text="fake timed out"):
    cfg = load_config(root / "mind.toml")
    resident = set(loaded) | {cfg.models.embedding}
    pulled = pulled if pulled is not None else {"polite:1", "terse:1", "deep:1",
                                                 cfg.models.reflection, cfg.models.embedding,
                                                 cfg.models.live}
    llm = BakeLLM(resident, models=pulled, fail_model=fail_model, fail_text=fail_text)
    admin = FakeAdmin(resident)
    return llm, admin, cfg


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
