"""The I/O layer: drivers, transports and lookups, with fakes instead of a database.

The first part is the drivers (``run_sync`` / ``run_async``), the transports and the snapshots that keep the answers of
a validation; the second is ``Lookup``, a question the logic asks that a resolver answers (the online resolver's
types lookup included).
"""

import asyncio
import dataclasses
import threading
from types import SimpleNamespace

import pytest
from redis.exceptions import ResponseError

from cypher_guardrail._internal.core import io as _io
from cypher_guardrail._internal.core.errors import CypherError, is_missing_graph
from cypher_guardrail._internal.core.io import (
    AsyncTransport,
    Concurrently,
    Explain,
    Lookup,
    Recall,
    Remember,
    RoQuery,
    SyncTransport,
    run_async,
    run_sync,
    snapshot,
)
from cypher_guardrail._internal.sources import online as _online
from cypher_guardrail.issues import ARITHMETIC_ERROR, EXECUTION_FAILED, SYNTAX_ERROR

# ----------------------------------------------------------------------------------------------------
# The I/O layer: drivers, snapshots and transports, with fakes instead of a database.


A = RoQuery("g", "A")


B = RoQuery("g", "B")


BAD = RoQuery("g", "BAD")


WORSE = RoQuery("g", "WORSE")


class Fake:
    """A transport that records the requests it is asked and answers them from canned results and errors."""

    def __init__(self):
        self.calls = []
        self.errors = {}

    def _answer(self, request):
        self.calls.append(request)
        if request in self.errors:
            raise self.errors[request]
        return f"answer to {request.query}"


class SyncFake(Fake):
    def run(self, request):
        return self._answer(request)


class AsyncFake(Fake):
    async def run(self, request):
        await asyncio.sleep(0)  # let other tasks run, as a real round-trip would
        return self._answer(request)


@pytest.fixture(params=["sync", "async"])
def mode(request):
    """(a fresh transport, a function running a generator on it), for the sync and for the async driver."""
    if request.param == "sync":
        return SyncFake(), run_sync
    return AsyncFake(), lambda generator, transport: asyncio.run(
        run_async(generator, transport)
    )


def asks(*requests):
    """A logic generator that asks each request in turn and returns the answers."""
    answers = []
    for request in requests:
        answers.append((yield request))  # noqa: PERF401 - a yield cannot be in a comprehension
    return answers


def single(request):
    """A logic generator that asks one request and returns its answer."""
    return (yield request)


def together(*requests):
    """Ask the requests at the same time: a Concurrently of generators that each ask one."""
    return Concurrently(tuple(single(request) for request in requests))


def database_error(message="boom"):
    return CypherError(EXECUTION_FAILED, message)


# --------------------------------------------------------------------------- drivers
def test_the_generator_is_sent_the_answers_and_its_value_is_returned(mode):
    transport, run = mode
    assert run(asks(A, B), transport) == ["answer to A", "answer to B"]
    assert transport.calls == [A, B]


def test_a_database_error_is_thrown_into_the_generator(mode):
    transport, run = mode
    transport.errors[BAD] = database_error()

    def logic():
        try:
            yield BAD
        except CypherError as e:
            return e.code
        return "no error"

    assert run(logic(), transport) == EXECUTION_FAILED


def test_an_error_the_generator_does_not_catch_reaches_the_caller(mode):
    transport, run = mode
    transport.errors[BAD] = database_error()
    with pytest.raises(CypherError):
        run(asks(A, BAD, B), transport)
    assert transport.calls == [A, BAD]


def test_errors_that_are_not_database_errors_are_thrown_in_too(mode):
    transport, run = mode
    transport.errors[BAD] = ConnectionError("down")

    def logic():
        try:
            yield BAD
        except ConnectionError:
            return "caught"

    assert run(logic(), transport) == "caught"


def test_the_generator_is_closed_when_an_error_stops_the_run(mode):
    transport, run = mode
    transport.errors[BAD] = database_error()
    cleaned = []

    def logic():
        try:
            yield BAD
        finally:
            cleaned.append(True)

    with pytest.raises(CypherError):
        run(logic(), transport)
    assert cleaned == [True]


