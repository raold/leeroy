"""Asyncio client for the mod-leeroy bridge (NDJSON over TCP)."""

import asyncio
import dataclasses
import logging
import random
from typing import Awaitable, Callable

from .clock import Clock, SystemClock, now_ms
from .ingress import WireOrigin
from .protocol import (MAX_LINE_BYTES, Ack, Cast, Command, Error, Event, GetCast, Hello, Ping,
                       Pong, ProtocolError, encode_command, parse_event_with_origin)

log = logging.getLogger(__name__)

# The event queue warns when it reaches the high-water mark, again each time it doubles, and
# re-arms once the dispatcher has brought it back down to the low-water mark.
QUEUE_HIGH_WATER = 1000
QUEUE_LOW_WATER = 500
ANOTHER_MIND = "another mind is connected"  # mod-leeroy's refusal (LeeroyProtocol.h Err)
MISSED_PONGS = 2  # pings without a pong, in a row, before the connection is dropped


class BridgeError(Exception):
    pass


class NotConnected(BridgeError):
    pass


class AckTimeout(BridgeError):
    pass


class ConnectionLost(BridgeError):
    pass


class Backoff:
    def __init__(self, initial: float = 0.5, maximum: float = 30.0, jitter: float = 0.2):
        self._initial = initial
        self._maximum = maximum
        self._jitter = jitter
        self._current = initial

    def next(self) -> float:
        base = self._current
        self._current = min(self._current * 2, self._maximum)
        if self._jitter:
            base *= 1 + random.uniform(-self._jitter, self._jitter)
        return min(base, self._maximum)

    def reset(self) -> None:
        self._current = self._initial


@dataclasses.dataclass(frozen=True, slots=True)
class TransportState:
    """Synchronous local writer generation; queued callbacks cannot prolong it."""
    epoch: int
    connected: bool


@dataclasses.dataclass(frozen=True, slots=True)
class _DeliveryMetadata:
    epoch: int | None
    origin: WireOrigin | None = None
    task: asyncio.Task | None = None


