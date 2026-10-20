"""``LiveSnapshot`` and ``AsyncLiveSnapshot``: a snapshot that keeps itself fresh.

Most of these tests need no database and no waiting: the clock, the fingerprint and the capture are fakes, and the
refresh a validation starts is held back until the test runs it, so every state can be looked at. The last part uses
FalkorDB and a real thread.
"""

import asyncio
import dataclasses
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from cypher_guardrail import (
    AsyncLiveSnapshot,
    AsyncSchemaValidator,
    LiveSnapshot,
    SchemaValidator,
    Snapshot,
    SnapshotSource,
    StaleSnapshotError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

EVERY = timedelta(minutes=10)
START = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, **how_much):
        self.now += timedelta(**how_much)


def snapshot_of(version, clock, graph="g"):
    """A snapshot that says which version it is (by its one label) and when it was captured."""
    return dataclasses.replace(
        Snapshot.from_schema(nodes={f"V{version}": []}, relationships=[], graph=graph),
        captured_at=clock.now.isoformat(timespec="seconds"),
    )


class Database:
    """What the live snapshot looks at: a fingerprint and a capture, counted, that can be made to fail."""

    def __init__(self, clock):
        self.clock = clock
        self.version = 1
        self.fingerprint_calls = 0
        self.capture_calls = 0
        self.failing = None

    def fingerprint(self):
        self.fingerprint_calls += 1
        if self.failing:
            raise self.failing
        return ("version", self.version)

    def capture(self):
        self.capture_calls += 1
        if self.failing:
            raise self.failing
        return snapshot_of(self.version, self.clock)


class FakeLive(LiveSnapshot):
    """A ``LiveSnapshot`` that looks at a fake database and decides what happens to its background refreshes.

    ``hold`` is ``"held"`` (the refresh a validation starts waits in ``pending`` until the test runs it), ``"now"``
    (it runs the moment it starts) or ``"thread"`` (a real thread, as in use).
    """

    def __init__(self, database, clock, hold="held", **options):
        options.setdefault("refresh_every", EVERY)
        super().__init__(None, "g", clock=clock, **options)
        self.database = database
        self.hold = hold
        self.pending = []

    def _fingerprint(self):
        return self.database.fingerprint()

    def _capture(self):
        return self.database.capture()

    def _spawn(self, target):
        if self.hold == "held":
            self.pending.append(target)
        elif self.hold == "now":
            target()
        else:
            super()._spawn(target)


def live_snapshot(clock, database, **options):
    return FakeLive(database, clock, **options)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def database(clock):
    return Database(clock)


def version_of(snapshot):
    (label,) = snapshot.labels
    return int(label[1:])


def run_pending(live):
    while live.pending:
        live.pending.pop(0)()


# ------------------------------------------------------------------------------ the first snapshot


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


# ----------------------------------------------------------------------- serving, and refreshing in use


def test_a_fresh_snapshot_is_served_with_no_refresh(clock, database):
    live = live_snapshot(clock, database)
    first = live.current()

    clock.advance(minutes=9, seconds=59)

    assert live.current() is first
    assert live.pending == []
    assert (database.fingerprint_calls, database.capture_calls) == (1, 1)


def test_an_old_snapshot_is_served_at_once_and_a_refresh_starts_in_the_background(
    clock, database
):
    live = live_snapshot(clock, database)
    first = live.current()
    database.version = 2
    clock.advance(minutes=10)

    served = live.current()

    assert served is first  # the old one, without waiting
    assert len(live.pending) == 1  # and a refresh has been started
    assert database.capture_calls == 1  # but it has not run yet
    run_pending(live)
    assert version_of(live.current()) == 2
    assert database.capture_calls == 2


def test_two_uses_while_a_refresh_is_due_start_one_refresh(clock, database):
    live = live_snapshot(clock, database)
    live.current()
    clock.advance(minutes=11)

    live.current()
    live.current()
    live.current()

    assert len(live.pending) == 1


def test_a_new_refresh_is_not_started_before_the_last_one_is_done(clock, database):
    live = live_snapshot(clock, database)
    live.current()
    clock.advance(minutes=11)
    live.current()  # started, not run: the lock is held

    clock.advance(minutes=11)
    live.current()

    assert len(live.pending) == 1
    run_pending(live)
    clock.advance(minutes=11)
    live.current()
    assert len(live.pending) == 1  # the lock is free again: the next one starts


