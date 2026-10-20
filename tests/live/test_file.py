"""The file of ``path=``: a restart is warm, every capture is written atomically, and what is wrong with the file is reported and not used."""

import json
import os
from datetime import timedelta

import pytest
from live_helpers import live_snapshot, run_pending, snapshot_of, version_of

from cypher_guardrail import (
    Snapshot,
    StaleSnapshotError,
)


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