# ------------------------------------------------------------------------ Concurrently
def test_concurrently_answers_in_order(mode):
    transport, run = mode
    assert run(asks(together(A, B)), transport) == [("answer to A", "answer to B")]


def test_concurrently_runs_every_generator_and_throws_the_first_error_in_order(mode):
    transport, run = mode
    transport.errors[BAD] = database_error("first")
    transport.errors[WORSE] = database_error("second")

    def logic():
        try:
            yield together(A, BAD, B, WORSE)
        except CypherError as e:
            return e.message

    assert run(logic(), transport) == "first"
    assert sorted(request.query for request in transport.calls) == [
        "A",
        "B",
        "BAD",
        "WORSE",
    ]


# ------------------------------------------------------------------------- snapshots
def test_requests_are_repeated_outside_a_snapshot(mode):
    transport, run = mode
    run(asks(A, A), transport)
    assert transport.calls == [A, A]


def test_a_snapshot_keeps_answers_until_it_closes(mode):
    transport, run = mode
    with snapshot(transport):
        run(asks(A, A, B), transport)
        run(asks(A), transport)
        assert transport.calls == [A, B]
    run(asks(A), transport)
    assert transport.calls == [A, B, A]


def test_a_failed_request_is_not_kept(mode):
    transport, run = mode
    transport.errors[A] = database_error()
    with snapshot(transport):
        with pytest.raises(CypherError):
            run(asks(A), transport)
        del transport.errors[A]
        assert run(asks(A, A), transport) == ["answer to A"] * 2
    assert transport.calls == [A, A]


def test_nested_snapshots_share_one_cache(mode):
    transport, run = mode
    with snapshot(transport):
        run(asks(A), transport)
        with snapshot(transport):
            run(asks(A), transport)
        run(asks(A), transport)
    assert transport.calls == [A]


def test_a_snapshot_belongs_to_its_transport(mode):
    transport, run = mode
    other = type(transport)()
    with snapshot(transport):
        run(asks(A, A), other)
    assert other.calls == [A, A]


def test_snapshots_of_two_transports_keep_separate_caches(mode):
    transport, run = mode
    other = type(transport)()
    with snapshot(transport), snapshot(other):
        run(asks(A, A), transport)
        run(asks(A, A), other)
    assert transport.calls == [A]
    assert other.calls == [A]


def test_nothing_is_kept_after_an_error_in_the_with_block(mode):
    transport, run = mode
    with pytest.raises(RuntimeError), snapshot(transport):
        raise RuntimeError
    run(asks(A, A), transport)
    assert transport.calls == [A, A]


def test_a_snapshot_is_private_to_its_thread():
    transport = SyncFake()
    with snapshot(transport):
        run_sync(asks(A), transport)
        worker = threading.Thread(target=run_sync, args=(asks(A, A), transport))
        worker.start()
        worker.join()
    assert transport.calls == [A, A, A]


# ------------------------------------------------------------- remembered values
def test_a_remembered_value_is_recalled_until_the_snapshot_ends(mode):
    transport, run = mode
    with snapshot(transport):
        assert run(asks(Recall(("k",))), transport) == [None]
        run(asks(Remember(("k",), frozenset())), transport)
        # an empty value is still a value
        assert run(asks(Recall(("k",)), Recall(("other",))), transport) == [
            frozenset(),
            None,
        ]
    assert run(asks(Recall(("k",))), transport) == [None]
    assert transport.calls == []  # they never reach the database


def test_nothing_is_remembered_outside_a_snapshot(mode):
    transport, run = mode
    run(asks(Remember(("k",), 1)), transport)
    assert run(asks(Recall(("k",))), transport) == [None]


def test_a_remembered_value_does_not_clash_with_an_answer(mode):
    transport, run = mode
    with snapshot(transport):
        run(asks(Remember((A,), "mine"), A), transport)
        assert run(asks(Recall((A,)), A), transport) == ["mine", "answer to A"]