def test_nothing_is_started_by_a_clock_that_has_not_moved_on_a_snapshot_that_was_just_checked(
    clock, database
):
    live = live_snapshot(clock, database)
    live.current()
    live.refresh()

    for _ in range(5):
        live.current()

    assert live.pending == []


# ----------------------------------------------------------------------------------- the fingerprint


def test_a_refresh_that_finds_the_fingerprint_unchanged_does_not_capture(
    clock, database
):
    live = live_snapshot(clock, database)
    first = live.current()
    clock.advance(minutes=11)

    live.current()
    run_pending(live)

    assert database.fingerprint_calls == 2
    assert database.capture_calls == 1  # the expensive part did not run
    assert live.current() is first
    assert live.last_checked_at == clock.now  # but it has been looked at
    assert live.age == timedelta(0)
    assert live.captured_at == first.captured_at  # the snapshot is as old as it was


def test_a_changed_fingerprint_captures(clock, database):
    live = live_snapshot(clock, database)
    live.current()
    database.version = 2
    clock.advance(minutes=11)

    live.current()
    run_pending(live)

    assert database.capture_calls == 2
    assert version_of(live.current()) == 2


def test_refresh_with_force_captures_even_when_nothing_changed(clock, database):
    live = live_snapshot(clock, database)
    live.current()

    live.refresh(force=True)

    assert database.capture_calls == 2


def test_refresh_without_force_on_a_graph_that_did_not_change_only_looks(
    clock, database
):
    live = live_snapshot(clock, database)
    live.current()

    snapshot = live.refresh()

    assert (database.fingerprint_calls, database.capture_calls) == (2, 1)
    assert snapshot is live.current()


def test_refresh_with_a_change_captures_at_once_whatever_the_time(clock, database):
    live = live_snapshot(clock, database)
    live.current()
    database.version = 2

    assert version_of(live.refresh()) == 2  # after a migration, say
    assert version_of(live.current()) == 2


def test_the_fingerprint_is_not_enough_for_ever_when_full_every_is_given(
    clock, database
):
    live = live_snapshot(clock, database, full_every=timedelta(hours=1))
    live.current()

    clock.advance(minutes=30)
    live.refresh()
    assert database.capture_calls == 1  # unchanged, and not an hour yet

    clock.advance(minutes=31)
    live.refresh()
    assert database.capture_calls == 2  # an hour since the capture: capture anyway


def test_full_every_counts_from_the_capture_and_is_reached_exactly(clock, database):
    live = live_snapshot(clock, database, full_every=timedelta(hours=1))
    live.current()

    clock.advance(minutes=59)
    live.refresh()  # looked at, not captured: it does not move the clock of the capture
    clock.advance(minutes=1)
    live.refresh()

    assert database.capture_calls == 2  # exactly an hour after the capture


def test_without_full_every_an_unchanged_fingerprint_is_trusted_for_ever(
    clock, database
):
    live = live_snapshot(clock, database)
    live.current()

    clock.advance(days=400)
    live.refresh()

    assert database.capture_calls == 1


# ------------------------------------------------------------------------------------------- failures


def test_a_failing_refresh_keeps_the_last_good_snapshot_and_records_the_error(
    clock, database
):
    live = live_snapshot(clock, database)
    first = live.current()
    clock.advance(minutes=11)
    database.failing = TimeoutError("slow")

    assert live.current() is first
    run_pending(live)  # the error does not escape the background

    assert live.current() is first
    assert isinstance(live.last_error, TimeoutError)


def test_a_failure_is_waited_out_before_the_next_try(clock, database):
    live = live_snapshot(clock, database, retry_after=timedelta(seconds=30))
    live.current()
    clock.advance(minutes=11)
    database.failing = TimeoutError("slow")
    live.current()
    run_pending(live)
    assert database.fingerprint_calls == 2

    clock.advance(seconds=29)
    live.current()
    assert live.pending == []  # not yet

    clock.advance(seconds=2)
    live.current()
    assert len(live.pending) == 1  # now


def test_the_error_is_cleared_when_a_refresh_works_again(clock, database):
    live = live_snapshot(clock, database)
    live.current()
    clock.advance(minutes=11)
    database.failing = TimeoutError("slow")
    live.current()
    run_pending(live)
    database.failing = None
    clock.advance(minutes=1)

    live.current()
    run_pending(live)

    assert live.last_error is None
    assert live.last_checked_at == clock.now


def test_a_manual_refresh_raises_what_went_wrong_and_keeps_the_snapshot(
    clock, database
):
    live = live_snapshot(clock, database)
    first = live.current()
    database.failing = ConnectionError("down")

    with pytest.raises(ConnectionError):
        live.refresh()

    assert live.current() is first
    assert isinstance(live.last_error, ConnectionError)


