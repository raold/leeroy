"""One application-owned read thread; the scheduler and all asyncio state stay on its loop.

MAX_PENDING bounds logical requests (including embedding waits) and physical work together.
Cancellation returns promptly, but a queued/running job keeps its slot until the owner thread
has discarded/finished it. There is no waiting room when admission is full. Construction is
inert; aclose joins finite work and closes the private reader before dependencies may close.
"""
import asyncio
import copy
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypeVar

from leeroy_mind.clock import SystemClock
from leeroy_mind.library.chunks import Chunk
from leeroy_mind.library.core import DEFAULT_EMBED_MODEL, Library, _query_vector
from leeroy_mind.llm.scheduler import Priority

MAX_PENDING = 8  # active + queued + embedding, not just queue length
T = TypeVar("T")


class SearchBusy(Exception):
    """The bounded reader has no admission slot; do not fall back inline."""


class SearchClosed(Exception):
    """The reader is closing, closed, or failed to open."""


@dataclass(eq=False)
class _Request:
    cancelled: threading.Event = field(default_factory=threading.Event)
    pending: bool = False
    finished: bool = False
    future: asyncio.Future | None = None
    embedding: asyncio.Task | None = None


@dataclass
class _Job:
    request: _Request
    future: asyncio.Future
    operation: Callable
    args: tuple
    kwargs: dict


def _kinds(kinds):
    return kinds if kinds is None or isinstance(kinds, str) else tuple(kinds)


def _format(reader: Library, function: Callable[..., T], args: tuple, kwargs: dict) -> T:
    return function(*args, **kwargs)


