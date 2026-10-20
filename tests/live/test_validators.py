"""A live snapshot given to the validators: one snapshot for a whole validation."""

import pytest
from live_helpers import live_snapshot

from cypher_guardrail import (
    SchemaValidator,
)


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
