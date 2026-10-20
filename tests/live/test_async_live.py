"""``AsyncLiveSnapshot``: the refresh is an asyncio task, the lock belongs to an event loop, and a loop that was dropped."""

import asyncio
import os
from datetime import timedelta

import pytest
from live_helpers import (
    EVERY,
    AsyncDatabase,
    Database,
    async_live,
    settle,
    snapshot_of,
    version_of,
)

from cypher_guardrail import (
    AsyncLiveSnapshot,
    AsyncSchemaValidator,
    Snapshot,
    StaleSnapshotError,
)


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