class LibrarySearchWorker:
    def __init__(self, db_path: Path, *, embed_model: str = DEFAULT_EMBED_MODEL,
                 max_pending: int = MAX_PENDING):
        if type(max_pending) is not int or max_pending <= 0:
            raise ValueError("max_pending must be a positive integer")
        self._path, self._model = Path(db_path), embed_model
        self._limit = max_pending
        self._queue: queue.Queue[_Job | None] = queue.Queue(maxsize=max_pending)
        self._requests: set[_Request] = set()  # loop-owned, never inspected by the worker
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._closed = False
        self._failure: BaseException | None = None
        self._stopped: asyncio.Future | None = None
        self._close_task: asyncio.Task | None = None

    def _bind(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("library search worker belongs to another event loop")
        return loop

    def _admit(self) -> _Request:
        loop = self._bind()
        if self._closed:
            raise SearchClosed() from self._failure
        if len(self._requests) >= self._limit:
            raise SearchBusy()
        if self._thread is None:
            self._stopped = loop.create_future()
            self._thread = threading.Thread(target=self._work, name="mind-library-search")
            try:
                self._thread.start()
            except BaseException:
                self._thread = None
                self._closed = True
                raise
        request = _Request()
        self._requests.add(request)
        return request

    def _release(self, request: _Request) -> None:
        if request.finished and not request.pending:
            self._requests.discard(request)

    async def _submit(self, request: _Request, operation: Callable[..., T],
                      *args, **kwargs) -> T:
        if self._closed:
            raise SearchClosed() from self._failure
        if request.cancelled.is_set():
            raise asyncio.CancelledError()
        future = self._loop.create_future()
        request.future, request.pending = future, True
        # Each admitted request submits at most one physical job at a time.
        self._queue.put_nowait(_Job(request, future, operation, args, kwargs))
        try:
            return await future
        except asyncio.CancelledError:
            request.cancelled.set()
            raise

    async def _call(self, operation: Callable[..., T], *args, **kwargs) -> T:
        request = self._admit()
        try:
            return await self._submit(request, operation, *args, **kwargs)
        finally:
            request.finished = True
            self._release(request)

    async def search(self, query: str, k: int = 5, kinds=None, *, llm=None,
                     priority: Priority = Priority.SOCIAL) -> list[Chunk]:
        request = self._admit()
        try:
            plan = await self._submit(request, Library._prepare_search, query, k,
                                      _kinds(kinds), embed_available=llm is not None)
            # Close may run after prepare completes but before this waiter resumes.
            # Do not start embedding after close has captured its dependency drain.
            if self._closed:
                raise SearchClosed() from self._failure
            if request.cancelled.is_set():
                raise asyncio.CancelledError()
            qvec = None
            if plan.embed:
                # No reader/transaction or physical job is held across this loop await.
                request.embedding = asyncio.create_task(
                    _query_vector(llm, self._model, plan.text, priority))
                try:
                    qvec = await request.embedding
                except asyncio.CancelledError:
                    if self._closed and not asyncio.current_task().cancelling():
                        raise SearchClosed() from None
                    raise
                finally:
                    request.embedding = None
            return await self._submit(request, Library._finish_search, plan,
                                      tuple(qvec) if qvec is not None else None)
        except asyncio.CancelledError:
            request.cancelled.set()
            raise
        finally:
            request.finished = True
            self._release(request)

    async def search_text(self, query: str, k: int = 5, kinds=None) -> list[Chunk]:
        return await self._call(Library.search_text, query, k, _kinds(kinds))

    async def reference_matches(self, query: str, kinds=None, k: int = 8) -> list[Chunk]:
        return await self._call(Library.reference_matches, query, _kinds(kinds), k)

    async def search_reference_text(self, query: str, k: int = 32, kinds=None, *,
                                    language: str | None = None) -> list[Chunk]:
        return await self._call(Library.search_reference_text, query, k, _kinds(kinds),
                                language=language)

    async def titled(self, names, kinds=None, *, limit: int = 60) -> list[Chunk]:
        return await self._call(Library.titled, tuple(names), _kinds(kinds), limit=limit)

    async def _format(self, function: Callable[..., T], *args, **kwargs) -> T:
        """Private talk seam: trusted pure module functions only, on copied value inputs.

        Never pass a bound method, application callback, connection, scheduler or store.
        Chunk.metadata is mutable, so even frozen chunks need an owned snapshot here.
        """
        request = self._admit()
        try:
            args, kwargs = copy.deepcopy((args, kwargs))
            return await self._submit(request, _format, function, args, kwargs)
        finally:
            request.finished = True
            self._release(request)

    def _complete(self, job: _Job, result=None, error: BaseException | None = None) -> None:
        """Only the owning event loop touches futures or logical admission state."""
        request = job.request
        if request.future is not job.future:
            return
        request.pending = False
        if not job.future.done():
            if request.cancelled.is_set() or self._closed:
                job.future.set_exception(SearchClosed())
            elif error is not None:
                job.future.set_exception(error)
            else:
                job.future.set_result(result)
        self._release(request)

    def _terminated(self, failure: BaseException | None, close_error: BaseException | None):
        if failure is not None:
            self._failure, self._closed = failure, True
        # Also covers opening failure before the first submit was enqueued, or an error
        # during operation cleanup. The thread has stopped, so no physical work remains.
        for request in tuple(self._requests):
            request.pending = False
            if failure is not None:
                request.cancelled.set()
                if request.embedding is not None:
                    request.embedding.cancel()
            if request.future is not None and not request.future.done():
                request.future.set_exception(failure or SearchClosed())
            self._release(request)
        while not self._queue.empty():
            self._queue.get_nowait()
        self._stopped.set_result(close_error)

    def _work(self) -> None:
        reader = None
        failure = close_error = None
        try:
            reader = Library(self._path, clock=SystemClock(), llm=None,
                             embed_model=self._model, readonly=True)
            while not self._stopping.is_set():
                job = self._queue.get()
                if job is None:
                    break
                result = error = None
                try:
                    reader._search_cancelled = job.request.cancelled.is_set
                    reader.conn.set_progress_handler(
                        lambda: int(job.request.cancelled.is_set()), 1000)
                    reader._check_search_cancelled()
                    result = job.operation(reader, *job.args, **job.kwargs)
                    reader._check_search_cancelled()
                except BaseException as exc:
                    error = exc
                finally:
                    reader.conn.set_progress_handler(None, 0)
                    reader._search_cancelled = None
                    if error is not None or job.request.cancelled.is_set():
                        reader.conn.rollback()
                    self._loop.call_soon_threadsafe(self._complete, job, result, error)
        except BaseException as exc:
            failure = exc
        finally:
            # No connection ever crosses the thread boundary, including teardown.
            if reader is not None:
                try:
                    reader.close()
                except BaseException as exc:
                    close_error = exc
            while True:
                try:
                    job = self._queue.get_nowait()
                except queue.Empty:
                    break
                if job is not None:
                    self._loop.call_soon_threadsafe(self._complete, job, None,
                                                   failure or SearchClosed())
            self._loop.call_soon_threadsafe(self._terminated, failure, close_error)

    async def aclose(self) -> None:
        """Close admission now; cancellation of one closer cannot abandon the owned join.

        Python/native work must eventually return: a thread cannot forcibly terminate it.
        SQLite progress checks and Python ranking checkpoints are cooperative cancellation.
        """
        loop = self._bind()
        if self._close_task is None:
            self._closed = True
            self._stopping.set()
            embeddings = []
            for request in self._requests:
                request.cancelled.set()
                if request.embedding is not None:
                    request.embedding.cancel()
                    embeddings.append(request.embedding)
                if request.future is not None and not request.future.done():
                    request.future.set_exception(SearchClosed())
            # A full queue already wakes the worker; never block inserting a sentinel.
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            self._close_task = loop.create_task(self._join(embeddings), name="mind-library-close")
        await asyncio.shield(self._close_task)

    async def _join(self, embeddings: list[asyncio.Task]) -> None:
        if embeddings:
            await asyncio.gather(*embeddings, return_exceptions=True)
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join)
            close_error = await self._stopped
            if close_error is not None:
                raise close_error
