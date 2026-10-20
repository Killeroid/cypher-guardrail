"""How the lookups of the logic are answered in each mode of the validators.

* **online**: the database answers every lookup (``cypher_guardrail._internal.sources.online``).
* **offline**: a :class:`~cypher_guardrail.snapshot.Snapshot` answers every lookup (``cypher_guardrail._internal.sources.offline``); no database.
* **hybrid**: a client and a snapshot. :class:`ChainResolver` lets the snapshot answer what it can answer for sure and
  asks the database for the rest.

The rules of the hybrid, for each lookup:

1. it is about another graph than the snapshot's: the database answers;
2. the snapshot cannot answer reliably (a path of 2 or more hops it only knows between kinds of nodes, a property
   value): the database answers;
3. the snapshot says the thing is there: the snapshot answers, and the database is not asked;
4. the snapshot says the thing is *not* there: with ``verify="misses"`` (the default) the database is asked before a
   problem is reported, because a snapshot goes stale by lacking new things; with ``verify="never"`` the snapshot is
   trusted. A snapshot that is a sample (``Snapshot.sample``) lacks things it was never going to have, so its "not
   there" is not an answer (rule 2): the database is asked, and with ``verify="never"`` nothing is reported.

So a valid query costs no lookup at all; only an invalid or unusual one asks the database, and then its answer is the
online answer. What a stale snapshot can still get wrong is a thing *deleted* since it was captured: reported as
present, a missed warning and never a false alarm.
"""

import contextvars
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

from cypher_guardrail._internal.core.io import Lookup, Request
from cypher_guardrail._internal.sources import online as _online
from cypher_guardrail._internal.sources.offline import SnapshotResolver
from cypher_guardrail.snapshot import Snapshot, SnapshotSource

VERIFY = ("misses", "never")

# Lookups a snapshot never answers in the hybrid mode: they have no "present" or "missing" to trust. (The label
# combinations the database has, which a stale snapshot would turn into false conflicts, are asked only when a query has
# a variable with several labels, so asking the database costs next to nothing.)
_ALWAYS_ASKED = frozenset({"multilabels"})


def check_verify(verify) -> str:
    """``verify`` or, if it is None, the default."""
    if verify is None:
        return "misses"
    if verify not in VERIFY:
        raise ValueError(f"verify must be one of {', '.join(VERIFY)}, not {verify!r}")
    return verify


class LookupStats:
    """Where the answers to the lookups of a validator came from. The counts only grow; read them as you like.

    Attributes:
        snapshot_answers: Lookups the snapshot answered.
        database_answers: Lookups the database answered (all of them online; in the hybrid mode those the snapshot
            could not answer reliably, other graphs, and the misses that were checked).
        verified_misses: Lookups where the snapshot said "not there" and the database was asked to be sure.
        corrected_misses: Of those, the ones where the database disagreed: a sign that the snapshot is stale.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.snapshot_answers = 0
        self.database_answers = 0
        self.verified_misses = 0
        self.corrected_misses = 0

    def count(self, **increments):
        with self._lock:
            for name, amount in increments.items():
                setattr(self, name, getattr(self, name) + amount)

    def as_dict(self) -> dict[str, int]:
        return {
            "snapshot_answers": self.snapshot_answers,
            "database_answers": self.database_answers,
            "verified_misses": self.verified_misses,
            "corrected_misses": self.corrected_misses,
        }

    def __repr__(self):
        return (
            f"LookupStats({', '.join(f'{k}={v}' for k, v in self.as_dict().items())})"
        )


# {Pins: the snapshot a validation uses} for every validation open in the current thread / asyncio task. A ContextVar,
# like the answers kept by ``core/io.py``'s ``snapshot()``, so threads and tasks never see each other's pins.
_pinned: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "cypher_guardrail_pinned_snapshots", default=None
)


class Pins:
    """The snapshot source of a backend, and which snapshot a validation uses.

    A validation (and a run of a logic generator) asks the source for its snapshot once, when it starts, and keeps it
    until it ends: ``pin()`` is entered around it. Inside, every lookup is answered from that snapshot; a source that
    is refreshed in the middle (a ``LiveSnapshot``) cannot mix two snapshots in one validation.
    """

    def __init__(self, source: SnapshotSource):
        self.source = source
        self._last: tuple[Snapshot, Callable] | None = None

    @property
    def graph(self) -> str:
        return self.source.graph

    @property
    def snapshot(self) -> Snapshot:
        """The snapshot of the validation that is running; outside one, what the source has now."""
        pinned = _pinned.get()
        if pinned is not None and self in pinned:
            return pinned[self]
        return self.source.current()

    @contextmanager
    def pin(self):
        """Use one snapshot, the source's current one, until the block ends. Nested blocks keep the outer one."""
        pinned = _pinned.get() or {}
        if self in pinned:
            yield
            return
        token = _pinned.set({**pinned, self: self.source.current()})
        try:
            yield
        finally:
            _pinned.reset(token)

    def resolver(self, make: Callable[[Snapshot], Callable]) -> Callable:
        """A resolver that answers with ``make(snapshot)`` for the snapshot that is pinned.

        ``make`` builds the resolver of one snapshot (parsing its edges is not free), so the last one is kept for as
        long as the snapshot is the same.
        """

        def resolve(lookup: Lookup) -> Generator[Request, Any, Any]:
            snapshot = self.snapshot
            last = self._last
            if last is None or last[0] is not snapshot:
                last = self._last = (snapshot, make(snapshot))
            return last[1](lookup)

        return resolve


