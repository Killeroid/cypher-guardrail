"""The I/O layer that lets one copy of the validation logic serve both the sync and the async API.

The logic is written as generators. Wherever it needs the database it ``yield``s a request and is
sent the answer::

    rows = yield RoQuery("kg", "MATCH (n) RETURN count(n)")

Nothing in the logic knows whether it is run by :func:`run_sync` or by :func:`run_async`. A driver
answers each request through a *transport* (a ``falkordb`` graph, sync or async), and throws any error
into the generator at the ``yield``, so ``try/except`` in the logic works as it would around a plain call.

Requests are:

* :class:`RoQuery`: ``GRAPH.RO_QUERY``. The answer is falkordb's ``QueryResult``.
* :class:`Explain`: ``GRAPH.EXPLAIN``. The answer is ``None``: it only matters that the database accepts the query.
  With ``read_only`` it is ``GRAPH.RO_QUERY "EXPLAIN ..."`` instead: still nothing is executed, but the database
  also refuses a query that can write (a ``CypherError`` with the code ``NOT_READ_ONLY``).
* :class:`Lookup`: a question about the graph ("does this path exist?", "which of these labels are unknown?"),
  not a query. The driver is given a *resolver*, a function that takes the lookup and returns a logic generator
  whose return value is the answer. The online resolver (``cypher_guardrail._internal.sources.online``) answers by asking the database,
  with ``RoQuery``; a resolver could as well answer from a summary of the graph and ask nothing. The generator may
  yield any request, lookups included, and its errors are thrown into the logic that asked.
* :class:`Concurrently`: several logic generators that do not depend on each other (each may ask many requests,
  one after another). The answer is the tuple of what they return. The sync driver runs them one after the
  other, the async driver at the same time; the number of requests in flight at once is still limited by
  ``max_concurrency``. Either way every generator runs, and then the first error, in order, is thrown into
  the generator that asked.
* :class:`Remember` and :class:`Recall`: keep a value the logic worked out (such as "this graph has no schema
  yet", which is not an answer the database gave) for the rest of the snapshot, and get it back. They never
  reach the database.

Inside :func:`snapshot` the answer to a request is kept, so asking the same thing twice costs one round-trip.
"""

import asyncio
import contextvars
from contextlib import closing, contextmanager
from dataclasses import dataclass

from redis.exceptions import ResponseError

from cypher_guardrail._internal.core.errors import (
    CypherError,
    is_missing_graph,
    missing_graph_key,
    to_cypher_error,
)


@dataclass(frozen=True)
class RoQuery:
    """Run ``query`` on the graph named ``graph`` (it can never modify the graph)."""

    graph: str
    query: str


@dataclass(frozen=True)
class Explain:
    """Check ``query`` against the graph named ``graph`` without executing it."""

    graph: str
    query: str
    read_only: bool = False


@dataclass(frozen=True)
class Concurrently:
    """Logic generators (a tuple) that do not depend on each other and can run at the same time.

    The answer is the tuple of what they return, in order.
    """

    generators: tuple


@dataclass(frozen=True)
class Remember:
    """Keep ``value`` under ``key`` until the snapshot ends (nothing is kept outside a snapshot)."""

    key: tuple
    value: object


@dataclass(frozen=True)
class Recall:
    """The value kept under ``key`` in this snapshot, or None if there is none."""

    key: tuple


@dataclass(frozen=True)
class Lookup:
    """A question about the graph named ``graph``, answered by the resolver of the driver.

    ``kind`` says which question (the resolver knows them) and ``args`` is a tuple of what it is about.
    """

    kind: str
    graph: str
    args: tuple


# What a logic generator may yield
Request = RoQuery | Explain | Lookup | Concurrently | Remember | Recall


# ---------------------------------------------------------------------- snapshots
# {transport: {request: answer}} for every snapshot open in the current thread / asyncio task. A ContextVar,
# not a global, so threads and tasks never see each other's snapshots. A task created inside a snapshot
# inherits it, so the requests it makes are shared with the ones made around it.
_snapshots: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "cypher_guardrail_snapshots", default=None
)


@contextmanager
def snapshot(transport):
    """Keep the answers of ``transport`` for the duration of the ``with`` block.

    A single validation asks the same questions many times (is ``Goal`` a label? does ``Goal`` have
    ``code``?). Inside a snapshot each is asked once. Nothing is kept after the outermost block ends, so
    the next validation sees any change made to the graph. Snapshots nest. Works around ``await`` as well.

    A snapshot belongs to the thread or asyncio task that opened it. Tasks it starts, and a copied context
    (``asyncio.to_thread``, ``contextvars.copy_context().run``), inherit it and share its answers; a new
    ``threading.Thread`` does not.
    """
    active = _snapshots.get() or {}
    if transport in active:
        yield
        return
    token = _snapshots.set({**active, transport: {}})
    try:
        yield
    finally:
        _snapshots.reset(token)


