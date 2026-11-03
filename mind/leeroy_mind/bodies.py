"""Scheduling reservations for bodies; never proof of native authority or cleanup.

Evidence refreshes are synchronous and atomic across the registered sources. A
failed refresh keeps the last good evidence and defers effective handbacks until
evidence is healthy again. Notifications use a committed-state baseline and a
FIFO pump: reentrant callbacks can mutate the ledger, but cannot interrupt source
inspection or cause the same effective release to be delivered twice. Decisions
are computed only after notification settlement; callback-driven evidence churn
is re-read up to eight times, then denied rather than exposing a stale result.
"""

from collections import deque
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
import logging
import math
from typing import Protocol


log = logging.getLogger(__name__)


class _AllBodies:
    __slots__ = ()

    def __repr__(self) -> str:
        return "ALL"


ALL = _AllBodies()
RANKS = {
    "raid": 0,
    "offline_raid": 1,
    "training": 2,
    "benchmark": 2,
    "realm_event": 3,
    "realm_day": 4,
}


class BodyEvidenceUnavailable(RuntimeError):
    """The ledger cannot safely describe current ownership evidence."""


def _identity(value: object) -> str:
    if type(value) is not str or not value or "\0" in value:
        raise ValueError("invalid body identity")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ValueError("invalid body identity") from None
    if size > 256:
        raise ValueError("invalid body identity")
    return value


def _uint(value: object, bits: int) -> int:
    if type(value) is not int or not 0 < value < 1 << bits:
        raise ValueError("invalid body integer")
    return value


def _members(value: Iterable[int], *, nonempty: bool = False) -> frozenset[int]:
    try:
        copied = frozenset(_uint(guid, 32) for guid in value)
    except (TypeError, ValueError):
        raise ValueError("invalid body membership") from None
    if nonempty and not copied:
        raise ValueError("invalid body membership")
    return copied


def _rank(value: object, owner: str | None = None) -> int:
    if type(value) is str and value in RANKS:
        value = RANKS[value]
    if type(value) is not int or not 0 <= value <= 4:
        raise ValueError("invalid body rank")
    if owner in RANKS and value != RANKS[owner]:
        raise ValueError("inconsistent body rank")
    return value


