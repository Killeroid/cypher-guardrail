"""The first snapshot of a live snapshot: captured on first use and waited for, once however many threads ask, and not tried again before ``retry_after`` when the database is down."""

import threading
from datetime import timedelta

import pytest
from live_helpers import START, Database, FakeLive, live_snapshot, version_of

from cypher_guardrail import (
    SnapshotSource,
)


def test_a_live_snapshot_is_a_snapshot_source(clock, database):
    live = live_snapshot(clock, database)

    assert isinstance(live, SnapshotSource)
    assert live.graph == "g"


def test_nothing_is_captured_until_it_is_used(clock, database):
    live = live_snapshot(clock, database)

    assert database.capture_calls == database.fingerprint_calls == 0
    assert live.captured_at is None
    assert live.age is None
    assert live.last_checked_at is None
    assert live.last_error is None


def test_the_first_use_captures_and_waits_for_it(clock, database):
    live = live_snapshot(clock, database)

    snapshot = live.current()

    assert version_of(snapshot) == 1
    assert (database.fingerprint_calls, database.capture_calls) == (1, 1)
    assert live.pending == []  # no background refresh: it was captured on the spot
    assert live.captured_at == snapshot.captured_at
    assert live.last_checked_at == START
    assert live.age == timedelta(0)


def test_when_the_first_capture_fails_there_is_nothing_to_serve_and_the_error_is_raised(
    clock, database
):
    database.failing = ConnectionError("down")
    live = live_snapshot(clock, database)

    with pytest.raises(ConnectionError):
        live.current()

    assert isinstance(live.last_error, ConnectionError)
    assert live.captured_at is None


def test_it_recovers_from_a_failed_first_capture(clock, database):
    database.failing = ConnectionError("down")
    live = live_snapshot(clock, database)
    with pytest.raises(ConnectionError):
        live.current()

    database.failing = None
    clock.advance(
        seconds=31
    )  # the wait after a failure (30 seconds unless told otherwise) is over

    assert version_of(live.current()) == 1
    assert live.last_error is None


def test_a_first_capture_that_failed_is_not_tried_again_before_retry_after(
    clock, database
):
    database.failing = ConnectionError("down")
    live = live_snapshot(clock, database, retry_after=timedelta(seconds=30))
    with pytest.raises(ConnectionError):
        live.current()
    assert database.fingerprint_calls == 1

    clock.advance(seconds=29)
    with pytest.raises(ConnectionError) as again:
        live.current()

    assert (
        database.fingerprint_calls == 1
    )  # the database was not asked again: the error of the last try is raised
    assert again.value is live.last_error

    clock.advance(seconds=2)  # past the wait
    database.failing = None
    assert version_of(live.current()) == 1
    assert database.fingerprint_calls == 2
    assert live.last_error is None


def test_a_manual_refresh_tries_whatever_the_wait(clock, database):
    database.failing = ConnectionError("down")
    live = live_snapshot(clock, database, retry_after=timedelta(minutes=5))
    with pytest.raises(ConnectionError):
        live.current()
    database.failing = None

    assert (
        version_of(live.refresh()) == 1
    )  # asked for, so tried, though a minute has not passed
    assert database.fingerprint_calls == 2


def test_many_threads_asking_while_the_database_is_down_make_one_attempt(
    clock, monkeypatch
):
    release = threading.Event()

    class DownAfterAWhile(Database):
        def fingerprint(self):
            self.fingerprint_calls += 1
            assert release.wait(
                10
            )  # the first try is held until all the threads are waiting for their turn
            raise ConnectionError("down")

    database = DownAfterAWhile(clock)
    live = FakeLive(database, clock, hold="thread")
    counting = CountingLock()
    monkeypatch.setattr(live, "_refreshing", counting)
    outcomes = []

    def ask():
        try:
            outcomes.append(live.current())
        except ConnectionError as error:
            outcomes.append(error)

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for thread in threads:
        thread.start()
    while counting.attempts < 8:  # one is trying, seven are waiting for their turn
        threading.Event().wait(0.001)
    release.set()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 8
    assert all(isinstance(outcome, ConnectionError) for outcome in outcomes)
    assert (
        database.fingerprint_calls == 1
    )  # the seven found that it had just failed, and did not try again


class CountingLock:
    """The lock of a refresh, that counts how many have tried to take it."""

    def __init__(self):
        self._lock = threading.Lock()
        self._count = threading.Lock()
        self.attempts = 0

    def acquire(self, blocking=True):
        with self._count:
            self.attempts += 1
        return self._lock.acquire(blocking)

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc_info):
        self.release()


def test_threads_that_waited_for_the_first_capture_find_it_done_and_look_at_nothing(
    clock,
    monkeypatch,
):
    release = threading.Event()

    class SlowFirstCapture(Database):
        def capture(self):
            assert release.wait(
                10
            )  # the first capture is held until all the threads are waiting for it
            return super().capture()

    database = SlowFirstCapture(clock)
    live = FakeLive(database, clock, hold="thread")
    counting = CountingLock()
    monkeypatch.setattr(live, "_refreshing", counting)
    results = []

    def ask():
        results.append(live.current())

    threads = [threading.Thread(target=ask) for _ in range(12)]
    for thread in threads:
        thread.start()
    while counting.attempts < 12:  # one holds the lock, eleven are waiting for it
        threading.Event().wait(0.001)
    release.set()
    for thread in threads:
        thread.join()

    assert len({id(r) for r in results}) == 1
    assert (database.fingerprint_calls, database.capture_calls) == (
        1,
        1,
    )  # not a fingerprint for each waiter


def test_a_file_that_could_not_be_read_does_not_hold_back_the_first_capture(
    clock, database, tmp_path
):
    path = tmp_path / "kg.json"
    path.write_text("not a snapshot")
    live = live_snapshot(clock, database, path=path, retry_after=timedelta(hours=1))
    assert isinstance(
        live.last_error, ValueError
    )  # an error, but not a failed capture: nothing to wait out

    assert version_of(live.current()) == 1


def test_the_first_capture_is_tried_again_at_exactly_retry_after_and_not_a_moment_before(
    clock, database
):
    database.failing = ConnectionError("down")
    live = live_snapshot(clock, database, retry_after=timedelta(seconds=30))
    with pytest.raises(ConnectionError):
        live.current()

    clock.advance(seconds=29, milliseconds=999)
    with pytest.raises(ConnectionError):
        live.current()
    assert database.fingerprint_calls == 1

    clock.advance(
        milliseconds=1
    )  # exactly 30 seconds after the failure, as `due()` has it
    database.failing = None
    assert version_of(live.current()) == 1
    assert database.fingerprint_calls == 2