def _answers_kept_for(transport):
    """The dict of answers of the open snapshot of ``transport``, or None when there is none."""
    active = _snapshots.get()
    return None if active is None else active.get(transport)


# ------------------------------------------------------------------------ transports
def _explain_command(graph, request):
    """The raw command that answers an :class:`Explain`, as the arguments of ``execute_command``.

    Sent as a raw command: falkordb's ``Graph.explain()`` insists on parsing a plan, but FalkorDB answers index
    commands (CREATE INDEX, DROP INDEX, ...) with a plain string instead.
    """
    if request.read_only:
        return "GRAPH.RO_QUERY", graph.name, f"EXPLAIN {request.query}"
    return "GRAPH.EXPLAIN", graph.name, request.query


class SyncTransport:
    """Answers requests with sync ``falkordb`` graphs.

    Args:
        select_graph: Called with a graph name, returns the ``falkordb.Graph`` to use.
    """

    def __init__(self, select_graph):
        self._select_graph = select_graph

    def run(self, request):
        """Answer one :class:`RoQuery` or :class:`Explain`. Raises CypherError if the database rejects it."""
        graph = self._select_graph(request.graph)
        try:
            if isinstance(request, RoQuery):
                return graph.ro_query(request.query)
            graph.execute_command(*_explain_command(graph, request))
        except ResponseError as e:
            raise to_cypher_error(e) from e


class AsyncTransport:
    """Answers requests with ``falkordb.asyncio`` graphs.

    Args:
        select_graph: Called with a graph name, returns the ``falkordb.asyncio.graph.AsyncGraph`` to use.
        max_concurrency (int): The most requests in flight at once on this transport, in each event loop.

    The ``falkordb.asyncio`` client is bound to the event loop that first used it (its connections cannot
    be used from another loop), so use one client, and one transport, per loop. The limit on requests in
    flight belongs to the loop the transport last ran in: used from another loop, it starts a new limit
    instead of failing on a semaphore that belongs to the first. Several loops running at the same time
    (one per thread) are therefore not limited as a whole.
    """

    def __init__(self, select_graph, max_concurrency=8):
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1.")
        self._select_graph = select_graph
        self._max_concurrency = max_concurrency
        self._slots_loop = None
        self._slots = None

    def _slots_for_running_loop(self):
        """The semaphore limiting requests in the running event loop (a new one when the loop has changed).

        A semaphore is bound to the loop that first has to wait on it, so it cannot be shared between loops.
        """
        loop = asyncio.get_running_loop()
        if self._slots_loop is not loop:
            self._slots_loop = loop
            self._slots = asyncio.Semaphore(self._max_concurrency)
        return self._slots

    async def run(self, request):
        """Answer one :class:`RoQuery` or :class:`Explain`. Raises CypherError if the database rejects it."""
        graph = self._select_graph(request.graph)
        async with self._slots_for_running_loop():
            try:
                if isinstance(request, RoQuery):
                    return await graph.ro_query(request.query)
                await graph.execute_command(*_explain_command(graph, request))
            except ResponseError as e:
                raise to_cypher_error(e) from e


# ---------------------------------------------------------------------------- drivers
def run_sync(generator, transport, resolver=None):
    """Run a logic generator to the end, answering its requests through the sync ``transport``.

    Args:
        resolver: Answers the :class:`Lookup`s: called with one, it returns the generator that gives the answer.
            Not needed by logic that asks no lookups.

    Returns:
        What the generator returns.
    """
    return _drive_sync(generator, transport, resolver, _answers_kept_for(transport))


class _Done:
    """What :func:`_resume` gives when the generator has returned: its value."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


def _resume(generator, answer, error):
    """Give a logic generator the answer of its last request (or throw the error into it): its next request, or a
    :class:`_Done` with what it returned."""
    try:
        return generator.send(answer) if error is None else generator.throw(error)
    except StopIteration as done:
        return _Done(done.value)


def _note_missing_graph(request, error, answers):
    """Note, for the run, that a request failed because its graph does not exist. The lookups take such a graph for one
    with no schema (a capture needs that), and ``queries.graph_checked`` ends a validation with the error."""
    graph = getattr(request, "graph", None)
    if (
        answers is not None
        and graph is not None
        and isinstance(error, CypherError)
        and is_missing_graph(error)
    ):
        answers[("remembered", missing_graph_key(graph))] = True


def _drive_sync(generator, transport, resolver, answers):
    answer = error = None
    with closing(generator):
        while True:
            request = _resume(generator, answer, error)
            if isinstance(request, _Done):
                return request.value
            answer = error = None
            try:
                answer = _answer_sync(request, transport, resolver, answers)
            except Exception as e:  # noqa: BLE001 - every error is thrown into the generator, as it would be from a plain call
                error = e
                _note_missing_graph(request, e, answers)


def _resolve(lookup, resolver):
    """The logic generator that answers ``lookup``."""
    if resolver is None:
        raise TypeError(f"{lookup} was asked, but the run has no resolver to answer it")
    return resolver(lookup)


def _answer_remembered(request, answers):
    """Answer a Remember or Recall from the snapshot. Its keys are tuples, so they never meet a request's."""
    if isinstance(request, Recall):
        return None if answers is None else answers.get(("remembered", request.key))
    if answers is not None:
        answers[("remembered", request.key)] = request.value