# ------------------------------------------------------------- async: concurrency
class Tracker:
    """The fake database behind ``FakeAsyncGraph``: what was asked, and how many requests were in flight."""

    def __init__(self):
        self.calls = []
        self.fail = set()
        self.delay = 0.01
        self.in_flight = 0
        self.peak = 0

    def select_graph(self, name):
        return FakeAsyncGraph(name, self)


class FakeAsyncGraph:
    def __init__(self, name, tracker):
        self.name = name
        self.tracker = tracker

    async def ro_query(self, query):
        tracker = self.tracker
        tracker.calls.append(("ro_query", self.name, query))
        tracker.in_flight += 1
        tracker.peak = max(tracker.peak, tracker.in_flight)
        try:
            await asyncio.sleep(tracker.delay)
        finally:
            tracker.in_flight -= 1
        if query in tracker.fail:
            raise ResponseError("boom")
        return f"rows of {query}"

    async def execute_command(self, *command):
        self.tracker.calls.append(("execute_command", *command))


def numbered_requests(count):
    return tuple(RoQuery("g", f"Q{i}") for i in range(count))


def test_concurrently_runs_at_the_same_time_up_to_max_concurrency():
    async def peak(limit, count):
        tracker = Tracker()
        transport = AsyncTransport(tracker.select_graph, max_concurrency=limit)
        await run_async(asks(together(*numbered_requests(count))), transport)
        return tracker.peak

    assert asyncio.run(peak(limit=8, count=5)) == 5
    assert asyncio.run(peak(limit=2, count=6)) == 2
    assert asyncio.run(peak(limit=1, count=3)) == 1


def test_a_transport_can_be_used_from_another_event_loop():
    tracker = Tracker()
    transport = AsyncTransport(tracker.select_graph, max_concurrency=2)

    async def burst():
        await run_async(asks(together(*numbered_requests(6))), transport)

    for _ in range(2):  # each asyncio.run is a new event loop
        tracker.peak = 0
        asyncio.run(burst())
        assert tracker.peak == 2


def test_max_concurrency_must_be_positive():
    with pytest.raises(ValueError, match="at least 1"):
        AsyncTransport(Tracker().select_graph, max_concurrency=0)


def test_a_request_already_in_flight_is_not_sent_again():
    async def main():
        tracker = Tracker()
        transport = AsyncTransport(tracker.select_graph)
        with snapshot(transport):
            # twice in one Concurrently, and again from a second run alongside the first
            await asyncio.gather(
                run_async(asks(together(A, A, B)), transport),
                run_async(asks(A), transport),
            )
        return tracker.calls

    assert asyncio.run(main()) == [
        ("ro_query", "g", "A"),
        ("ro_query", "g", "B"),
    ]


def test_a_failed_request_is_shared_by_those_waiting_and_then_forgotten():
    async def main():
        tracker = Tracker()
        tracker.fail.add("BAD")
        transport = AsyncTransport(tracker.select_graph)

        def logic():
            try:
                yield together(BAD, BAD)
            except CypherError:
                return "failed"

        with snapshot(transport):
            first = await run_async(logic(), transport)
            tracker.fail.clear()
            second = await run_async(asks(BAD), transport)
        return first, second, len(tracker.calls)

    first, second, sent = asyncio.run(main())
    assert (first, second) == ("failed", ["rows of BAD"])
    assert (
        sent == 2
    )  # once for the failure (shared by both waiters), once for the retry


def test_a_timeout_inside_a_snapshot_stops_the_query_and_frees_its_slot():
    async def main():
        tracker = Tracker()
        tracker.delay = 5
        transport = AsyncTransport(tracker.select_graph, max_concurrency=1)
        with snapshot(transport):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(run_async(asks(A), transport), 0.02)
            await asyncio.sleep(0.01)  # let the cancelled query unwind
            assert tracker.in_flight == 0
            tracker.delay = 0.01
            # the only slot is free again
            return await asyncio.wait_for(run_async(asks(B), transport), 1)

    assert asyncio.run(main()) == ["rows of B"]