def test_a_snapshot_is_served_for_ever_by_default_however_old(clock, database):
    live = live_snapshot(clock, database)
    first = live.current()
    database.failing = ConnectionError("down")

    clock.advance(days=30)

    assert live.current() is first


def test_max_age_stops_serving_a_snapshot_that_was_not_confirmed_for_that_long(
    clock, database
):
    live = live_snapshot(clock, database, max_age=timedelta(hours=1))
    first = live.current()
    database.failing = ConnectionError("down")

    clock.advance(minutes=59)
    assert live.current() is first
    run_pending(live)  # the failed refresh
    clock.advance(minutes=2)

    with pytest.raises(StaleSnapshotError, match="ConnectionError"):
        live.current()


def test_a_snapshot_exactly_max_age_old_is_still_served_and_one_second_more_is_not(
    clock, database
):
    live = live_snapshot(clock, database, max_age=timedelta(hours=1))
    first = live.current()
    database.failing = ConnectionError("down")

    clock.advance(hours=1)
    assert live.current() is first
    clock.advance(seconds=1)
    with pytest.raises(StaleSnapshotError):
        live.current()


def test_max_age_still_starts_the_refresh_that_could_cure_it(clock, database):
    live = live_snapshot(clock, database, max_age=timedelta(hours=1))
    live.current()
    clock.advance(hours=2)

    with pytest.raises(StaleSnapshotError):
        live.current()
    assert len(live.pending) == 1
    run_pending(live)

    assert version_of(live.current()) == 1  # it was checked just now: served again
    assert live.last_error is None


def test_a_snapshot_that_was_found_unchanged_is_not_too_old(clock, database):
    live = live_snapshot(clock, database, max_age=timedelta(minutes=30))
    live.current()

    for _ in range(5):
        clock.advance(minutes=11)
        live.current()
        run_pending(live)  # the fingerprint is the same: confirmed, not captured

    assert database.capture_calls == 1
    live.current()  # five checks over 55 minutes, and never 30 without one


# ------------------------------------------------------------------------------------------- the file


def test_a_restart_is_warm_when_the_file_has_a_snapshot(clock, database, tmp_path):
    path = tmp_path / "kg.json"
    snapshot_of(7, clock).save(path)
    clock.advance(minutes=3)

    live = live_snapshot(clock, database, path=path)

    assert version_of(live.current()) == 7
    assert (
        database.capture_calls == database.fingerprint_calls == 0
    )  # nothing was captured
    assert live.age == timedelta(minutes=3)  # its age is its captured_at


def test_a_snapshot_read_from_the_file_is_refreshed_when_it_is_old(
    clock, database, tmp_path
):
    path = tmp_path / "kg.json"
    snapshot_of(7, clock).save(path)
    clock.advance(minutes=11)
    live = live_snapshot(clock, database, path=path)

    served = live.current()
    run_pending(live)

    assert version_of(served) == 7
    assert (
        version_of(live.current()) == 1
    )  # the file had no fingerprint, so it is captured


def test_a_file_without_a_date_is_as_old_as_it_gets(clock, database, tmp_path):
    path = tmp_path / "kg.json"
    Snapshot.from_schema(nodes={"V7": []}, relationships=[], graph="g").save(path)
    live = live_snapshot(clock, database, path=path, max_age=timedelta(days=1))

    with pytest.raises(StaleSnapshotError):
        live.current()
    run_pending(live)
    assert version_of(live.current()) == 1


def test_every_capture_is_written_to_the_file(clock, database, tmp_path):
    path = tmp_path / "kg.json"
    live = live_snapshot(clock, database, path=path)

    live.current()
    assert Snapshot.load(path) == live.current()
    database.version = 2
    live.refresh()
    assert version_of(Snapshot.load(path)) == 2


def test_a_refresh_that_finds_nothing_new_does_not_rewrite_the_file(
    clock, database, tmp_path
):
    path = tmp_path / "kg.json"
    live = live_snapshot(clock, database, path=path)
    live.current()
    written = os.stat(path).st_mtime_ns
    os.utime(path, ns=(written - 10**9, written - 10**9))
    before = os.stat(path).st_mtime_ns

    live.refresh()

    assert os.stat(path).st_mtime_ns == before


