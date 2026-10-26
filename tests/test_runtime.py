"""Queries that FalkorDB accepts and fails to run: ``SyntaxValidator(check_runtime=True)``.

The labelled cases are in ``tests/accuracy/runtime.json``; these tests are about the option, the fields and what is
not reported."""

import typing

import pytest

from cypher_guardrail import AsyncSyntaxValidator, SyntaxValidator
from cypher_guardrail import issues as issue_types
from cypher_guardrail.issues import ARITHMETIC_ERROR, FAILS_WHEN_RUN

DIVISION = "MATCH ()-[r:HAS_INDICATOR]->() RETURN 1 / (r.weight - r.weight)"
TYPE_MISMATCH = "MATCH (g:Goal) RETURN sum(g.title)"


def codes(found):
    return [issue["code"] for issue in found]


def test_it_is_off_by_default(falkordb_client):
    assert SyntaxValidator(falkordb_client).validate(TYPE_MISMATCH, "kg") == (True, [])


def test_a_failure_on_the_data_is_a_warning_that_has_the_message_of_the_database(
    falkordb_client,
):
    valid, found = SyntaxValidator(falkordb_client, check_runtime=True).validate(
        TYPE_MISMATCH, "kg"
    )

    assert valid is True
    (issue,) = found
    assert issue["code"] == FAILS_WHEN_RUN
    message = issue["message"]
    assert message.startswith("Type mismatch")
    assert message in issue["description"]
    assert set(issue) == set(typing.get_type_hints(issue_types.FailsWhenRunIssue))


def test_the_async_validator_reports_the_same(falkordb_client, run_async):
    expected = SyntaxValidator(falkordb_client, check_runtime=True).validate(
        DIVISION, "kg"
    )

    async def ask(client):
        return await AsyncSyntaxValidator(client, check_runtime=True).validate(
            DIVISION, "kg"
        )

    assert run_async(ask) == expected
    assert codes(expected[1]) == [FAILS_WHEN_RUN]


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (g:Goal) RETURN g.title",
        "MATCH (g:Goal) RETURN count(g)",
        "MATCH (g:Ghost) RETURN g.x / (g.x - g.x)",  # no row to fail on
    ],
)
def test_a_query_that_runs_has_no_warning(falkordb_client, query):
    validator = SyntaxValidator(falkordb_client, check_runtime=True)

    assert validator.validate(query, "kg") == (True, [])


def test_a_parameter_that_has_no_value_is_not_a_failure_of_the_query(falkordb_client):
    query = "MATCH (g:Goal) WHERE g.title = $title RETURN g"

    found = SyntaxValidator(falkordb_client, check_runtime=True).validate(query, "kg")[
        1
    ]

    assert FAILS_WHEN_RUN not in codes(found)


@pytest.mark.parametrize("query", ["RETURN 1 / 0", "RETURN 1 / (2 - 2)"])
def test_a_division_by_zero_in_the_text_is_reported_once(falkordb_client, query):
    # FalkorDB 4 refuses it when it plans the query, FalkorDB 6 when it runs it
    found = SyntaxValidator(falkordb_client, check_runtime=True).validate(query, "kg")[
        1
    ]

    assert codes(found) == [ARITHMETIC_ERROR]


def test_a_query_that_does_not_parse_is_a_syntax_error_and_nothing_else(
    falkordb_client,
):
    found = SyntaxValidator(falkordb_client, check_runtime=True).validate(
        "MATCH (g:Goal RETURN g", "kg"
    )[1]

    assert codes(found) == [issue_types.SYNTAX_ERROR]