def test_a_request_survives_one_of_its_waiters_being_cancelled():
    async def main():
        tracker = Tracker()
        transport = AsyncTransport(tracker.select_graph)
        with snapshot(transport):
            first = asyncio.ensure_future(run_async(asks(A), transport))
            second = asyncio.ensure_future(run_async(asks(A), transport))
            await asyncio.sleep(0)  # both are now waiting for the one request
            first.cancel()
            return await second, len(tracker.calls)

    assert asyncio.run(main()) == (["rows of A"], 1)


def test_a_run_arriving_after_the_last_waiter_left_asks_again():
    async def main():
        tracker = Tracker()
        tracker.delay = 5
        transport = AsyncTransport(tracker.select_graph)
        with snapshot(transport):
            first = asyncio.ensure_future(run_async(asks(A), transport))
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.sleep(
                0
            )  # the last waiter is gone, so the request is cancelled and forgotten
            tracker.delay = 0.01
            return await run_async(asks(A), transport), len(tracker.calls)

    assert asyncio.run(main()) == (["rows of A"], 2)


def steps(name, count):
    """A logic generator that asks ``count`` requests one after another, each after the previous one's answer."""
    answers = []
    for i in range(count):
        answers.append((yield RoQuery("g", f"{name}{i}")))  # noqa: PERF401 - a yield cannot be in a comprehension
    return answers


def test_concurrently_runs_generators_that_ask_several_requests(mode):
    transport, run = mode
    answers = run(
        asks(Concurrently((steps("a", 3), steps("b", 2), steps("c", 1)))), transport
    )
    assert answers == [
        (
            ["answer to a0", "answer to a1", "answer to a2"],
            ["answer to b0", "answer to b1"],
            ["answer to c0"],
        )
    ]
    # whatever the interleaving, each generator asked its requests in its own order
    for name in "abc":
        asked = [r.query for r in transport.calls if r.query.startswith(name)]
        assert asked == sorted(asked)
    assert len(transport.calls) == 6


def test_a_sync_run_does_one_generator_after_the_other():
    transport = SyncFake()
    run_sync(asks(Concurrently((steps("a", 2), steps("b", 2)))), transport)
    assert [r.query for r in transport.calls] == ["a0", "a1", "b0", "b1"]


def test_concurrently_nests(mode):
    transport, run = mode

    def outer():
        inner = yield Concurrently((steps("x", 2), steps("y", 2)))
        return inner

    (answers,) = run(asks(Concurrently((outer(),))), transport)
    assert answers == (
        (["answer to x0", "answer to x1"], ["answer to y0", "answer to y1"]),
    )


def test_a_generator_that_catches_its_own_error_does_not_disturb_the_others(mode):
    transport, run = mode
    transport.errors[RoQuery("g", "b0")] = database_error()

    def careful():
        try:
            yield RoQuery("g", "b0")
        except CypherError:
            return "recovered"

    answers = run(
        asks(Concurrently((steps("a", 2), careful(), steps("c", 2)))), transport
    )
    assert answers == [
        (
            ["answer to a0", "answer to a1"],
            "recovered",
            ["answer to c0", "answer to c1"],
        )
    ]


def test_an_error_stops_only_its_own_generator(mode):
    transport, run = mode
    transport.errors[RoQuery("g", "b1")] = database_error("second step of b")

    def logic():
        try:
            yield Concurrently((steps("a", 3), steps("b", 3), steps("c", 3)))
        except CypherError as e:
            return e.message

    assert run(logic(), transport) == "second step of b"
    asked = sorted(r.query for r in transport.calls)
    # a and c ran to the end, b stopped at its failing step
    assert asked == ["a0", "a1", "a2", "b0", "b1", "c0", "c1", "c2"]


def test_generators_that_ask_several_requests_overlap_up_to_max_concurrency():
    async def run(limit):
        tracker = Tracker()
        transport = AsyncTransport(tracker.select_graph, max_concurrency=limit)
        answers = await run_async(
            asks(Concurrently(tuple(steps(f"g{i}_", 3) for i in range(4)))), transport
        )
        return tracker.peak, len(tracker.calls), answers

    peak, sent, (answers,) = asyncio.run(run(limit=8))
    assert (peak, sent) == (4, 12)  # four generators, each a chain of three
    assert answers[2] == ["rows of g2_0", "rows of g2_1", "rows of g2_2"]
    assert asyncio.run(run(limit=2))[0] == 2
    assert asyncio.run(run(limit=1))[0] == 1