def test_a_file_of_another_graph_is_not_used_and_says_so(clock, database, tmp_path):
    path = tmp_path / "kg.json"
    snapshot_of(7, clock, graph="other").save(path)

    live = live_snapshot(clock, database, path=path)

    assert isinstance(live.last_error, ValueError)
    assert "other" in str(live.last_error)
    assert version_of(live.current()) == 1  # captured instead


def test_a_file_that_is_not_a_snapshot_is_not_used_and_says_so(
    clock, database, tmp_path
):
    path = tmp_path / "kg.json"
    path.write_text("not json at all")

    live = live_snapshot(clock, database, path=path)

    assert isinstance(live.last_error, ValueError)
    assert version_of(live.current()) == 1


def test_a_file_that_cannot_be_written_does_not_stop_the_snapshot_being_used(
    clock, database, tmp_path, monkeypatch
):
    path = tmp_path / "kg.json"
    live = live_snapshot(clock, database, path=path)

    def full_disk(self, path):
        raise OSError("no space left on device")

    monkeypatch.setattr(Snapshot, "save", full_disk)

    snapshot = live.current()

    assert version_of(snapshot) == 1
    assert isinstance(live.last_error, OSError)


def test_an_interrupted_write_leaves_the_old_file_whole_and_no_stray_file(
    clock, database, tmp_path, monkeypatch
):
    path = tmp_path / "kg.json"
    live = live_snapshot(clock, database, path=path)
    live.current()
    good = path.read_text()
    database.version = 2

    def interrupted(source, destination):
        raise OSError("interrupted")

    monkeypatch.setattr(os, "replace", interrupted)
    live.refresh()

    assert path.read_text() == good
    assert [p.name for p in tmp_path.iterdir()] == ["kg.json"]
    assert version_of(live.current()) == 2  # the snapshot is used all the same


# ---------------------------------------------------------------------------------- close and options


def test_a_closed_live_snapshot_serves_what_it_has_and_starts_nothing(clock, database):
    live = live_snapshot(clock, database)
    first = live.current()
    live.close()
    clock.advance(days=1)

    assert live.current() is first
    assert live.pending == []


def test_it_is_a_context_manager_that_closes(clock, database):
    with live_snapshot(clock, database) as live:
        live.current()
        clock.advance(days=1)
    assert live._closed
    live.current()
    assert live.pending == []


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"refresh_every": timedelta(0)}, ValueError),
        ({"refresh_every": timedelta(minutes=-1)}, ValueError),
        ({"refresh_every": 600}, TypeError),
        ({"max_age": 3600}, TypeError),
        ({"max_age": timedelta(seconds=-1)}, ValueError),
        ({"full_every": "daily"}, TypeError),
        ({"retry_after": -1}, TypeError),
        ({"sample": 0}, ValueError),
        ({"sample": 10, "types": True}, ValueError),
    ],
)
def test_options_that_make_no_sense_are_refused(options, error):
    options = {"refresh_every": EVERY, **options}
    build = cast(
        "Callable[..., object]", LiveSnapshot
    )  # the arguments are wrong on purpose

    with pytest.raises(error):
        build(None, "g", **options)


@pytest.mark.parametrize("graph", ["", None, 5])
def test_the_graph_must_be_a_name(graph):
    with pytest.raises(ValueError, match="name of a graph"):
        LiveSnapshot(None, graph, refresh_every=EVERY)


def test_the_options_of_the_capture_are_the_ones_it_is_given(monkeypatch):
    seen = {}

    def fake_capture(client, graph, **options):
        seen.update(options, client=client, graph=graph)
        return snapshot_of(1, Clock())

    monkeypatch.setattr(Snapshot, "capture", staticmethod(fake_capture))
    live = LiveSnapshot(None, "g", refresh_every=EVERY, sample=None, types=True)

    live._capture()

    assert seen == {"sample": None, "types": True, "client": None, "graph": "g"}


# --------------------------------------------------------------------- used by the validators


def test_a_validation_sees_one_snapshot_though_a_refresh_lands_during_it(
    clock, database
):
    live = live_snapshot(clock, database)
    live.current()
    database.version = 2
    clock.advance(minutes=11)
    live.hold = "now"  # the refresh runs the moment it starts, inside `current()`
    validator = SchemaValidator(live)

    during = validator.validate("MATCH (n:V1) RETURN n")
    after = validator.validate("MATCH (n:V1) RETURN n")

    assert during == (1.0, [])  # the snapshot it was given when it started: V1 exists
    assert (
        after[1][0]["label"] == "V1"
    )  # the refresh has landed: V2 is the graph now, and V1 is gone
    assert database.capture_calls == 2


