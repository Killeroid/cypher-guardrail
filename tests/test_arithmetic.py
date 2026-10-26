"""A division or a modulo by a zero that is in the text of the query (no database needed)."""

import pytest

from cypher_guardrail._internal.core.arithmetic import divides_by_zero

ALWAYS_FAILS = [
    "RETURN 1/0",
    "RETURN 1 / 0",
    "RETURN 1/(0)",
    "RETURN 1/(1-1)",
    "RETURN 1/(2*0)",
    "RETURN 1/( 3 - 2 - 1 )",
    "RETURN 10/(5-5)*2",
    "RETURN 5 % 0",
    "RETURN 5 % (1-1)",
    "RETURN 7 % (3 - 3) AS r",
    "RETURN 1/(5/10)",  # an integer division truncates: the divisor is 0
    "RETURN 1/(1%1)",
    "RETURN 1/-0",
    "RETURN 1/(-0)",
    "RETURN 0/0",
    "MATCH (p) RETURN p.age / 0",  # the type of p.age is not known: an integer fails
    "MATCH (p) RETURN p.age / (1-1)",
    "RETURN (1+2)/0",
    "RETURN 8 / 2 / 0",
    "RETURN [x IN [1,2] | x / 0]",
    "RETURN 1/0 AS a, 1/2 AS b",
]

NEVER_FAILS = [
    "RETURN 1/2",
    "RETURN 10 / 0.5",
    "RETURN 100 / 10",
    "RETURN 1/(2-1)",
    "RETURN 1/(1+1)",
    "RETURN 5 % 3",
    "RETURN 5 % (4-1)",
    "RETURN 1/(10/5)",
    "RETURN 1/0.5",
    "RETURN 1/abs(0)",  # a call: not looked into
    "RETURN 1/(x-x)",  # not numbers
    "RETURN 1/x",
    "MATCH (p) RETURN p.age / (p.age - p.age)",
    "RETURN 1/0^x",
    "RETURN 1/0abc",
    "RETURN 1/0.x",
    "RETURN 1/0(",
    # a decimal gives inf or nan in FalkorDB: it is no error
    "RETURN 1.0/0",
    "RETURN 1/0.0",
    "RETURN 1.0/0.0",
    "RETURN 1/(0.0)",
    "RETURN 1.0/(1-1)",
    "RETURN 1/(1.0-1.0)",
    "RETURN 1/(1-1.0)",
    "RETURN 5.0 % 0",
    "RETURN 5 % 0.0",
    "RETURN .5/0",
    "RETURN 1e3/0",
    "RETURN 1/0e0",
    "RETURN 1/(1/0.0)",
    "RETURN 1",
    "RETURN 1/",
    "RETURN 1/(",
]


@pytest.mark.parametrize("query", ALWAYS_FAILS)
def test_a_divisor_that_is_an_integer_zero_is_found(query):
    assert divides_by_zero(query) is True


@pytest.mark.parametrize("query", NEVER_FAILS)
def test_anything_else_is_left_alone(query):
    assert divides_by_zero(query) is False