def test_cancelling_a_run_cancels_the_generators_running_together():
    async def main():
        tracker = Tracker()
        tracker.delay = 5
        transport = AsyncTransport(tracker.select_graph, max_concurrency=4)
        with snapshot(transport):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    run_async(
                        asks(Concurrently(tuple(steps(f"g{i}_", 2) for i in range(4)))),
                        transport,
                    ),
                    0.05,
                )
            await asyncio.sleep(0.05)  # let the cancelled queries unwind
            return tracker.in_flight

    assert asyncio.run(main()) == 0


# --------------------------------------------------------------------- transports
class ErroringGraph:
    """A sync ``falkordb.Graph`` stand-in whose calls raise ``error`` when it is set."""

    name = "g"

    def __init__(self, error=None):
        self.error = error
        self.commands = []

    def ro_query(self, query):
        if self.error:
            raise self.error
        return f"rows of {query}"

    def execute_command(self, *command):
        if self.error:
            raise self.error
        self.commands.append(command)


class ErroringAsyncGraph(ErroringGraph):
    async def ro_query(self, query):
        return super().ro_query(query)

    async def execute_command(self, *command):
        return super().execute_command(*command)


@pytest.fixture(params=["sync", "async"])
def transport_kit(request):
    """(a graph class, builds a transport on a graph, runs a request on a transport) for sync and async."""
    if request.param == "sync":
        return (
            ErroringGraph,
            lambda graph: SyncTransport(lambda _: graph),
            lambda transport, request: transport.run(request),
        )
    return (
        ErroringAsyncGraph,
        lambda graph: AsyncTransport(lambda _: graph),
        lambda transport, request: asyncio.run(transport.run(request)),
    )


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("Invalid input 'X': expected ...", SYNTAX_ERROR),
        ("Division by zero", ARITHMETIC_ERROR),
        ("Type mismatch", EXECUTION_FAILED),
    ],
)
def test_a_response_error_becomes_a_cypher_error(transport_kit, message, code):
    graph_class, build, run = transport_kit
    transport = build(graph_class(ResponseError(message)))
    for request in (RoQuery("g", "RETURN 1"), Explain("g", "RETURN 1")):
        with pytest.raises(CypherError) as error:
            run(transport, request)
        assert error.value.code == code


def test_an_error_that_is_not_a_response_error_is_not_changed(transport_kit):
    graph_class, build, run = transport_kit
    transport = build(graph_class(ConnectionError("down")))
    with pytest.raises(ConnectionError):
        run(transport, RoQuery("g", "RETURN 1"))


def test_a_query_answers_with_the_rows_and_an_explain_with_nothing(transport_kit):
    graph_class, build, run = transport_kit
    graph = graph_class()
    transport = build(graph)
    assert run(transport, RoQuery("g", "RETURN 1")) == "rows of RETURN 1"
    assert run(transport, Explain("g", "CREATE INDEX FOR (n:N) ON (n.x)")) is None
    assert graph.commands == [("GRAPH.EXPLAIN", "g", "CREATE INDEX FOR (n:N) ON (n.x)")]


# ----------------------------------------------------------------------------------------------------
# ``Lookup``: a question the logic asks, answered by a resolver that the driver is given.
#
# The logic asks "does this path exist?", not "run this query". How the question is answered (by the database,
# and later from a snapshot of the graph) is the resolver's business. These tests use a fake transport and fake
# resolvers, so they need no database and run on every Python version.


class Transport:
    """Answers a request with its own text, and keeps what it was asked."""

    def __init__(self):
        self.asked = []

    def run(self, request):
        self.asked.append(request)
        if request.query == "boom":
            raise CypherError("code", "boom")
        return request.query.upper()


class InFlightAsyncTransport:
    """Answers after a short wait, and keeps how many requests were in flight at once."""

    def __init__(self):
        self.asked = []
        self.in_flight = 0
        self.most_in_flight = 0

    async def run(self, request):
        self.asked.append(request)
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            return request.query.upper()
        finally:
            self.in_flight -= 1