def test_the_validators_take_a_live_snapshot_as_driver_and_as_snapshot(clock, database):
    live = live_snapshot(clock, database)

    assert SchemaValidator(live).mode == "offline"
    assert SchemaValidator(live).validate("MATCH (n:V1) RETURN n") == (1.0, [])
    assert SchemaValidator(live).validate("MATCH (n:V1) RETURN n", "g") == (1.0, [])
    with pytest.raises(ValueError, match="'g'"):
        SchemaValidator(live).validate("MATCH (n:V1) RETURN n", "other")


# --------------------------------------------------------------- several threads, for real, and FalkorDB


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


# ------------------------------------------------------------------------------------------------ async


class AsyncDatabase(Database):
    async def fingerprint(self):
        return Database.fingerprint(self)

    async def capture(self):
        return Database.capture(self)


class FakeAsyncLive(AsyncLiveSnapshot):
    """An ``AsyncLiveSnapshot`` that looks at a fake database."""

    def __init__(self, database, clock, **options):
        options.setdefault("refresh_every", EVERY)
        super().__init__(None, "g", clock=clock, **options)
        self.database = database

    async def _fingerprint(self):
        return await self.database.fingerprint()

    async def _capture(self):
        return await self.database.capture()


def async_live(clock, database, **options):
    return FakeAsyncLive(database, clock, **options)


async def settle(live):
    """Let the refresh a validation started finish."""
    while live._task is not None and not live._task.done():
        await live._task


def test_the_async_class_has_nothing_to_serve_until_it_is_refreshed(clock):
    live = async_live(clock, AsyncDatabase(clock))

    with pytest.raises(RuntimeError, match="no snapshot yet"):
        live.current()


def test_the_async_class_captures_on_the_first_refresh_and_serves_at_once_after(clock):
    database = AsyncDatabase(clock)
    live = async_live(clock, database)

    async def go():
        snapshot = await live.refresh()
        return snapshot, live.current(), live.age

    snapshot, current, age = asyncio.run(go())
    assert current is snapshot
    assert version_of(snapshot) == 1
    assert age == timedelta(0)
    assert database.capture_calls == 1


def test_async_with_makes_the_first_snapshot(clock):
    database = AsyncDatabase(clock)

    async def go():
        async with async_live(clock, database) as live:
            return live.current()

    assert version_of(asyncio.run(go())) == 1


def test_the_async_class_refreshes_in_a_task_and_serves_the_old_snapshot_meanwhile(
    clock,
):
    database = AsyncDatabase(clock)
    live = async_live(clock, database)

    async def go():
        first = await live.refresh()
        database.version = 2
        clock.advance(minutes=11)
        served = live.current()
        scheduled = live._task is not None
        assert served is first  # the old one, at once
        await settle(live)
        return scheduled, version_of(live.current())

    assert asyncio.run(go()) == (True, 2)


def test_two_uses_start_one_refresh_task(clock):
    database = AsyncDatabase(clock)
    live = async_live(clock, database)

    async def go():
        await live.refresh()
        clock.advance(minutes=11)
        for _ in range(5):
            live.current()
        await settle(live)

    asyncio.run(go())

    assert database.fingerprint_calls == 2  # the first refresh, and one task


def test_a_second_async_refresh_starts_once_the_first_is_done(clock):
    database = AsyncDatabase(clock)
    live = async_live(clock, database)

    async def go():
        await live.refresh()
        for _ in range(3):
            clock.advance(minutes=11)
            live.current()
            await settle(live)

    asyncio.run(go())

    assert (
        database.fingerprint_calls == 4
    )  # the first refresh, and one for each of the three


def test_a_refresh_task_cancelled_before_it_ran_does_not_stop_the_next_from_starting(
    clock,
):
    database = AsyncDatabase(clock)
    live = async_live(clock, database)

    async def go():
        await live.refresh()
        clock.advance(minutes=11)
        live.current()
        assert live._task is not None
        live._task.cancel()  # before it has run a line
        with pytest.raises(asyncio.CancelledError):
            await live._task
        live.current()
        await settle(live)

    asyncio.run(go())

    assert (
        database.fingerprint_calls == 2
    )  # the cancelled one never looked; the next one did


