"""When a live snapshot refreshes: what is served meanwhile, what the fingerprint decides, ``full_every``, and what a failure, ``retry_after`` and ``max_age`` do."""

from datetime import timedelta

import pytest
from live_helpers import live_snapshot, run_pending, version_of

from cypher_guardrail import (
    StaleSnapshotError,
)


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