def resolver(lookup):
    """The online way: a lookup is answered by asking the database one thing and shaping the answer."""
    answer = yield RoQuery(lookup.graph, lookup.args[0])
    return (lookup.kind, answer)


def test_a_lookup_is_a_request_that_can_be_kept_and_shared():
    lookup = Lookup("path_exists", "kg", ("(:A)",))

    assert lookup == Lookup("path_exists", "kg", ("(:A)",))
    assert lookup != Lookup("path_exists", "kg", ("(:B)",))
    assert len({lookup, Lookup("path_exists", "kg", ("(:A)",))}) == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(lookup, "kind", "other")  # noqa: B010 - frozen: a plain assignment would not even type-check


def test_the_logic_gets_what_the_resolver_returns():
    def logic():
        return (yield Lookup("path_exists", "kg", ("a",)))

    transport = Transport()

    assert _io.run_sync(logic(), transport, resolver) == ("path_exists", "A")
    assert transport.asked == [RoQuery("kg", "a")]


def test_what_a_resolver_asks_the_database_is_shared_inside_a_snapshot():
    def logic():
        first = yield Lookup("one", "kg", ("a",))
        second = yield Lookup("two", "kg", ("a",))
        return first, second

    transport = Transport()
    with _io.snapshot(transport):
        answer = _io.run_sync(logic(), transport, resolver)

    assert answer == (("one", "A"), ("two", "A"))
    assert transport.asked == [RoQuery("kg", "a")]  # asked once, answered twice


def test_an_error_of_the_resolver_is_raised_where_the_logic_asked():
    def logic():
        try:
            yield Lookup("path_exists", "kg", ("boom",))
        except CypherError as e:
            return f"caught {e.code}"
        return "not caught"

    assert _io.run_sync(logic(), Transport(), resolver) == "caught code"


def test_lookups_asked_together_all_run_and_the_first_error_is_raised():
    def one(text):
        return (yield Lookup("k", "kg", (text,)))

    def logic():
        try:
            yield Concurrently((one("a"), one("boom"), one("b")))
        except CypherError:
            return "raised"
        return "no error"

    transport = Transport()

    assert _io.run_sync(logic(), transport, resolver) == "raised"
    assert [r.query for r in transport.asked] == ["a", "boom", "b"]  # every one ran


def test_lookups_asked_together_return_their_answers_in_order():
    def one(text):
        return (yield Lookup("k", "kg", (text,)))

    def logic():
        return (yield Concurrently((one("a"), one("b"))))

    assert _io.run_sync(logic(), Transport(), resolver) == (("k", "A"), ("k", "B"))


def test_a_resolver_can_answer_without_asking_the_database():
    def offline(lookup):
        return lookup.args[0] * 2
        yield  # makes it a generator that never asks anything

    def logic():
        return (yield Lookup("k", "kg", ("ab",)))

    transport = Transport()

    assert _io.run_sync(logic(), transport, offline) == "abab"
    assert transport.asked == []


def test_a_resolver_can_ask_for_other_lookups():
    def chained(lookup):
        if lookup.kind == "outer":
            inner = yield Lookup("inner", lookup.graph, lookup.args)
            return ("outer", inner)
        answer = yield RoQuery(lookup.graph, lookup.args[0])
        return ("inner", answer)

    def logic():
        return (yield Lookup("outer", "kg", ("a",)))

    assert _io.run_sync(logic(), Transport(), chained) == (
        "outer",
        ("inner", "A"),
    )


def test_the_async_driver_resolves_lookups_too(run_async_loop):
    def one(text):
        return (yield Lookup("k", "kg", (text,)))

    def logic():
        return (yield Concurrently((one("a"), one("b"), one("c"))))

    transport = InFlightAsyncTransport()

    async def ask():
        return await _io.run_async(logic(), transport, resolver)

    assert run_async_loop(ask) == (("k", "A"), ("k", "B"), ("k", "C"))
    assert transport.most_in_flight == 3  # they ran at the same time