def test_a_manual_async_refresh_in_flight_means_no_second_one_is_started(clock):
    class Blocking(AsyncDatabase):
        def __init__(self, clock):
            super().__init__(clock)
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def fingerprint(self):
            self.entered.set()
            await self.release.wait()
            return Database.fingerprint(self)

    database = Blocking(clock)
    live = async_live(clock, database)

    async def go():
        database.release.set()
        await live.refresh()  # the first snapshot
        database.release.clear()
        database.entered.clear()
        clock.advance(minutes=11)
        manual = asyncio.create_task(live.refresh())
        await database.entered.wait()  # it is in the middle of looking
        live.current()  # a refresh is due, but one is running
        started = live._task
        database.release.set()
        await manual
        return started

    assert asyncio.run(go()) is None


def test_the_async_class_looks_before_it_captures(clock):
    database = AsyncDatabase(clock)
    live = async_live(clock, database)

    async def go():
        first = await live.refresh()
        clock.advance(minutes=11)
        live.current()
        await settle(live)
        return first, live.current()

    first, current = asyncio.run(go())
    assert current is first
    assert database.capture_calls == 1
    assert database.fingerprint_calls == 2


def test_an_async_refresh_that_fails_keeps_the_snapshot_and_waits_to_retry(clock):
    database = AsyncDatabase(clock)
    live = async_live(clock, database, retry_after=timedelta(seconds=30))

    async def go():
        first = await live.refresh()
        clock.advance(minutes=11)
        database.failing = TimeoutError("slow")
        live.current()
        await settle(live)
        kept = live.current() is first
        error = live.last_error
        before = database.fingerprint_calls
        live.current()
        await settle(live)
        return kept, error, database.fingerprint_calls - before

    kept, error, extra = asyncio.run(go())
    assert kept
    assert isinstance(error, TimeoutError)
    assert extra == 0  # not before retry_after


def test_a_manual_async_refresh_raises_and_keeps_the_snapshot(clock):
    database = AsyncDatabase(clock)
    live = async_live(clock, database)

    async def go():
        first = await live.refresh()
        database.failing = ConnectionError("down")
        with pytest.raises(ConnectionError):
            await live.refresh()
        return first is live.current()

    assert asyncio.run(go())


def test_async_max_age(clock):
    database = AsyncDatabase(clock)
    live = async_live(clock, database, max_age=timedelta(hours=1))

    async def go():
        await live.refresh()
        database.failing = ConnectionError("down")
        clock.advance(hours=2)
        with pytest.raises(StaleSnapshotError):
            live.current()
        await settle(live)
        clock.advance(minutes=1)
        with pytest.raises(StaleSnapshotError):
            live.current()

    asyncio.run(go())


def test_the_async_class_reads_and_writes_the_file(clock, tmp_path):
    path = tmp_path / "kg.json"
    snapshot_of(7, clock).save(path)
    database = AsyncDatabase(clock)
    live = async_live(clock, database, path=path)

    async def go():
        warm = live.current()  # no await needed: it came from the file
        database.version = 2
        await live.refresh()
        return warm

    assert version_of(asyncio.run(go())) == 7
    assert version_of(Snapshot.load(path)) == 2


def test_closing_the_async_class_cancels_a_running_refresh_and_starts_none(clock):
    database = AsyncDatabase(clock)
    live = async_live(clock, database)

    async def go():
        await live.refresh()
        clock.advance(minutes=11)
        live.current()
        live.close()
        with pytest.raises(asyncio.CancelledError):
            await live._task
        clock.advance(minutes=11)
        live._task = None
        live.current()
        return live._task

    assert asyncio.run(go()) is None


def test_the_async_validators_follow_the_live_snapshot(clock):
    database = AsyncDatabase(clock)
    live = async_live(clock, database)
    validator = AsyncSchemaValidator(live)

    async def go():
        await live.refresh()
        first = await validator.validate("MATCH (n:V1) RETURN n")
        database.version = 2
        clock.advance(minutes=11)
        during = await validator.validate("MATCH (n:V1) RETURN n")
        await settle(live)
        after = await validator.validate("MATCH (n:V1) RETURN n")
        return first, during, after

    first, during, after = asyncio.run(go())
    assert first == (1.0, [])
    assert during == (
        1.0,
        [],
    )  # the refresh started during it; it kept its own snapshot
    assert after[1][0]["label"] == "V1"


def test_with_a_real_async_database_the_live_snapshot_follows_the_graph(
    run_async, clock
):
    async def go(client):
        graph = client.select_graph("live_async_lab")
        await graph.query("CREATE (:A {p: 1})")
        try:
            async with AsyncLiveSnapshot(
                client, "live_async_lab", refresh_every=EVERY, clock=clock
            ) as live:
                assert live.current().labels == ("A",)
                await graph.query("CREATE (:B)")
                await live.refresh()
                return live.current().labels, await AsyncSchemaValidator(live).validate(
                    "MATCH (n:B) RETURN n"
                )
        finally:
            await graph.delete()

    labels, validated = run_async(go)
    assert labels == ("A", "B")
    assert validated == (1.0, [])