def _seconds(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError("invalid body time")
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise ValueError("invalid body time") from None
    if not math.isfinite(result):
        raise ValueError("invalid body time")
    return result


@dataclass(frozen=True)
class BodyHold:
    scope_id: str
    owner: str
    rank: int
    guids: frozenset[int] | _AllBodies
    retained: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_id", _identity(self.scope_id))
        object.__setattr__(self, "owner", _identity(self.owner))
        object.__setattr__(self, "rank", _rank(self.rank, self.owner))
        if self.guids is not ALL:
            object.__setattr__(self, "guids", _members(self.guids))
        if type(self.retained) is not bool:
            raise ValueError("invalid retained flag")


@dataclass(frozen=True)
class RunBodyProof:
    run: object
    session: object
    owner: str
    scope_id: str
    session_key: str
    connection_epoch: int
    instance_id: int
    members: frozenset[int]

    def __post_init__(self) -> None:
        if self.run is None or self.session is None:
            raise ValueError("invalid body run proof")
        object.__setattr__(self, "owner", _identity(self.owner))
        object.__setattr__(self, "scope_id", _identity(self.scope_id))
        object.__setattr__(self, "session_key", _identity(self.session_key))
        object.__setattr__(self, "connection_epoch", _uint(self.connection_epoch, 64))
        object.__setattr__(self, "instance_id", _uint(self.instance_id, 32))
        object.__setattr__(self, "members", _members(self.members, nonempty=True))


class _Clock(Protocol):
    def now(self) -> float: ...


@dataclass(frozen=True)
class _Claim:
    rank: int
    until: float


@dataclass
class _Source:
    snapshot: Callable[[], tuple[BodyHold, ...]]
    rows: tuple[BodyHold, ...] = ()


@dataclass(frozen=True)
class _State:
    rows: tuple[tuple[str, BodyHold], ...]
    claims: dict[str, dict[int, _Claim]]
    observed: frozenset[int]


class BodyLedger:
    def __init__(
        self,
        clock: _Clock,
        *,
        run_proof: Callable[[object], RunBodyProof | None] | None = None,
    ):
        if run_proof is not None and not callable(run_proof):
            raise ValueError("invalid body proof provider")
        self.clock = clock
        self._run_proof = run_proof
        self._sources: dict[str, _Source] = {}
        self._claims: dict[str, dict[int, _Claim]] = {}
        self._observed: set[int] = set()
        self._healthy = True
        self._depth = 0
        self._reading_evidence = False
        self._evidence_reentered = False
        self._notifying = False
        self._callback_generation = 0
        self._notifications: deque[tuple[Callable, tuple]] = deque()
        self._preempt: dict[str, list[Callable[[set[int]], None]]] = {}
        self._release: list[Callable[[str, set[int]], None]] = []
        self._pending_preempt: dict[str, set[int]] = {}
        self._reported = self._state()
        self._last_poll: float | None = None

    def add_holder(
        self, name: str, rank: int | str, held: Callable[[], Iterable[int] | _AllBodies]
    ) -> None:
        name = _identity(name)
        normalized = _rank(rank, name)
        if not callable(held):
            raise ValueError("invalid body source")

        def snapshot() -> tuple[BodyHold, ...]:
            return (BodyHold("fixed", name, normalized, held(), False),)

        self.add_holder_source(name, snapshot)

    def add_holder_source(
        self, name: str, snapshot: Callable[[], tuple[BodyHold, ...]]
    ) -> None:
        self._check_reentry()
        name = _identity(name)
        if name in self._sources or not callable(snapshot):
            raise ValueError("invalid body source")
        self._sources[name] = _Source(snapshot)

    def claim(
        self, owner: str, guids: Iterable[int], *, rank: int | str, until: float
    ) -> set[int]:
        owner = _identity(owner)
        normalized = _rank(rank, owner)
        selected = _members(guids)
        deadline = _seconds(until)
        callbacks = self._callback_generation
        granted = set()
        with self._operation(refresh=True):
            now = _seconds(self.clock.now())
            if deadline <= now:
                raise ValueError("body claim must end in the future")
            self._observed.update(selected)
            ranks = self._owner_ranks(self._state()).get(owner, set())
            if self._healthy and (not ranks or ranks == {normalized}):
                for guid in sorted(selected):
                    if not self._grantable(owner, normalized, guid):
                        continue
                    for other, claims in tuple(self._claims.items()):
                        prior = claims.get(guid)
                        if other != owner and prior is not None and prior.rank > normalized:
                            del claims[guid]
                            self._pending_preempt.setdefault(other, set()).add(guid)
                            if not claims:
                                del self._claims[other]
                    self._claims.setdefault(owner, {})[guid] = _Claim(normalized, deadline)
                    granted.add(guid)
        if callbacks != self._callback_generation and not self._settle(refresh=True):
            return set()
        if not self._healthy:
            return set()
        current = self._claims.get(owner, {})
        return {
            guid for guid in granted
            if guid in current and current[guid].rank == normalized
            and self._grantable(owner, normalized, guid)
        }

    def release(self, owner: str, guids: Iterable[int] | None = None) -> set[int]:
        owner = _identity(owner)
        selected = None if guids is None else _members(guids)
        callbacks = self._callback_generation
        with self._operation():
            if selected is not None:
                self._observed.update(selected)
            claims = self._claims.get(owner, {})
            removed = set(claims) if selected is None else set(claims) & selected
            for guid in removed:
                del claims[guid]
            if not claims:
                self._claims.pop(owner, None)
        if callbacks != self._callback_generation:
            self._settle()
        # This is a mutation receipt, not a promise that callbacks did not reclaim.
        return removed

    def claims_for(self, owner: str) -> frozenset[int]:
        owner = _identity(owner)
        self._settle()
        return frozenset(self._claims.get(owner, {}))

    def holder(self, guid: int) -> str | None:
        guid = _uint(guid, 32)
        self._settle(refresh=True, observed=(guid,))
        self._require_evidence()
        holds = self._holders(self._state(), guid)
        return min(holds, key=lambda row: row[0])[1] if holds else None

    def others(self, guid: int, *, besides: set[str]) -> set[str]:
        guid = _uint(guid, 32)
        excluded = {_identity(owner) for owner in besides}
        self._settle(refresh=True, observed=(guid,))
        self._require_evidence()
        return {owner for _, owner in self._holders(self._state(), guid)} - excluded

    def may_move(self, owner: str, guid: int) -> bool:
        owner = _identity(owner)
        guid = _uint(guid, 32)
        self._settle(refresh=True, observed=(guid,))
        if not self._healthy:
            return False
        state = self._state()
        if any(row.retained and self._covers(row, guid) for _, row in state.rows):
            return False
        holds = self._holders(state, guid)
        if not holds:
            return True
        priority = min(rank for rank, _ in holds)
        return {held_by for rank, held_by in holds if rank == priority} == {owner}

    def may_move_all(
        self, owner: str, guids: Iterable[int], *, rank: int | str | None = None
    ) -> bool:
        """Check an immutable batch and optional expected rank in one settled view."""
        owner = _identity(owner)
        selected = _members(guids)
        expected = None if rank is None else _rank(rank)
        self._settle(refresh=True, observed=selected)
        if not self._healthy:
            return False
        state = self._state()
        if expected is not None:
            ranks = self._owner_ranks(state).get(owner, set())
            current = RANKS.get(owner)
            if current is None and len(ranks) == 1:
                current = next(iter(ranks))
            if current != expected:
                return False
        for guid in selected:
            if any(row.retained and self._covers(row, guid) for _, row in state.rows):
                return False
            holds = self._holders(state, guid)
            if holds:
                priority = min(held_rank for held_rank, _ in holds)
                if {held_by for held_rank, held_by in holds if held_rank == priority} != {owner}:
                    return False
        # No refresh, notification finalization, or callback follows this result.
        return True

    def rank_for(self, owner: str) -> int | None:
        owner = _identity(owner)
        self._settle(refresh=True)
        if not self._healthy:
            return None
        if owner in RANKS:
            return RANKS[owner]
        ranks = self._owner_ranks(self._state()).get(owner, set())
        return next(iter(ranks)) if len(ranks) == 1 else None

    def owner_for_run(self, run: object) -> str | None:
        self._settle(refresh=True)
        if not self._healthy or self._run_proof is None:
            return None
        self._reading_evidence = True
        self._evidence_reentered = False
        try:
            supplied = self._run_proof(run)
            if self._evidence_reentered or type(supplied) is not RunBodyProof:
                return None
            proof = RunBodyProof(
                supplied.run, supplied.session, supplied.owner, supplied.scope_id,
                supplied.session_key, supplied.connection_epoch, supplied.instance_id,
                supplied.members,
            )
        except Exception:
            log.warning("body_run_proof_unavailable")
            return None
        finally:
            self._reading_evidence = False
        if proof.run is not run or proof.owner not in {"raid", "offline_raid"}:
            return None
        state = self._state()
        matching = [
            (source, row) for source, row in state.rows
            if row.owner == proof.owner and row.scope_id == proof.scope_id
        ]
        if len(matching) != 1:
            return None
        operation, reservation = matching[0]
        if reservation.guids is ALL or not proof.members <= reservation.guids:
            return None
        # Every valid proof member is already observed through its finite source.
        # No finalization (and therefore no callbacks) follows this decision.
        for guid in proof.members:
            if any(
                row.retained and self._covers(row, guid)
                and (source != operation or row.scope_id != proof.scope_id
                     or row.owner != proof.owner)
                for source, row in state.rows
            ):
                return None
            holds = self._holders(state, guid)
            priority = min(rank for rank, _ in holds)
            if {owner for rank, owner in holds if rank == priority} != {proof.owner}:
                return None
        return proof.owner

    def on_preempt(self, owner: str, fn: Callable[[set[int]], None]) -> None:
        self._check_reentry()
        owner = _identity(owner)
        if not callable(fn):
            raise ValueError("invalid body callback")
        self._preempt.setdefault(owner, []).append(fn)

    def on_release(self, fn: Callable[[str, set[int]], None]) -> None:
        self._check_reentry()
        if not callable(fn):
            raise ValueError("invalid body callback")
        self._release.append(fn)

    def poll(self) -> None:
        self._check_reentry()
        now = _seconds(self.clock.now())
        if self._last_poll is not None and 0 <= now - self._last_poll < 1.0:
            return
        self._last_poll = now
        with self._operation(refresh=True):
            pass

    def _check_reentry(self) -> None:
        if self._reading_evidence:
            self._evidence_reentered = True
            raise BodyEvidenceUnavailable("body evidence unavailable")

    def _settle(self, *, refresh: bool = False, observed: Iterable[int] = ()) -> bool:
        """Finish callbacks before reading a result, then refresh their side effects.

        Inside a listener the FIFO pump is paused until that listener returns.
        Reentrant calls therefore finalize their own state without recursively
        running later listeners; their results reflect their immediate return
        boundary. The enclosing outer call settles all queued work before its
        own result is computed.
        """
        for _ in range(8):
            callbacks = self._callback_generation
            with self._operation(refresh=refresh):
                self._observed.update(observed)
            if self._notifying or callbacks == self._callback_generation:
                return True
        self._healthy = False
        log.warning("body_evidence_unstable")
        return False

    @contextmanager
    def _operation(self, *, refresh: bool = False):
        self._check_reentry()
        self._depth += 1
        try:
            self._expire(_seconds(self.clock.now()))
            if refresh:
                self._refresh()
            yield
        finally:
            self._depth -= 1
            if self._depth == 0:
                self._finish()

    def _expire(self, now: float) -> None:
        for owner, claims in tuple(self._claims.items()):
            for guid, claim in tuple(claims.items()):
                if claim.until <= now:
                    del claims[guid]
            if not claims:
                del self._claims[owner]

    def _refresh(self) -> None:
        replacements: dict[str, tuple[BodyHold, ...]] = {}
        self._reading_evidence = True
        self._evidence_reentered = False
        try:
            for name, source in self._sources.items():
                snapshot = source.snapshot()
                if type(snapshot) is not tuple:
                    raise ValueError("invalid body snapshot")
                scopes = set()
                rows = []
                for supplied in snapshot:
                    if type(supplied) is not BodyHold:
                        raise ValueError("invalid body snapshot")
                    row = BodyHold(
                        supplied.scope_id, supplied.owner, supplied.rank,
                        supplied.guids, supplied.retained,
                    )
                    if row.scope_id in scopes:
                        raise ValueError("duplicate body scope")
                    scopes.add(row.scope_id)
                    rows.append(row)
                replacements[name] = tuple(rows)
            if self._evidence_reentered:
                raise ValueError("reentrant body snapshot")
            proposed = _State(
                tuple((name, row) for name, rows in replacements.items() for row in rows),
                self._claims,
                frozenset(self._observed),
            )
            if any(len(ranks) != 1 for ranks in self._owner_ranks(proposed).values()):
                raise ValueError("conflicting body rank")
        except Exception:
            self._healthy = False
            log.warning("body_evidence_unavailable")
            return
        finally:
            self._reading_evidence = False
        for name, rows in replacements.items():
            self._sources[name].rows = rows
            for row in rows:
                if row.guids is not ALL:
                    self._observed.update(row.guids)
        self._healthy = True
        rows = self._state().rows
        for owner, claims in tuple(self._claims.items()):
            for guid, claim in tuple(claims.items()):
                if any(
                    row.owner != owner and row.rank < claim.rank and self._covers(row, guid)
                    for _, row in rows
                ):
                    del claims[guid]
                    self._pending_preempt.setdefault(owner, set()).add(guid)
            if not claims:
                del self._claims[owner]

    def _state(self) -> _State:
        return _State(
            tuple((name, row) for name, source in self._sources.items() for row in source.rows),
            {owner: dict(claims) for owner, claims in self._claims.items()},
            frozenset(self._observed),
        )

    @staticmethod
    def _covers(row: BodyHold, guid: int) -> bool:
        return row.guids is ALL or guid in row.guids

    @classmethod
    def _holders(cls, state: _State, guid: int) -> list[tuple[int, str]]:
        # Stable source/row order, then claims, supplies provider-before-claim ties.
        holds = [(row.rank, row.owner) for _, row in state.rows if cls._covers(row, guid)]
        holds.extend(
            (claims[guid].rank, owner) for owner, claims in state.claims.items() if guid in claims
        )
        return holds

    def _grantable(self, owner: str, rank: int, guid: int) -> bool:
        state = self._state()
        return not any(
            row.retained and row.owner != owner and self._covers(row, guid)
            for _, row in state.rows
        ) and not any(
            other != owner and priority <= rank
            for priority, other in self._holders(state, guid)
        )

    @staticmethod
    def _owner_ranks(state: _State) -> dict[str, set[int]]:
        ranks: dict[str, set[int]] = {}
        for _, row in state.rows:
            ranks.setdefault(row.owner, set()).add(row.rank)
        for owner, claims in state.claims.items():
            ranks.setdefault(owner, set()).update(claim.rank for claim in claims.values())
        return ranks

    def _owner_members(self, state: _State) -> dict[str, set[int]]:
        members: dict[str, set[int]] = {}
        for _, row in state.rows:
            members.setdefault(row.owner, set()).update(
                state.observed if row.guids is ALL else row.guids
            )
        for owner, claims in state.claims.items():
            members.setdefault(owner, set()).update(claims)
        return members

    def _blocked(self, state: _State, guid: int) -> set[str]:
        holds = self._holders(state, guid)
        if not holds:
            return set()
        priority = min(rank for rank, _ in holds)
        winners = {owner for rank, owner in holds if rank == priority}
        return {owner for rank, owner in holds if rank > priority and owner not in winners}

    def _finish(self) -> None:
        if self._healthy:
            current = self._state()
            before = self._owner_members(self._reported)
            after = self._owner_members(current)
            preempted = self._pending_preempt
            self._pending_preempt = {}
            for guid in self._observed:
                for owner in self._blocked(current, guid) - self._blocked(self._reported, guid):
                    preempted.setdefault(owner, set()).add(guid)
            # Commit the baseline before invoking any callback, including reentrant ones.
            self._reported = current
            for owner in sorted(preempted):
                for callback in tuple(self._preempt.get(owner, ())):
                    self._notifications.append((callback, (set(preempted[owner]),)))
            for owner in sorted(before):
                lost = before[owner] - after.get(owner, set())
                if lost:
                    for callback in tuple(self._release):
                        self._notifications.append((callback, (owner, set(lost))))
        if self._notifying:
            return
        self._notifying = True
        try:
            while self._notifications:
                callback, args = self._notifications.popleft()
                self._callback_generation += 1
                try:
                    callback(*args)
                except Exception:
                    log.warning("body_callback_failed")
        finally:
            self._notifying = False

    def _require_evidence(self) -> None:
        if not self._healthy:
            raise BodyEvidenceUnavailable("body evidence unavailable")
