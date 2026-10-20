"""The fakes that the tests of the live snapshots share: a clock, a database that can fail, and live snapshots that look at them."""

import dataclasses
from datetime import UTC, datetime, timedelta

from cypher_guardrail import (
    AsyncLiveSnapshot,
    LiveSnapshot,
    Snapshot,
)

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
        super().__init__(None, "g", clock=clock, **options)  # ty: ignore[invalid-argument-type]
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


def version_of(snapshot):
    (label,) = snapshot.labels
    return int(label[1:])


def run_pending(live):
    while live.pending:
        live.pending.pop(0)()


class AsyncDatabase(Database):
    async def fingerprint(self):
        return Database.fingerprint(self)

    async def capture(self):
        return Database.capture(self)


class FakeAsyncLive(AsyncLiveSnapshot):
    """An ``AsyncLiveSnapshot`` that looks at a fake database."""

    def __init__(self, database, clock, **options):
        options.setdefault("refresh_every", EVERY)
        super().__init__(None, "g", clock=clock, **options)  # ty: ignore[invalid-argument-type]
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