def _answer_sync(request, transport, resolver, answers):
    if isinstance(request, Remember | Recall):
        return _answer_remembered(request, answers)
    if isinstance(request, Lookup):
        return _drive_sync(_resolve(request, resolver), transport, resolver, answers)
    if not isinstance(request, Concurrently):
        return _answer_one_sync(request, transport, answers)
    results, first_error = [], None
    for generator in request.generators:
        try:
            results.append(_drive_sync(generator, transport, resolver, answers))
        except Exception as e:  # noqa: BLE001 - the first error is raised once every generator has run
            results.append(None)
            if first_error is None:
                first_error = e
    if first_error is not None:
        raise first_error
    return tuple(results)


def _answer_one_sync(request, transport, answers):
    if answers is None:
        return transport.run(request)
    if request not in answers:
        answers[request] = transport.run(request)
    return answers[request]


async def run_async(generator, transport, resolver=None):
    """Run a logic generator to the end, answering its requests through the async ``transport``.

    Args:
        resolver: Answers the :class:`Lookup`s, as for :func:`run_sync`.

    Returns:
        What the generator returns.
    """
    return await _drive_async(
        generator, transport, resolver, _answers_kept_for(transport)
    )


async def _drive_async(generator, transport, resolver, answers):
    answer = error = None
    with closing(generator):
        while True:
            request = _resume(generator, answer, error)
            if isinstance(request, _Done):
                return request.value
            answer = error = None
            try:
                answer = await _answer_async(request, transport, resolver, answers)
            except Exception as e:  # noqa: BLE001 - every error is thrown into the generator, as it would be from a plain call
                error = e
                _note_missing_graph(request, e, answers)


async def _answer_async(request, transport, resolver, answers):
    if isinstance(request, Remember | Recall):
        return _answer_remembered(request, answers)
    if isinstance(request, Lookup):
        return await _drive_async(
            _resolve(request, resolver), transport, resolver, answers
        )
    if not isinstance(request, Concurrently):
        return await _answer_one_async(request, transport, answers)
    outcomes = await asyncio.gather(
        *(
            _drive_async(generator, transport, resolver, answers)
            for generator in request.generators
        ),
        return_exceptions=True,
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
    return tuple(outcomes)


class _InFlight:
    """A request being answered, and how many waiters share the answer."""

    __slots__ = ("task", "waiters")

    def __init__(self, task):
        self.task = task
        self.waiters = 0


async def _answer_one_async(request, transport, answers):
    if answers is None:
        return await transport.run(request)
    # What is kept is the request being answered, so one already in flight is awaited, not sent again
    pending = answers.get(request)
    if pending is None:
        task = asyncio.ensure_future(transport.run(request))
        pending = answers[request] = _InFlight(task)
        task.add_done_callback(lambda _: _forget_if_failed(answers, request, pending))
    pending.waiters += 1
    try:
        # Shielded: one waiter being cancelled must not cancel the request the others are waiting for
        return await asyncio.shield(pending.task)
    finally:
        pending.waiters -= 1
        if pending.waiters == 0 and not pending.task.done():
            # The last waiter is gone (cancelled, for example by a timeout), so nobody needs the answer:
            # stop the query, which frees its slot. Forget it now, not when the task ends, so that a run
            # arriving in between asks again instead of joining a request that is being cancelled.
            pending.task.cancel()
            if answers.get(request) is pending:
                del answers[request]


def _forget_if_failed(answers, request, pending):
    """A request that failed is not kept, so that asking again asks the database again.

    This runs before the waiters resume, so a waiter that retries finds no stale failure. Reading
    ``task.exception()`` also marks the error as retrieved, in case every waiter was cancelled.
    """
    task = pending.task
    if (task.cancelled() or task.exception() is not None) and answers.get(
        request
    ) is pending:
        del answers[request]
