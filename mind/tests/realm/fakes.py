"""Small application surface for Realm service tests; no runtime is started."""

from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

from leeroy_mind.config import load_config
from leeroy_mind.protocol import Ack, event_type


class RealmApp:
    def __init__(self, root, store, clock):
        self.data_dir, self.store, self.clock = root, store, clock
        self.cfg = load_config(root / "mind.toml")
        self.human_guid = 1
        self.skip_event_rows = {"training_test"}
        self.handlers, self.before = defaultdict(list), defaultdict(list)
        self.background, self.drains, self.receipts = [], [], defaultdict(list)
        self.sent = []
        self.bridge = SimpleNamespace(send=self.send)
        self.engine = SimpleNamespace(speak=AsyncMock(return_value=[]),
                                      say_as=AsyncMock(return_value=True))

    async def send(self, cmd, *, preserve_id=False):
        assert preserve_id is True
        self.sent.append(cmd)
        return Ack(0, cmd.id, True, "")

    def register_event_handler(self, name, handler, *, before=False):
        (self.before if before else self.handlers)[name].append(handler)

    def register_background(self, factory):
        self.background.append(factory)

    def register_shutdown_drain(self, hook):
        self.drains.append(hook)

    def register_shutdown_receipt(self, name, handler):
        self.receipts[name].append(handler)

    async def event(self, ev):
        name = event_type(ev)
        for hook in self.before[name]:
            await hook(ev)
        for hook in self.handlers[name]:
            await hook(ev)


def enabled(root, value):
    (root / "mind.toml").write_text(
        "[realm]\nenabled = " + str(value).lower() + "\n", encoding="utf-8")
