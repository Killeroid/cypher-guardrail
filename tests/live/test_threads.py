"""Real threads, FalkorDB and ``fork``: a slow refresh does not hold up validations, one refresh at a time, a thread that cannot start, a forked child."""

import os
import threading

import pytest
from live_helpers import EVERY, Database, FakeLive, snapshot_of, version_of

from cypher_guardrail import (
    LiveSnapshot,
    SchemaValidator,
)


class Slow:
    """A database whose second capture waits for the test to let it finish."""

    def __init__(self, clock):
        self.clock = clock
        self.version = 1
        self.captures = []
        self.started = threading.Event()
        self.release = threading.Event()

    def fingerprint(self):
        return ("v", self.version)

    def capture(self):
        self.captures.append(self.version)
        if len(self.captures) > 1:
            self.started.set()
            assert self.release.wait(10)
        return snapshot_of(self.version, self.clock)


def test_a_slow_refresh_in_a_real_thread_is_started_once_and_does_not_hold_up_validations(
    clock,
):
    slow = Slow(clock)
    live = FakeLive(slow, clock, hold="thread")
    first = live.current()
    slow.version = 2
    clock.advance(minutes=11)

    assert (
        live.current() is first
    )  # the refresh starts, and `current()` does not wait for it
    assert slow.started.wait(10)
    for _ in range(20):
        assert live.current() is first  # the refresh is running: nobody starts another
    assert slow.captures == [1, 2]

    slow.release.set()
    assert live._thread is not None
    live._thread.join(10)
    assert version_of(live.current()) == 2
    assert slow.captures == [1, 2]


def test_many_threads_asking_at_once_get_a_snapshot_and_start_one_refresh(clock):
    database = Database(clock)
    live = FakeLive(database, clock, hold="thread")
    results = []

    def ask():
        results.append(live.current())

    threads = [threading.Thread(target=ask) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # all twelve got the snapshot, and it was captured once: the others found the fingerprint unchanged
    assert len(results) == 12
    assert len({id(r) for r in results}) == 1
    assert database.capture_calls == 1


@pytest.fixture
def kg_client(falkordb_client):
    return falkordb_client


def test_with_a_real_database_a_refresh_looks_first_and_scans_only_when_the_graph_changed(
    kg_client, clock, monkeypatch
):
    from cypher_guardrail._internal.core import io as _io

    scans = []
    run = _io.SyncTransport.run
    monkeypatch.setattr(
        _io.SyncTransport,
        "run",
        lambda self, request: (
            (
                scans.append(request.query)
                if "keys(" in getattr(request, "query", "")
                else None
            )
            or run(self, request)
        ),
    )
    graph = kg_client.select_graph("live_lab")
    graph.query("CREATE (:A {p: 1})-[:R]->(:B)")
    try:
        live = LiveSnapshot(kg_client, "live_lab", refresh_every=EVERY, clock=clock)
        first = live.current()
        captured = len(scans)
        assert captured > 0 and first.labels == ("A", "B")

        live.refresh()  # nothing changed: a few cheap queries and no scan
        assert len(scans) == captured
        assert live.current() is first

        graph.query("CREATE (:C {q: 1})")
        live.refresh()  # a label and a node more: it scans
        assert len(scans) > captured
        assert live.current().labels == ("A", "B", "C")
    finally:
        graph.delete()


def test_with_a_real_database_the_validators_follow_the_graph(kg_client, clock):
    graph = kg_client.select_graph("live_follow")
    graph.query("CREATE (:A {p: 1})")
    try:
        live = LiveSnapshot(kg_client, "live_follow", refresh_every=EVERY, clock=clock)
        offline = SchemaValidator(live)
        hybrid = SchemaValidator(kg_client, snapshot=live)
        query = "MATCH (n:Later) RETURN n"
        assert offline.validate(query)[1][0]["label"] == "Later"

        graph.query("CREATE (:Later)")
        # not refreshed yet: offline still says it is unknown, the hybrid checks the miss with the database
        assert offline.validate(query)[1][0]["label"] == "Later"
        assert hybrid.validate(query, "live_follow") == (1.0, [])

        clock.advance(minutes=11)
        offline.validate(query)  # starts the refresh in a real thread
        assert live._thread is not None
        live._thread.join(10)
        assert offline.validate(query) == (1.0, [])  # now it knows
    finally:
        graph.delete()


def test_a_thread_that_cannot_start_does_not_reach_the_validation(
    clock, database, monkeypatch
):
    live = FakeLive(database, clock, hold="thread")
    first = live.current()
    clock.advance(minutes=11)

    def cannot_start(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    assert live.current() is first  # the error is not the validation's
    assert isinstance(live.last_error, RuntimeError)
    monkeypatch.undo()

    clock.advance(minutes=1)  # past the wait after a failure
    database.version = 2
    assert live.current() is first
    assert live._thread is not None
    live._thread.join(10)
    assert version_of(live.current()) == 2  # the refresh lock was not left held


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_forked_child_is_not_left_waiting_for_a_lock_nobody_holds(clock, database):
    import signal

    live = FakeLive(database, clock, hold="thread")
    live.current()
    live._refreshing.acquire()  # as if a refresh were running in this process at the moment of the fork
    read, write = os.pipe()

    pid = os.fork()
    if pid == 0:  # the child: the thread that would release the lock is not here
        try:
            signal.alarm(20)  # a child that hangs is killed, not waited for
            live.refresh()
            os.write(write, b"ok")
        except BaseException as error:  # noqa: BLE001 - whatever happens is the answer
            os.write(write, repr(error).encode())
        finally:
            os._exit(0)

    live._refreshing.release()
    os.close(write)
    answer = os.read(read, 200)
    os.close(read)
    os.waitpid(pid, 0)

    assert answer == b"ok"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_forked_child_keeps_the_snapshot_and_the_state_of_its_parent(clock, database):
    live = FakeLive(database, clock, hold="thread")
    first = live.current()
    read, write = os.pipe()

    pid = os.fork()
    if pid == 0:
        try:
            os.write(write, b"same" if live.current() is first else b"different")
        finally:
            os._exit(0)

    os.close(write)
    answer = os.read(read, 50)
    os.close(read)
    os.waitpid(pid, 0)

    assert answer == b"same"
