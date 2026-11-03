"""Small application surface for Realm service tests; no runtime is started."""

from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

from leeroy_mind.bridge import TransportState
from leeroy_mind.config import load_config
from leeroy_mind.protocol import Ack, event_type


class RealmApp:
    def __init__(self, root, store, clock):
        self.data_dir, self.store, self.clock = root, store, clock
        self.cfg = load_config(root / "mind.toml")
        self.human_guid = 1
        self._hello_ts = None
        self._transport = TransportState(0, False)
        self.skip_event_rows = {"training_test"}
        self.handlers, self.before = defaultdict(list), defaultdict(list)
        self.background, self.drains, self.receipts = [], [], defaultdict(list)
        self.connection_hooks = []
        self.world = SimpleNamespace(human_online=False)
        self.personas = {}
        self.training = None
        self.sent = []
        self.bridge = self
        self.engine = SimpleNamespace(speak=AsyncMock(return_value=[]),
                                      say_as=AsyncMock(return_value=True))

    @property
    def transport_state(self):
        return self._transport

    def delivery_epoch(self, value):
        # This small fake explicitly supplies native delivery provenance; real
        # Bridge tests exercise exact-object and queued-transport association.
        return self._transport.epoch or None

    @property
    def confirmed_human_guid(self):
        return self.human_guid

    def is_backlog(self, ev):
        return (self._hello_ts is not None and event_type(ev) not in {"hello", "cast"}
                and ev.ts < self._hello_ts - 5000)

    async def send(self, cmd, *, preserve_id=False):
        assert preserve_id is True
        self.sent.append(cmd)
        return Ack(0, cmd.id, True, "")

    def register_event_handler(self, name, handler, *, before=False):
        (self.before if before else self.handlers)[name].append(handler)

    def register_connection_hook(self, hook):
        self.connection_hooks.append(hook)

    async def connection(self, up):
        self._transport = TransportState(self._transport.epoch + int(up), up)
        for hook in self.connection_hooks:
            await hook(up)

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
        # Simulate the ordinary app presence row between before/after hooks.
        if name == "hello":
            self._hello_ts = ev.ts
            if ev.human_guid:
                self.human_guid = ev.human_guid
            self.world.human_online = bool(ev.human_guid)
        elif name in {"login", "logout"} and ev.human:
            self.human_guid = ev.guid
            self.world.human_online = name == "login"
        for hook in self.handlers[name]:
            await hook(ev)


def enabled(root, value):
    (root / "mind.toml").write_text(
        "[realm]\nenabled = " + str(value).lower() + "\n", encoding="utf-8")