# ---------------------------------------------------------------------------------- the fingerprint


@pytest.fixture
def fingerprinted(falkordb_client):
    from cypher_guardrail._internal.capture_driver import fingerprint

    graph = falkordb_client.select_graph("live_fingerprint")
    graph.query("CREATE (:A {p: 1})-[:R]->(:B {q: 1}), (:A {p: 2})")
    yield graph, lambda: fingerprint(falkordb_client, "live_fingerprint")
    graph.delete()


def test_an_unchanged_graph_has_the_same_fingerprint(fingerprinted):
    _, fingerprint = fingerprinted

    assert fingerprint() == fingerprint()
    assert fingerprint()[4:] == (3, 1)  # nodes, relationships


@pytest.mark.parametrize(
    "change",
    [
        "CREATE (:A {p: 3})",  # a node more, nothing else new: only the number of nodes
        "MATCH (a:A {p: 1}), (b:B) CREATE (a)-[:R]->(b)",  # a relationship more: only its number
        "CREATE (:C)",  # a label
        "MATCH (a:A {p: 1}), (b:B) CREATE (a)-[:S]->(b)",  # a relationship type
        "MATCH (a:A {p: 2}) SET a.fresh = 1",  # a property key
        "CREATE INDEX FOR (n:A) ON (n.p)",  # an index
        "MATCH (a:A {p: 2}) DELETE a",  # a node less
    ],
)
def test_each_part_of_the_fingerprint_notices_its_own_change(fingerprinted, change):
    graph, fingerprint = fingerprinted
    before = fingerprint()

    graph.query(change)

    assert fingerprint() != before


def test_the_fingerprint_of_a_graph_that_does_not_exist_is_empty(falkordb_client):
    from cypher_guardrail._internal.capture_driver import fingerprint

    assert fingerprint(falkordb_client, "live_no_such_graph") == ((), (), (), (), 0, 0)


def test_removing_one_relationship_and_adding_another_is_not_noticed(fingerprinted):
    # the documented limit of a fingerprint: the counts are the same; `refresh(force=True)` and `full_every` exist for it
    graph, fingerprint = fingerprinted
    graph.query("MATCH (:A)-[r:R]->(:B) DELETE r")
    graph.query("MATCH (a:A {p: 2}), (b:B) CREATE (a)-[:R]->(b)")
    before = fingerprint()
    graph.query("MATCH (:A {p: 2})-[r:R]->(:B) DELETE r")
    graph.query("MATCH (a:A {p: 1}), (b:B) CREATE (a)-[:R]->(b)")

    assert fingerprint() == before


def test_the_async_fingerprint_is_the_same(falkordb_client, run_async, fingerprinted):
    from cypher_guardrail._internal.capture_driver import (
        fingerprint,
        fingerprint_async,
    )

    async def ask(client):
        return await fingerprint_async(client, "live_fingerprint")

    assert run_async(ask) == fingerprint(falkordb_client, "live_fingerprint")


# --------------------------------------------------------------- what a file or a thread can get wrong


def test_a_date_without_a_zone_in_the_file_is_taken_as_utc(clock, database, tmp_path):
    path = tmp_path / "kg.json"
    data = snapshot_of(7, clock).to_dict()
    data["captured_at"] = "2026-10-19T11:50:00"  # ten minutes before the clock, no zone
    path.write_text(json.dumps(data))

    live = live_snapshot(clock, database, path=path)

    assert version_of(live.current()) == 7  # it used to raise TypeError, for ever
    assert live.age == timedelta(minutes=10)
    assert len(live.pending) == 1  # and ten minutes is the time to refresh


def test_a_date_in_the_future_in_the_file_is_not_a_negative_age(
    clock, database, tmp_path
):
    path = tmp_path / "kg.json"
    data = snapshot_of(7, clock).to_dict()
    data["captured_at"] = "2999-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data))

    live = live_snapshot(clock, database, path=path, max_age=timedelta(hours=1))

    assert live.age == timedelta(0)
    assert version_of(live.current()) == 7
    clock.advance(hours=2)
    with pytest.raises(StaleSnapshotError):
        live.current()  # a snapshot from the future does not stay fresh for a thousand years


