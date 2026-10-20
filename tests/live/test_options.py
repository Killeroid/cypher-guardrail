"""The options of ``LiveSnapshot``, ``close()`` and the context manager."""

from datetime import timedelta
from typing import TYPE_CHECKING, cast

import pytest

from cypher_guardrail import (
    LiveSnapshot,
    Snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Callable
from live_helpers import EVERY, Clock, live_snapshot, snapshot_of


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
        LiveSnapshot(None, graph, refresh_every=EVERY)  # ty: ignore[invalid-argument-type]


def test_the_options_of_the_capture_are_the_ones_it_is_given(monkeypatch):
    seen = {}

    def fake_capture(client, graph, **options):
        seen.update(options, client=client, graph=graph)
        return snapshot_of(1, Clock())

    monkeypatch.setattr(Snapshot, "capture", staticmethod(fake_capture))
    live = LiveSnapshot(None, "g", refresh_every=EVERY, sample=None, types=True)  # ty: ignore[invalid-argument-type]

    live._capture()

    assert seen == {
        "sample": None,
        "types": True,
        "udfs": True,
        "client": None,
        "graph": "g",
    }


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
    LiveSnapshot(None, "g", refresh_every=EVERY, max_age=EVERY + timedelta(seconds=1))  # ty: ignore[invalid-argument-type]
    LiveSnapshot(None, "g", refresh_every=EVERY, retry_after=timedelta(0))  # ty: ignore[invalid-argument-type]
