"""A snapshot that keeps itself fresh: :class:`LiveSnapshot` and :class:`AsyncLiveSnapshot`.

A :class:`~cypher_guardrail.snapshot.Snapshot` is a picture of a graph at one moment, and it goes stale. A live snapshot holds
a client and the name of a graph, and replaces its snapshot from time to time. It is a
:class:`~cypher_guardrail.snapshot.SnapshotSource`: give it to a validator as ``snapshot=`` (the hybrid mode) or as the driver
(the validator never asks the database), and each validation uses the one snapshot it was given when it started::

    live = LiveSnapshot(client, "kg", refresh_every=timedelta(minutes=10))
    validator = SchemaValidator(client, snapshot=live)

How it refreshes, which is the whole design:

* **When something is validated, not on a timer.** ``current()`` returns the snapshot it has *at once*. If it was last
  checked longer than ``refresh_every`` ago, it also starts a refresh in the background (a daemon thread; for the async
  class an asyncio task) and the next validations get the result. Nothing runs while nobody validates. To refresh at a
  fixed rhythm, or after a migration, call ``refresh()`` yourself.
* **A refresh first looks, then captures.** A *fingerprint* (the names, the indexes, the number of nodes and
  relationships: a few cheap queries) is compared with the one of the snapshot. Only a different one runs the capture,
  which scans the whole graph. The fingerprint is a heuristic: removing one relationship and adding another leaves
  the counts as they were. ``full_every`` captures anyway after that long, and ``refresh(force=True)`` always does.
* **A failing refresh never reaches a validation.** The last good snapshot is kept, the error is in ``last_error``, and
  the next try is not before ``retry_after``. With ``max_age`` a snapshot that was not confirmed for that long is no
  longer served: ``current()`` raises :class:`StaleSnapshotError`.
* **One refresh at a time.** Callers that arrive during a refresh get the snapshot they have.
* **``path=``** keeps a copy on disk (written atomically after each capture) and reads it at the start, so a restart
  is warm.
"""

import asyncio
import contextlib
import os
import threading
import weakref
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta

from cypher_guardrail._internal.sources import capture as _capture
from cypher_guardrail.snapshot import Snapshot, SnapshotSource

_NEVER = datetime.min.replace(tzinfo=UTC)
_NO_LOCK = (
    contextlib.nullcontext()
)  # for the state of the async class, which one thread (the loop's) touches

# The live snapshots of this process. A child made by `os.fork` has the memory of its parent, with the locks as they
# were: one that a refresh thread of the parent held would never be released in the child, which has no such thread.
_instances: "weakref.WeakSet[_LiveBase]" = weakref.WeakSet()


def _after_fork_in_child() -> None:
    for live in list(_instances):
        live._after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