@pytest.mark.parametrize(
    "broken",
    [{"indexes": [1]}, {"labels": 5}, {"edges": [[1]]}, {"graph": 5}],
)
def test_a_file_that_is_a_snapshot_of_the_wrong_kind_does_not_stop_the_live_snapshot(
    clock, database, tmp_path, broken
):
    path = tmp_path / "kg.json"
    path.write_text(json.dumps({**snapshot_of(7, clock).to_dict(), **broken}))

    live = live_snapshot(clock, database, path=path)  # it used to raise TypeError here

    assert isinstance(live.last_error, ValueError)
    assert version_of(live.current()) == 1  # captured instead


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


@pytest.mark.parametrize(
    ("options", "error"),
    [
        (
            {"max_age": EVERY},
            ValueError,
        ),  # not confirmed for as long as it takes to be due: no refresh was tried
        ({"max_age": timedelta(minutes=5)}, ValueError),
        ({"max_age": timedelta(0)}, ValueError),
        ({"full_every": timedelta(0)}, ValueError),
    ],
)
def test_options_that_contradict_each_other_are_refused(options, error):
    build = cast("Callable[..., object]", LiveSnapshot)
    with pytest.raises(error):
        build(None, "g", **{"refresh_every": EVERY, **options})


def test_a_max_age_longer_than_refresh_every_is_accepted_and_no_retry_wait_is_too():
    LiveSnapshot(None, "g", refresh_every=EVERY, max_age=EVERY + timedelta(seconds=1))
    LiveSnapshot(None, "g", refresh_every=EVERY, retry_after=timedelta(0))


# ------------------------------------------------------------- the first snapshot, when the database is down


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


# ------------------------------------------------------------------------------------------------- fork


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


# ----------------------------------------------------------------------------- several event loops


class Yielding(AsyncDatabase):
    """A database that gives up the event loop while it is asked, so that two refreshes overlap."""

    async def fingerprint(self):
        await asyncio.sleep(0)
        return Database.fingerprint(self)


def test_a_live_snapshot_goes_on_refreshing_after_the_event_loop_it_was_used_in_was_dropped(
    clock, monkeypatch
):
    database = Yielding(clock)
    live = async_live(clock, database)
    original = live._fingerprint
    gate = []

    async def stuck():
        gate.append(1)
        await asyncio.Event().wait()  # never set: this refresh does not end
        return await original()

    async def first_loop():
        await live.refresh()
        clock.advance(minutes=11)
        monkeypatch.setattr(live, "_fingerprint", stuck)
        live.current()  # starts a refresh task that never ends
        await asyncio.sleep(0)
        assert gate

    loop = asyncio.new_event_loop()
    loop.run_until_complete(first_loop())
    loop.close()  # dropped with the task still pending (asyncio.run would have cancelled it)
    monkeypatch.undo()
    before = database.fingerprint_calls

    async def second_loop():
        live.current()  # a refresh is due, and the task of the dropped loop must not be waited for
        task = live._task
        assert task is not None
        await task

    asyncio.run(second_loop())

    assert database.fingerprint_calls == before + 1


def test_the_lock_of_one_event_loop_is_not_used_in_another(clock):
    database = Yielding(clock)
    live = async_live(clock, database)

    async def two_refreshes_at_once():
        await asyncio.gather(live.refresh(), live.refresh())

    asyncio.run(two_refreshes_at_once())
    asyncio.run(
        two_refreshes_at_once()
    )  # the lock was bound to the first loop, and this one would not take it

    assert database.fingerprint_calls == 4


def test_an_async_live_snapshot_used_in_a_thread_with_no_loop_does_not_refresh_and_says_so_in_its_docs():
    doc = " ".join((AsyncLiveSnapshot.__doc__ or "").split())  # a line break is a space

    assert "one event loop at a time" in doc
    assert "outside an event loop" in doc


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_forked_child_does_not_inherit_the_event_loop_state_of_its_parent(clock):
    database = Yielding(clock)
    live = async_live(clock, database)

    async def use_it():
        await live.refresh()
        clock.advance(minutes=11)
        live.current()  # a refresh task, in this loop
        assert live._task is not None
        await live._task

    asyncio.run(use_it())
    assert live._loop is not None and live._lock is not None and live._task is not None
    read, write = os.pipe()

    pid = os.fork()
    if pid == 0:
        try:
            state = (live._loop, live._lock, live._task)
            os.write(write, b"reset" if state == (None, None, None) else b"inherited")
        finally:
            os._exit(0)

    os.close(write)
    answer = os.read(read, 50)
    os.close(read)
    os.waitpid(pid, 0)

    assert answer == b"reset"


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