def test_an_async_error_of_the_resolver_is_raised_where_the_logic_asked(
    run_async_loop,
):
    class Failing(InFlightAsyncTransport):
        async def run(self, request):
            raise CypherError("code", "boom")

    def logic():
        try:
            yield Lookup("k", "kg", ("a",))
        except CypherError as e:
            return f"caught {e.code}"
        return "not caught"

    async def ask():
        return await _io.run_async(logic(), Failing(), resolver)

    assert run_async_loop(ask) == "caught code"


def test_what_a_resolver_asks_is_shared_between_lookups_in_flight_together(
    run_async_loop,
):
    def one(kind):
        return (yield Lookup(kind, "kg", ("a",)))

    def logic():
        return (yield Concurrently((one("x"), one("y"))))

    transport = InFlightAsyncTransport()

    async def ask():
        with _io.snapshot(transport):
            return await _io.run_async(logic(), transport, resolver)

    assert run_async_loop(ask) == (("x", "A"), ("y", "A"))
    assert transport.asked == [RoQuery("kg", "a")]


@pytest.fixture
def run_async_loop():
    """Run a coroutine function in a fresh event loop (no database involved)."""

    def run(coroutine_function):
        return asyncio.run(coroutine_function())

    return run


def test_a_lookup_without_a_resolver_is_an_error_not_a_hang():
    def logic():
        return (yield Lookup("k", "kg", ("a",)))

    with pytest.raises(TypeError, match="no resolver"):
        _io.run_sync(logic(), Transport())


def test_the_async_driver_refuses_a_lookup_without_a_resolver(run_async_loop):
    def logic():
        return (yield Lookup("k", "kg", ("a",)))

    async def ask():
        return await _io.run_async(logic(), InFlightAsyncTransport())

    with pytest.raises(TypeError, match="no resolver"):
        run_async_loop(ask)


def test_the_online_resolver_knows_every_kind_the_logic_asks_and_no_other():
    kinds = {
        "missing_names": ("labels", ("A",)),
        "index_exists": ("fulltext", "nodes", "A", None),
        "path_exists": ("(:A)",),
        "property_exists": ("A", "node", "p"),
        "property_types": ("A", "node", "p", "String"),
        "multilabels": (),
        "label_combinations": ("(a)",),
    }
    for kind, args in kinds.items():
        generator = _online.resolve(Lookup(kind, "kg", args))
        assert hasattr(generator, "send"), kind  # a logic generator
        generator.close()

    with pytest.raises(ValueError, match="nope"):
        _online.resolve(Lookup("nope", "kg", ()))


# -------------------------------------------------------------------- the types of a property (online)


class Rows:
    """What a RoQuery answers: the header and the rows of a result."""

    def __init__(self, *rows):
        self.header = [[1, "t"]]
        self.result_set = [[row] for row in rows]


class TypeTransport:
    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def run(self, request):
        self.asked.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_the_types_of_a_property_of_a_node_are_asked_with_typeof():
    transport = TypeTransport(Rows("String", "Integer", "String"))

    types = _io.run_sync(
        _online.property_types("kg", "Goal", "node", "year", "String"), transport
    )

    assert types == ("Integer", "String")  # sorted, each once
    (request,) = transport.asked
    assert isinstance(request, RoQuery)
    assert request.graph == "kg"
    assert request.query == (
        "MATCH (x:`Goal`) WHERE (x.`year`) IS NOT NULL RETURN DISTINCT typeof(x.`year`) AS t"
    )


def test_the_types_of_a_property_of_a_relationship_are_asked_with_typeof():
    transport = TypeTransport(Rows("Float"))

    types = _io.run_sync(
        _online.property_types(
            "kg", "HAS_INDICATOR", "relationship", "weight", "Float"
        ),
        transport,
    )

    assert types == ("Float",)
    assert transport.asked[0].query == (
        "MATCH ()-[x:`HAS_INDICATOR`]->() WHERE (x.`weight`) IS NOT NULL "
        "RETURN DISTINCT typeof(x.`weight`) AS t"
    )


