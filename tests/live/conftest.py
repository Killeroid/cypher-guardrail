"""The clock and the fake database of the tests of the live snapshots."""

import pytest
from live_helpers import Clock, Database


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def database(clock):
    return Database(clock)