def online_resolver(stats: LookupStats):
    """The online resolver, counting what it answers."""

    def resolve(lookup: Lookup) -> Generator[Request, Any, Any]:
        stats.count(database_answers=1)
        return _online.resolve(lookup)

    return resolve


def snapshot_resolver(snapshot: Snapshot, stats: LookupStats):
    """The offline resolver: the snapshot answers everything, with the plain value, and counts what it answers."""
    resolver = SnapshotResolver(snapshot)

    def resolve(lookup: Lookup) -> Generator[Request, Any, Any]:
        stats.count(snapshot_answers=1)
        return resolver.resolve(lookup)

    return resolve


def chain_resolver(snapshot: Snapshot, verify: str, stats: LookupStats):
    """The hybrid resolver: the snapshot where it is sure, the database for the rest (see the module docstring)."""
    resolver = SnapshotResolver(snapshot)

    def resolve(lookup: Lookup) -> Generator[Request, Any, Any]:
        # 1. a snapshot of one graph says nothing about another (a hand-written one has no graph name: any)
        if snapshot.graph and lookup.graph != snapshot.graph:
            return _ask_the_database(lookup, stats)
        if lookup.kind in _ALWAYS_ASKED:
            return _ask_the_database(lookup, stats)
        # Where the database raises a syntax error so does the snapshot, and the text of the pattern alone decides it
        answer = resolver.answer(lookup)
        # 2. an approximate answer is not trusted. A "no" of a sample is one too, but "never" asks nothing about misses
        if not answer.reliable:
            if answer.sampled and verify == "never":
                stats.count(snapshot_answers=1)
                return _answer(answer.value)
            return _ask_the_database(lookup, stats)
        # 4. "not there" is checked before a problem is reported
        if answer.miss and verify == "misses":
            return _verify_the_miss(lookup, answer.value, stats)
        # 3. "there": no round trip
        stats.count(snapshot_answers=1)
        return _answer(answer.value)

    return resolve


def _answer(value) -> Generator[Request, Any, Any]:
    return value
    yield  # the unreachable yield makes this a generator that asks for nothing


def _ask_the_database(
    lookup: Lookup, stats: LookupStats
) -> Generator[Request, Any, Any]:
    stats.count(database_answers=1)
    return _online.resolve(lookup)


def _verify_the_miss(
    lookup: Lookup, snapshot_value, stats: LookupStats
) -> Generator[Request, Any, Any]:
    stats.count(database_answers=1, verified_misses=1)
    value = yield from _online.resolve(lookup)
    if not _same(value, snapshot_value):
        stats.count(corrected_misses=1)
    return value


def _same(database_value, snapshot_value) -> bool:
    """Whether the database agreed with the snapshot (a table is compared by its rows, whatever their order)."""
    if (
        isinstance(database_value, tuple)
        and database_value
        and hasattr(database_value[0], "to_numpy")
    ):
        frame, other = database_value[0], snapshot_value[0]
        return other is not None and (
            sorted(map(tuple, frame.to_numpy().tolist()))
            == sorted(map(tuple, other.to_numpy().tolist()))
        )
    if (
        isinstance(database_value, tuple)
        and database_value
        and database_value[0] is None
    ):
        return snapshot_value[0] is None
    return database_value == snapshot_value


class OfflineTransport:
    """The transport of a validator that has no database: asking it anything is a mistake in the validator."""

    def run(self, request):
        raise RuntimeError(
            f"{request} needs a database, and this validator was given a snapshot only. "
            "Create it with a client (and the snapshot as snapshot=) to answer what a snapshot cannot."
        )


class ModeInfo:
    """``mode`` and ``stats`` of a validator, read from its backend."""

    backend: Any

    @property
    def mode(self) -> str:
        """``"online"`` (a database answers), ``"offline"`` (a snapshot answers) or ``"hybrid"`` (both)."""
        return self.backend.mode

    @property
    def stats(self) -> LookupStats:
        """Where the answers to the lookups came from (see :class:`LookupStats`)."""
        return self.backend.stats