class StaleSnapshotError(RuntimeError):
    """The snapshot was not confirmed for longer than ``max_age`` (and the refreshes since have failed)."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _Tracker:
    """The state and the policy of a live snapshot: what is known, when it is due, what a failure does.

    It does no I/O and starts nothing, so the sync and the async class share it, and a test can drive it with a clock.
    Whoever owns it makes sure it is not touched by two threads at once.
    """

    def __init__(self, refresh_every, max_age, full_every, retry_after):
        for name, value in (
            ("refresh_every", refresh_every),
            ("max_age", max_age),
            ("full_every", full_every),
            ("retry_after", retry_after),
        ):
            if value is None and name in ("max_age", "full_every"):
                continue
            if not isinstance(value, timedelta):
                raise TypeError(f"{name} must be a datetime.timedelta, not {value!r}")
            # retry_after may be zero (try again at once); the others are lengths of time
            if value < timedelta(0) or (
                name != "retry_after" and value == timedelta(0)
            ):
                raise ValueError(f"{name} must be positive, not {value}")
        if max_age is not None and max_age <= refresh_every:
            raise ValueError(
                f"max_age ({max_age}) must be longer than refresh_every ({refresh_every}): otherwise the snapshot "
                "is refused before a refresh has been due, let alone tried"
            )
        self.refresh_every = refresh_every
        self.max_age = max_age
        self.full_every = full_every
        self.retry_after = retry_after
        self.snapshot: Snapshot | None = None
        self.fingerprint: tuple | None = None
        self.checked_at: datetime | None = (
            None  # when the snapshot was last known to be current
        )
        self.captured_at: datetime = _NEVER  # when it was last captured
        self.error: BaseException | None = None
        self.retry_at: datetime | None = None

    def age(self, now):
        return None if self.checked_at is None else now - self.checked_at

    def due(self, now) -> bool:
        """Whether a refresh should start: the snapshot has not been checked for ``refresh_every``, and no failure
        is being waited out."""
        if self.checked_at is None:
            return True
        if self.retry_at is not None and now < self.retry_at:
            return False
        return now - self.checked_at >= self.refresh_every

    def check_max_age(self, now):
        if (
            self.max_age is not None
            and self.checked_at is not None
            and now - self.checked_at > self.max_age
        ):
            raise StaleSnapshotError(
                f"The snapshot was last confirmed {now - self.checked_at} ago, more than max_age={self.max_age}"
                + (f"; the last refresh failed: {self.error!r}" if self.error else "")
            )

    def needs_capture(self, now, fingerprint, force) -> bool:
        return (
            force
            or self.snapshot is None
            or self.fingerprint is None
            or fingerprint != self.fingerprint
            or (
                self.full_every is not None
                and now - self.captured_at >= self.full_every
            )
        )

    def loaded(self, snapshot, now):
        """A snapshot read from the file of ``path``: its age is the age of its ``captured_at`` (unknown: never).

        A date without a zone is taken as UTC (that is what ``captured_at`` is), and one in the future is taken as
        now, so a wrong clock cannot make a snapshot stay fresh for ever.
        """
        self.snapshot = snapshot
        taken = _NEVER
        if snapshot.captured_at:
            with contextlib.suppress(ValueError):
                taken = datetime.fromisoformat(snapshot.captured_at)
                if taken.tzinfo is None:
                    taken = taken.replace(tzinfo=UTC)
        self.checked_at = self.captured_at = min(taken, now)

    def captured(self, snapshot, fingerprint, now):
        self.snapshot, self.fingerprint = snapshot, fingerprint
        self.checked_at = self.captured_at = now
        self.error = self.retry_at = None

    def confirmed(self, now):
        self.checked_at = now
        self.error = self.retry_at = None

    def failed(self, error, now):
        self.error = error
        self.retry_at = now + self.retry_after


def _check_names(graph, sample, types):
    if not isinstance(graph, str) or not graph:
        raise ValueError(f"graph must be the name of a graph, not {graph!r}")
    _capture._check_sample(sample)
    _capture._check_types(sample, types)


def _read(path, graph, tracker, now):
    """Start from the file at ``path``, if it has a snapshot of this graph; what is wrong with it is the last error."""
    if path is None or not os.path.exists(path):
        return
    try:
        snapshot = Snapshot.load(path)
        if snapshot.graph != graph:
            raise ValueError(
                f"{path} is a snapshot of the graph {snapshot.graph!r}, not {graph!r}"
            )
    except (ValueError, OSError) as error:
        tracker.error = error
    else:
        tracker.loaded(snapshot, now)


class _LiveBase(SnapshotSource):
    """What the sync and the async live snapshot share: the options, the state, and what can be read of it.

    A subclass says how the state of the tracker is protected (``_state``), and does the refreshing.
    """

    @property
    def _state(self) -> AbstractContextManager:
        """Held while the state of the tracker is read or written in more than one step."""
        raise NotImplementedError

    def __init__(
        self,
        client,
        graph: str,
        *,
        refresh_every: timedelta,
        max_age: timedelta | None,
        full_every: timedelta | None,
        retry_after: timedelta,
        sample: int | None,
        types: bool,
        path: str | os.PathLike | None,
        clock: Callable[[], datetime],
    ):
        _check_names(graph, sample, types)
        self._client = client
        self._graph = graph
        self._sample = sample
        self._types = types
        self._path = path
        self._clock = clock
        self._t = _Tracker(refresh_every, max_age, full_every, retry_after)
        self._closed = False
        _read(path, graph, self._t, self._clock())
        _instances.add(self)

    # ----------------------------------------------------------------------------- reading it
    @property
    def graph(self) -> str:
        return self._graph

    @property
    def captured_at(self) -> str | None:
        """When the snapshot it has was captured (ISO 8601), or None before the first capture."""
        snapshot = self._t.snapshot
        return None if snapshot is None else snapshot.captured_at

    @property
    def last_checked_at(self) -> datetime | None:
        """When the snapshot was last known to be current: captured, or found unchanged by a refresh."""
        return self._t.checked_at

    @property
    def age(self) -> timedelta | None:
        """How long ago the snapshot was last known to be current, or None before there is one."""
        with self._state:
            return self._t.age(self._clock())

    @property
    def last_error(self) -> BaseException | None:
        """What went wrong in the last refresh (or in reading ``path``); None when the last one worked."""
        return self._t.error

    def _save(self, snapshot: Snapshot) -> None:
        """Write the file of ``path``. A failure is recorded and the snapshot is used all the same."""
        if self._path is None:
            return
        try:
            snapshot.save(self._path)
        except OSError as error:
            with self._state:
                self._t.error = error

    def _after_fork(self) -> None:
        """In a child made by ``os.fork``: no refresh is running here, whatever the locks say."""
        raise NotImplementedError

    def __repr__(self):
        return f"{type(self).__name__}(graph={self._graph!r}, captured_at={self.captured_at!r})"


class LiveSnapshot(_LiveBase):
    """A :class:`~cypher_guardrail.snapshot.SnapshotSource` that refreshes its snapshot from a database (see the module).

    Args:
        client: A ``falkordb.FalkorDB`` client (or FalkorDBLite), or a ``falkordb.Graph``.
        graph: The name of the graph.
        refresh_every: How long a snapshot is trusted before a validation starts a refresh (a ``timedelta``).
        max_age: If given, a snapshot not confirmed for this long is not served: ``current()`` raises
            :class:`StaleSnapshotError`. The default is to serve what there is.
        full_every: If given, capture again after this long even when the fingerprint has not changed.
        retry_after: How long to wait after a failed refresh before trying again. Default 30 seconds.
        sample: Passed to :meth:`Snapshot.capture` (see there for what it costs in accuracy).
        types: Passed to :meth:`Snapshot.capture`: also record the types of the values of the properties.
        path: A file to keep the snapshot in. It is read at the start and written, atomically, after each capture.
        clock: A function that returns the current time as a timezone-aware ``datetime``. For tests.

    The first ``current()`` of a live snapshot that has nothing yet (no readable ``path``) captures the graph and waits
    for it; if that fails, the error is raised. Use :class:`AsyncLiveSnapshot` in async code: this class blocks on the
    first capture.
    """

    def __init__(
        self,
        client,
        graph: str,
        *,
        refresh_every: timedelta,
        max_age: timedelta | None = None,
        full_every: timedelta | None = None,
        retry_after: timedelta = timedelta(seconds=30),
        sample: int | None = None,
        types: bool = False,
        path: str | os.PathLike | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ):
        super().__init__(
            client,
            graph,
            refresh_every=refresh_every,
            max_age=max_age,
            full_every=full_every,
            retry_after=retry_after,
            sample=sample,
            types=types,
            path=path,
            clock=clock,
        )
        self._lock = threading.Lock()  # the state of the tracker
        self._refreshing = threading.Lock()  # held for as long as a refresh runs
        self._thread: threading.Thread | None = None

    @property
    def _state(self) -> AbstractContextManager:
        return self._lock

    # ----------------------------------------------------------------------- the SnapshotSource
    def current(self) -> Snapshot:
        """The snapshot to validate with, at once; a refresh starts in the background if one is due.

        Raises:
            StaleSnapshotError: If ``max_age`` is set and the snapshot has not been confirmed for that long.
            Exception: Whatever the database raised, if there is no snapshot yet and the first capture fails.
        """
        now = self._clock()
        with self._lock:
            snapshot = self._t.snapshot
            due = snapshot is not None and self._t.due(now)
        if snapshot is None:
            return self._first_snapshot()  # there is nothing to serve yet
        if due and not self._closed:
            self._refresh_in_background()
        with self._lock:
            self._t.check_max_age(now)
        return snapshot

    def _first_snapshot(self) -> Snapshot:
        """The first snapshot, which there is nothing to serve in place of: captured now, once however many are
        waiting for it, and not tried again before ``retry_after`` when the capture failed."""
        self._raise_while_waiting_to_retry()
        with self._refreshing:
            with self._lock:
                snapshot = self._t.snapshot
            if snapshot is not None:
                return snapshot  # another caller captured it while this one waited for its turn
            self._raise_while_waiting_to_retry()  # or failed to
            return self._refresh(False)

    def _raise_while_waiting_to_retry(self) -> None:
        """Raise the error of the last try if it failed less than ``retry_after`` ago: the database is not asked again."""
        with self._lock:
            error, retry_at = self._t.error, self._t.retry_at
        if error is not None and retry_at is not None and self._clock() < retry_at:
            raise error

    # ------------------------------------------------------------------------------ refreshing
    def refresh(self, force: bool = False) -> Snapshot:
        """Look at the graph now, and capture it again if its fingerprint has changed (or ``force``). Waits for a
        refresh that is running. Raises what the database raised; the last good snapshot is kept."""
        with self._refreshing:
            return self._refresh(force)

    def close(self) -> None:
        """Stop starting refreshes; wait for one that is running. ``current()`` goes on serving what there is."""
        self._closed = True
        with self._refreshing:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def _fingerprint(self) -> tuple:
        return _capture.fingerprint(self._client, self._graph)

    def _capture(self) -> Snapshot:
        return Snapshot.capture(
            self._client, self._graph, sample=self._sample, types=self._types
        )

    def _after_fork(self) -> None:
        """In a child made by ``os.fork``: no refresh is running here, whatever the locks say."""
        self._lock = threading.Lock()
        self._refreshing = threading.Lock()
        self._thread = None

    def _spawn(self, target: Callable[[], None]) -> None:
        self._thread = threading.Thread(
            target=target, name="cypher-guardrail-live-snapshot", daemon=True
        )
        self._thread.start()

    def _refresh_in_background(self) -> None:
        if not self._refreshing.acquire(blocking=False):
            return  # one is running: whoever arrives meanwhile gets the snapshot there is
        try:
            self._spawn(self._background)
        except BaseException as error:
            self._refreshing.release()
            if not isinstance(error, Exception):
                raise
            # A thread that cannot start (the process is out of them) is a failed refresh like any other: it
            # must not reach the validation that asked for the snapshot
            with self._lock:
                self._t.failed(error, self._clock())

    def _background(self) -> None:
        try:
            with contextlib.suppress(Exception):  # it is in last_error
                self._refresh(False)
        finally:
            self._refreshing.release()

    def _refresh(self, force: bool) -> Snapshot:
        """The refresh itself; the caller holds ``_refreshing``."""
        try:
            fingerprint = self._fingerprint()
            with self._lock:
                capture = self._t.needs_capture(self._clock(), fingerprint, force)
                snapshot = self._t.snapshot
            if capture or snapshot is None:
                snapshot = self._capture()
                with self._lock:
                    self._t.captured(snapshot, fingerprint, self._clock())
                self._save(snapshot)
            else:
                with self._lock:
                    self._t.confirmed(self._clock())
        except Exception as error:
            with self._lock:
                self._t.failed(error, self._clock())
            raise
        return snapshot


class AsyncLiveSnapshot(_LiveBase):
    """The async twin of :class:`LiveSnapshot`, for a ``falkordb.asyncio`` client: ``refresh()`` is a coroutine and the
    refresh a validation starts is an asyncio task.

    ``current()`` never waits (a validator calls it without ``await``), so there has to be a snapshot before the first
    validation: ``await live.refresh()`` once, use ``async with`` (which does it), or give a ``path=`` that holds one.
    Otherwise ``current()`` raises ``RuntimeError``. The arguments are those of :class:`LiveSnapshot`, and
    ``max_concurrency`` is the one of the capture.

    Use it from one event loop at a time, with a client that belongs to that loop: after one loop is finished it may
    be used in the next (``asyncio.run`` twice, say), which starts with a lock and a refresh of its own. Called outside
    an event loop, ``current()`` serves the snapshot it has and starts no refresh: there is nothing to run it on.
    """

    def __init__(
        self,
        client,
        graph: str,
        *,
        refresh_every: timedelta,
        max_age: timedelta | None = None,
        full_every: timedelta | None = None,
        retry_after: timedelta = timedelta(seconds=30),
        sample: int | None = None,
        types: bool = False,
        path: str | os.PathLike | None = None,
        max_concurrency: int = 8,
        clock: Callable[[], datetime] = _utc_now,
    ):
        super().__init__(
            client,
            graph,
            refresh_every=refresh_every,
            max_age=max_age,
            full_every=full_every,
            retry_after=retry_after,
            sample=sample,
            types=types,
            path=path,
            clock=clock,
        )
        self._max_concurrency = max_concurrency
        # The lock and the task belong to one event loop: see _guard
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._task: asyncio.Task | None = None

    @property
    def _state(self) -> AbstractContextManager:
        return _NO_LOCK  # one thread, one event loop: the state is not touched by two at once

    # ----------------------------------------------------------------------- the SnapshotSource
    def current(self) -> Snapshot:
        """The snapshot to validate with, at once; a refresh starts as a task if one is due.

        Raises:
            RuntimeError: If there is no snapshot yet (see the class).
            StaleSnapshotError: If ``max_age`` is set and the snapshot has not been confirmed for that long.
        """
        snapshot = self._t.snapshot
        if snapshot is None:
            raise RuntimeError(
                "This AsyncLiveSnapshot has no snapshot yet: `await live.refresh()` once, use `async with`, "
                "or give path= of a saved snapshot."
            )
        now = self._clock()
        if self._t.due(now) and not self._closed:
            self._refresh_in_background()
        self._t.check_max_age(now)
        return snapshot

    # ------------------------------------------------------------------------------ refreshing
    async def refresh(self, force: bool = False) -> Snapshot:
        """The async version of :meth:`LiveSnapshot.refresh`."""
        async with self._guard():
            return await self._refresh(force)

    def close(self) -> None:
        """Stop starting refreshes and cancel the one that is running."""
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def __aenter__(self):
        if self._t.snapshot is None:
            await self.refresh()
        return self

    async def __aexit__(self, *exc_info):
        self.close()

    def _guard(self) -> asyncio.Lock:
        """The lock of the running event loop. A lock that has had to wait is bound to its loop, and a task that is
        still running in a loop that was dropped never ends: so a live snapshot that is used in another loop starts
        with a lock and a task of its own there."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._lock is None:
            self._loop, self._lock, self._task = loop, asyncio.Lock(), None
        return self._lock

    def _after_fork(self) -> None:
        """In a child made by ``os.fork``: the event loop and its tasks are not this process's."""
        self._loop = self._lock = self._task = None

    async def _fingerprint(self) -> tuple:
        return await _capture.fingerprint_async(
            self._client, self._graph, max_concurrency=self._max_concurrency
        )

    async def _capture(self) -> Snapshot:
        return await Snapshot.capture_async(
            self._client,
            self._graph,
            sample=self._sample,
            types=self._types,
            max_concurrency=self._max_concurrency,
        )

    def _refresh_in_background(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # not in an event loop: nothing to run it on (call `await refresh()` yourself)
        guard = self._guard()
        if (self._task is not None and not self._task.done()) or guard.locked():
            return  # one is scheduled or running: whoever arrives meanwhile gets the snapshot there is
        self._task = loop.create_task(self._background())

    async def _background(self) -> None:
        with contextlib.suppress(Exception):  # it is in last_error
            async with self._guard():
                await self._refresh(False)

    async def _refresh(self, force: bool) -> Snapshot:
        try:
            fingerprint = await self._fingerprint()
            snapshot = self._t.snapshot
            if snapshot is None or self._t.needs_capture(
                self._clock(), fingerprint, force
            ):
                snapshot = await self._capture()
                self._t.captured(snapshot, fingerprint, self._clock())
                self._save(snapshot)
            else:
                self._t.confirmed(self._clock())
        except Exception as error:
            self._t.failed(error, self._clock())
            raise
        return snapshot