class BridgeClient:
    DRAIN_S = 1.0  # close(): how long queued events and the last on_connection(False) may take
    STOP_S = 5.0  # then how long a cancelled handler may take to give up
    # A mind refused because another holds the bridge (one with another data dir: the lock
    # cannot see it) waits this long between tries instead of the reconnect backoff.
    REFUSED_RETRY_S = 300.0

    def __init__(self, host: str, port: int, on_event: Callable[[Event], Awaitable[None]], *,
                 ack_timeout: float = 5.0, ping_interval: float = 30.0,
                 backoff: Backoff | None = None, clock: Clock | None = None,
                 on_connection: Callable[[bool], Awaitable[None]] | None = None):
        self._host = host
        self._port = port
        self._on_event = on_event
        self._on_connection = on_connection
        self._ack_timeout = ack_timeout
        self._ping_interval = ping_interval
        self._backoff = backoff or Backoff()
        self._clock = clock or SystemClock()
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._transport_epoch = 0
        self._delivery = None
        self._connected_event = asyncio.Event()
        self._closing = False
        self._run_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._cast_ids: list[str] = []
        self._next_id = 1
        self._queue: asyncio.Queue = asyncio.Queue()
        self._warn_at = QUEUE_HIGH_WATER
        self._greeted = False  # this connection has had its hello
        self._greeted_event = asyncio.Event()
        self._refused = False  # this connection was refused: another mind holds the bridge
        self._refusal_told = False
        # hello's human_guid as sent (0 while the human is offline); the app keeps the last non-zero.
        self.human_guid = 0

    @property
    def connected(self) -> bool:
        """True while the TCP connection is open (not necessarily greeted)."""
        return self._connected

    @property
    def transport_state(self) -> TransportState:
        """Read the actual writer fence synchronously, outside dispatcher order."""
        writer = self._writer
        return TransportState(self._transport_epoch, bool(
            self._connected and not self._closing and writer is not None and not writer.is_closing()))

    def delivery_epoch(self, value: Event | bool) -> int | None:
        """Epoch of this exact currently delivered native event/connection hook.

        Injected events and calls outside their dispatcher delivery have no
        transport provenance. A delayed delivery keeps its original epoch even
        if the reader already installed a replacement writer.
        """
        item = self._delivery
        if (item is None or item[1] is not value
                or item[0] != ("conn" if type(value) is bool else "event")):
            return None
        return item[2].epoch

    def delivery_origin(self, value: Event) -> WireOrigin | None:
        """Origin of the exact event, only inside its active dispatcher task."""
        item = self._delivery
        if item is None or item[0] != "event" or item[1] is not value:
            return None
        try:
            task = asyncio.current_task()
        except RuntimeError:  # a synchronous caller outside an event loop
            return None
        metadata = item[2]
        return metadata.origin if task is not None and metadata.task is task else None

    async def wait_connected(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._connected_event.wait(), timeout)

    @property
    def greeted(self) -> bool:
        """True while the connection is open and has had its hello: the mind is served."""
        return self._connected and self._greeted

    async def wait_greeted(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._greeted_event.wait(), timeout)

    @property
    def refused(self) -> bool:
        """The last connection was refused because another mind holds the bridge."""
        return self._refused

    @property
    def queued(self) -> int:
        """Events and connection changes waiting for the dispatcher."""
        return self._queue.qsize()

    async def run(self) -> None:
        self._run_task = asyncio.current_task()
        dispatcher = asyncio.create_task(self._dispatch())
        try:
            while not self._closing:
                try:
                    reader, writer = await asyncio.open_connection(
                        self._host, self._port, limit=MAX_LINE_BYTES + 1)
                except OSError as exc:
                    log.info("bridge_connect_failed: %s", exc.__class__.__name__)
                else:
                    await self._session(reader, writer)
                if self._closing:
                    break
                if self._refused:
                    if not self._refusal_told:
                        self._refusal_told = True
                        log.warning("bridge_refused: another mind holds the bridge; trying "
                                    "again every %d s", self.REFUSED_RETRY_S)
                    await self._clock.sleep(self.REFUSED_RETRY_S)
                    continue
                await self._clock.sleep(self._backoff.next())
        except asyncio.CancelledError:
            if not self._closing:
                raise
            task = asyncio.current_task()
            if task is not None and task.uncancel() > 0:
                raise
        finally:
            # One dispatcher serves every connection, so a slow or stuck handler never delays
            # a reconnect. It is stopped here: gracefully (queued items first, bounded) on
            # close(), at once otherwise. A cancel that interrupts the drain still stops it.
            try:
                if self._closing:
                    self._queue.put_nowait(None)
                    await asyncio.wait({dispatcher}, timeout=self.DRAIN_S)
            finally:
                await self._stop_dispatcher(dispatcher)

    async def _stop_dispatcher(self, dispatcher: asyncio.Task) -> None:
        """Cancel the dispatcher and wait until it has ended. The sentinel goes in first, so a
        handler that swallows the cancel still lets it finish. A cancel of this task meanwhile
        is passed on once the dispatcher is gone; a handler that will not stop at all is given
        up on after STOP_S, so close() always returns."""
        if dispatcher.done():
            return
        self._queue.put_nowait(None)
        dispatcher.cancel()
        loop = asyncio.get_running_loop()
        end = loop.time() + self.STOP_S
        interrupted = False
        while not dispatcher.done():
            left = end - loop.time()
            if left <= 0:
                log.error("bridge_dispatcher_stuck: a handler ignored the cancel")
                break
            try:
                await asyncio.wait({dispatcher}, timeout=left)
            except asyncio.CancelledError:
                interrupted = True
        if interrupted:
            raise asyncio.CancelledError

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._transport_epoch += 1
        transport_epoch = self._transport_epoch
        self._greeted = self._refused = False
        self._connected = True
        self._connected_event.set()
        ping_task: asyncio.Task | None = None
        try:
            self._enqueue(("conn", True, transport_epoch))
            ping_task = asyncio.create_task(self._ping_loop())
            await self._read_loop(reader, transport_epoch=transport_epoch)
        finally:
            writer.close()
            self._connected = False
            self._connected_event.clear()
            self._greeted_event.clear()
            self._writer = None
            self._fail_pending()
            self._enqueue(("conn", False, transport_epoch))
            if ping_task is not None:
                ping_task.cancel()
                await asyncio.gather(ping_task, return_exceptions=True)
            try:
                await asyncio.wait_for(writer.wait_closed(), 1.0)
            except (OSError, asyncio.TimeoutError):
                pass

    def inject(self, ev: Event) -> None:
        """Put an event on the dispatcher's queue, behind those already there: an extension's
        own events (his transcribed lines) are handled in order with the bridge's."""
        self._enqueue(("event", ev, None))

    def _enqueue(self, item) -> None:
        self._queue.put_nowait(item)
        size = self._queue.qsize()
        if size >= self._warn_at:
            log.warning("bridge_event_queue_backlog: size=%d", size)
            self._warn_at = 2 * size
        elif size <= QUEUE_LOW_WATER:
            self._warn_at = QUEUE_HIGH_WATER

    async def _dispatch(self) -> None:
        """Hand events and connection changes to the app, in order, one at a time."""
        try:
            while (item := await self._queue.get()) is not None:
                kind, value = item[:2]
                payload = item[2] if len(item) == 3 else None
                metadata = (payload if isinstance(payload, _DeliveryMetadata)
                            else _DeliveryMetadata(payload))
                self._delivery = (kind, value, dataclasses.replace(
                    metadata, task=asyncio.current_task()))
                try:
                    if kind == "conn":
                        if self._on_connection is not None:
                            await self._on_connection(value)
                    else:
                        await self._on_event(value)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling() > 0:
                        raise
                    log.warning("bridge_handler_cancelled")
                except Exception:
                    log.exception("bridge_handler_failed")
                finally:
                    self._delivery = None
        except asyncio.CancelledError:
            raise
        except BaseException:
            log.exception("bridge_dispatcher_died")
            raise

    async def _read_loop(self, reader: asyncio.StreamReader, *, transport_epoch: int | None = None) -> None:
        discard = False
        while True:
            try:
                line = await reader.readuntil(b"\n")
            except asyncio.LimitOverrunError as exc:
                log.warning("bridge_line_too_long")
                await reader.readexactly(exc.consumed)
                discard = True
                continue
            except (asyncio.IncompleteReadError, OSError):
                return
            if discard:
                discard = False
                continue
            line = line.strip()
            if not line:
                continue
            try:
                ev, origin = parse_event_with_origin(
                    line, received_at=self._clock.now(), transport_epoch=transport_epoch)
            except ProtocolError as exc:
                log.warning("bridge_line_rejected: %s", exc)
                continue
            if self._handle(ev):
                self._enqueue(("event", ev, _DeliveryMetadata(transport_epoch, origin)))

    def _handle(self, ev: Event) -> bool:
        """Resolve pending sends. Returns True when the event goes on to on_event."""
        if isinstance(ev, Ack):
            self._resolve(ev.id, ev)
            return False
        if isinstance(ev, Pong):
            self._resolve(ev.id, Ack(ev.ts, ev.id, True, ""))
            return False
        if isinstance(ev, Error) and ev.id in self._pending:
            self._resolve(ev.id, Ack(ev.ts, ev.id, False, ev.message))
            return False
        if isinstance(ev, Error) and not self._greeted and ev.message == ANOTHER_MIND:
            self._refused = True
        if isinstance(ev, Hello):
            self.human_guid = ev.human_guid
            self._greeted = True
            self._greeted_event.set()
            self._refusal_told = False
            self._backoff.reset()
        elif isinstance(ev, Cast):
            ids, self._cast_ids = self._cast_ids, []
            for cid in ids:
                self._resolve(cid, Ack(ev.ts, cid, True, ""))
        return True

    def _resolve(self, cmd_id: str, ack: Ack) -> None:
        fut = self._pending.pop(cmd_id, None)
        if fut is not None and not fut.done():
            fut.set_result(ack)

    def _fail_pending(self) -> None:
        pending, self._pending = self._pending, {}
        self._cast_ids = []
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(ConnectionLost("bridge connection lost"))

    async def _ping_loop(self) -> None:
        """A ping every ping_interval. mod-leeroy answers on its world thread, so one slow
        world update can miss a pong: the connection is dropped only after MISSED_PONGS in a
        row (a dead socket ends the read loop anyway)."""
        missed = 0
        while True:
            await self._clock.sleep(self._ping_interval)
            try:
                await self.send(Ping(id=""))
                missed = 0
            except AckTimeout:
                missed += 1
                log.warning("bridge_ping_timeout: missed=%d", missed)
                if missed >= MISSED_PONGS:
                    if self._writer is not None:
                        self._writer.close()
                    return
            except BridgeError:
                return

    async def send(self, cmd: Command, *, preserve_id: bool = False) -> Ack:
        """Send a command and wait for its ack (real-time timeout).

        Acks, pongs and the cast that answers get_cast resolve in the reader, while events go to
        on_event through a queue. Events keep their order among themselves, but not relative to
        acks: a send can return before on_event has run for events that arrived earlier.
        """
        writer = self._writer
        if not self._connected or writer is None:
            raise NotConnected("bridge is not connected")
        # Guild jobs persist their UUID before dispatch and receive progress long
        # after the ack. Rewriting it would sever both replay and ownership proof.
        issued = getattr(self, "_preserved_ids", None)
        if issued is None:
            issued = self._preserved_ids = set()
        if preserve_id:
            cmd_id = cmd.id
            if (not isinstance(cmd_id, str) or not cmd_id or len(cmd_id) > 128
                    or not cmd_id.isascii() or not all(c.isalnum() or c in "_-" for c in cmd_id)):
                raise ValueError("invalid preserved command identity")
            old_auto = cmd_id.startswith("m") and cmd_id[1:].isdigit() and int(cmd_id[1:]) < self._next_id
            if cmd_id in issued or cmd_id in self._pending or old_auto:
                raise ValueError("command identity already issued")
            if len(issued) >= 65536:
                raise ValueError("preserved command identity capacity reached")
            issued.add(cmd_id)
        else:
            cmd_id = f"m{self._next_id}"
            self._next_id += 1
            while cmd_id in issued or cmd_id in self._pending:
                cmd_id = f"m{self._next_id}"
                self._next_id += 1
        line = encode_command(dataclasses.replace(cmd, id=cmd_id), now_ms(self._clock))
        fut = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = fut
        if isinstance(cmd, GetCast):
            self._cast_ids.append(cmd_id)
        try:
            try:
                writer.write(line)
                await writer.drain()
            except OSError as exc:
                raise ConnectionLost(exc.__class__.__name__) from None
            try:
                return await asyncio.wait_for(fut, self._ack_timeout)
            except asyncio.TimeoutError:
                raise AckTimeout(f"no ack for {cmd_id}") from None
        finally:
            self._pending.pop(cmd_id, None)
            if cmd_id in self._cast_ids:
                self._cast_ids.remove(cmd_id)

    async def close(self) -> None:
        """Stop run(): queued events are handed over first (within DRAIN_S). Idempotent: a
        second call while the first is draining only waits for it, and never cancels the
        drain. Never await it from a handler."""
        already = self._closing
        self._closing = True
        task = self._run_task
        if task is None or task.done() or task is asyncio.current_task():
            return
        if not already:
            task.cancel()
        await asyncio.wait({task})