def test_a_property_with_no_value_has_no_types():
    transport = TypeTransport(Rows())

    assert (
        _io.run_sync(
            _online.property_types("kg", "Goal", "node", "nope", "String"), transport
        )
        == ()
    )


def test_when_the_database_cannot_answer_no_type_is_known():
    transport = TypeTransport(CypherError("code", "Invalid input: empty key"))

    assert (
        _io.run_sync(
            _online.property_types("none", "Goal", "node", "year", "String"), transport
        )
        == ()
    )


def test_the_literal_type_does_not_change_what_the_database_is_asked():
    queries = []
    for literal_type in ("String", "Integer", "Boolean"):
        transport = TypeTransport(Rows("Integer"))
        _io.run_sync(
            _online.property_types("kg", "Goal", "node", "year", literal_type),
            transport,
        )
        queries.append(transport.asked[0].query)

    assert len(set(queries)) == 1


# ----------------------------------------------------------------------------------------------------
# What the online lookups do with the errors of the database

MISSING_GRAPH = CypherError(
    "FalkorDB.DatabaseError.Statement.ExecutionFailed",
    "errMsg: Invalid graph operation on empty key",
)
TIMED_OUT = CypherError(
    "FalkorDB.DatabaseError.Statement.ExecutionFailed", "Query timed out"
)
NOT_PARSED = CypherError(
    "FalkorDB.ClientError.Statement.SyntaxError", "errMsg: Invalid input 'x'"
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("errMsg: Invalid graph operation on empty key", True),
        ("Invalid graph operation on empty key", True),
        ("Query timed out", False),
        ("errMsg: Invalid input 'x'", False),
        ("", False),
    ],
)
def test_a_graph_that_does_not_exist_is_told_by_the_message_of_the_database(
    message, expected
):
    error = CypherError("FalkorDB.DatabaseError.Statement.ExecutionFailed", message)

    assert is_missing_graph(error) is expected


def test_a_label_in_a_graph_that_does_not_exist_is_no_label():
    transport = TypeTransport(MISSING_GRAPH)

    assert _io.run_sync(
        _online.missing_names("none", "labels", ("Goal",)), transport
    ) == ("Goal",)


@pytest.mark.parametrize("error", [TIMED_OUT, NOT_PARSED])
def test_a_label_is_not_called_unknown_because_the_database_failed(error):
    # a timeout or an out-of-memory error says nothing about the schema: it is not "no such label"
    transport = TypeTransport(error)

    with pytest.raises(CypherError) as raised:
        _io.run_sync(_online.missing_names("kg", "labels", ("Goal",)), transport)

    assert raised.value is error


def test_a_property_in_a_graph_that_does_not_exist_has_no_types():
    transport = TypeTransport(MISSING_GRAPH)

    assert (
        _io.run_sync(
            _online.property_types("none", "Goal", "node", "year", "String"), transport
        )
        == ()
    )


def test_a_name_the_database_cannot_parse_has_no_types():
    transport = TypeTransport(NOT_PARSED)

    assert (
        _io.run_sync(
            _online.property_types("kg", "Two Words", "node", "year", "String"),
            transport,
        )
        == ()
    )


def test_a_property_is_not_called_typeless_because_the_database_failed():
    transport = TypeTransport(TIMED_OUT)

    with pytest.raises(CypherError) as raised:
        _io.run_sync(
            _online.property_types("kg", "Goal", "node", "year", "String"), transport
        )

    assert raised.value is TIMED_OUT


def test_the_question_whether_a_query_can_write_stops_a_read_query_after_a_millisecond():
    # FalkorDB takes `EXPLAIN` in a read-only request for part of the query, and runs it: a write is refused before
    # that, a read query is stopped by the timeout
    command = _io._explain_command(
        SimpleNamespace(name="g"),
        _io.Explain("g", "MATCH (n) RETURN n", read_only=True),
    )

    assert command == (
        "GRAPH.RO_QUERY",
        "g",
        "EXPLAIN MATCH (n) RETURN n",
        "TIMEOUT",
        _io.READ_ONLY_EXPLAIN_TIMEOUT_MS,
    )
    assert _io.READ_ONLY_EXPLAIN_TIMEOUT_MS == 1
