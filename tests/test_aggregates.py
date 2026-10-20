"""Aggregates that need numbers, called on a property that has none (``PropertiesValidator(check_types=True)``).

Unlike a comparison with a literal of another type, which finds nothing, FalkorDB fails when it runs ``sum(n.name)``
for a ``name`` of strings: ``Type mismatch: expected Integer, Float, or Null but was String``.
"""

import typing

import pytest

from cypher_guardrail import AsyncPropertiesValidator, PropertiesValidator, Snapshot
from cypher_guardrail import issues as issue_types
from cypher_guardrail._internal.core import literals
from cypher_guardrail.issues import AGGREGATE_TYPE_MISMATCH


def found(query):
    return [(a.function, a.variable, a.property) for a in literals.aggregates(query)]


# ----------------------------------------------------------------------------------------------------
# Finding them


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("RETURN sum(n.price)", [("sum", "n", "price")]),
        (
            "RETURN SUM(n.price), Avg(n.price)",
            [("sum", "n", "price"), ("avg", "n", "price")],
        ),
        ("RETURN sum(DISTINCT n.price)", [("sum", "n", "price")]),
        ("RETURN stDev(n.a), stdevp(n.a)", [("stDev", "n", "a"), ("stDevP", "n", "a")]),
        ("RETURN percentileCont(n.a, 0.5)", [("percentileCont", "n", "a")]),
        ("RETURN percentileDisc(n.a, 0.9)", [("percentileDisc", "n", "a")]),
        ("RETURN sum( n . price )", [("sum", "n", "price")]),
        ("RETURN sum(`n`.`price`)", [("sum", "n", "price")]),
        ("RETURN sum(n.a), sum(n.a)", [("sum", "n", "a")]),  # each once
        ("WITH sum(n.a) AS s RETURN s", [("sum", "n", "a")]),
        # not an aggregate that needs numbers, or not a property by itself
        ("RETURN min(n.a), max(n.a), count(n.a), collect(n.a)", []),
        ("RETURN sum(n.a + 1)", []),
        ("RETURN sum(1 + n.a)", []),
        ("RETURN sum(toInteger(n.a))", []),
        ("RETURN sum(n.a.b)", []),
        ("RETURN sum(n)", []),
        ("RETURN sum(1)", []),
        ("RETURN lib.sum(n.a)", []),
        ("RETURN sum", []),
        ("RETURN 'sum(n.a)'", []),
        ("MATCH (n) SET n.t = sum(n.a)", []),  # a clause that writes
    ],
)
def test_the_aggregates_that_need_numbers_on_a_property(query, expected):
    assert found(query) == expected


# ----------------------------------------------------------------------------------------------------
# What each aggregate does with strings, on the engine of the tests


@pytest.mark.parametrize(
    ("function", "fails"),
    [(name, True) for name in ("sum", "avg", "stDev", "stDevP")]
    + [("percentileCont", True), ("percentileDisc", True)]
    + [(name, False) for name in ("min", "max", "count", "collect")],
)
def test_the_aggregates_that_are_looked_at_are_the_ones_that_fail_on_strings(
    graph, function, fails
):
    assert (function in set(literals.NUMERIC_AGGREGATES.values())) is fails
    argument = "n.title, 0.5" if function.startswith("percentile") else "n.title"
    query = f"MATCH (n:Goal) RETURN {function}({argument})"

    if fails:
        with pytest.raises(Exception, match="Type mismatch"):
            graph.query(query)
    else:
        graph.query(query)


# ----------------------------------------------------------------------------------------------------
# Reporting them


def issues_of(client, query, **options):
    return PropertiesValidator(client, check_types=True, **options).validate(
        query, "kg"
    )[1]


def mismatches(found):
    return [i for i in found if i["code"] == AGGREGATE_TYPE_MISMATCH]


def test_a_sum_of_strings_is_reported_with_what_it_is_called_on(falkordb_client):
    (issue,) = mismatches(
        issues_of(falkordb_client, "MATCH (g:Goal) RETURN sum(g.title)")
    )

    assert (
        issue.get("function"),
        issue.get("kind"),
        issue.get("name"),
        issue.get("property"),
        issue.get("types"),
    ) == (
        "sum",
        "node",
        "Goal",
        "title",
        ["String"],
    )
    assert "Type mismatch" in issue["description"]


def test_a_property_that_has_numbers_is_fine(falkordb_client):
    # `weight` of HAS_INDICATOR is a number
    query = "MATCH ()-[r:HAS_INDICATOR]->() RETURN sum(r.weight), avg(r.weight)"

    assert mismatches(issues_of(falkordb_client, query)) == []


def test_the_property_of_a_relationship_is_checked_as_one(falkordb_client):
    query = "MATCH (g:Goal)-[r:HAS_TARGET]->(t:Target) RETURN sum(t.text)"

    (issue,) = mismatches(issues_of(falkordb_client, query))

    assert (issue.get("kind"), issue.get("name")) == ("node", "Target")


def test_it_is_off_unless_the_types_are_checked(falkordb_client):
    query = "MATCH (g:Goal) RETURN sum(g.title)"

    assert PropertiesValidator(falkordb_client).validate(query, "kg")[1] == []


def test_a_variable_whose_label_is_inferred_is_left_alone(falkordb_client):
    # no label in the query: a guess is never reported
    assert (
        mismatches(
            issues_of(
                falkordb_client, "MATCH (g)-[:HAS_TARGET]->(t) RETURN sum(t.text)"
            )
        )
        == []
    )


def test_the_score_does_not_change(falkordb_client):
    query = "MATCH (g:Goal) RETURN sum(g.title)"

    with_check = PropertiesValidator(falkordb_client, check_types=True).validate(
        query, "kg"
    )
    without = PropertiesValidator(falkordb_client).validate(query, "kg")

    assert with_check[0] == without[0] == 1.0


def test_the_async_validator_says_the_same(falkordb_client, run_async):
    query = "MATCH (g:Goal) RETURN sum(g.title)"

    async def ask(client):
        return (
            await AsyncPropertiesValidator(client, check_types=True).validate(
                query, "kg"
            )
        )[1]

    assert run_async(ask) == issues_of(falkordb_client, query)


def test_offline_the_snapshot_answers_when_it_has_the_types(falkordb_client):
    snapshot = Snapshot.capture(falkordb_client, "kg", types=True)
    query = "MATCH (g:Goal) RETURN sum(g.title)"

    offline = PropertiesValidator(snapshot, check_types=True).validate(query)[1]

    assert [i["code"] for i in offline] == [AGGREGATE_TYPE_MISMATCH]


def test_offline_a_snapshot_without_the_types_says_nothing(falkordb_client):
    snapshot = Snapshot.capture(falkordb_client, "kg")

    assert (
        PropertiesValidator(snapshot, check_types=True).validate(
            "MATCH (g:Goal) RETURN sum(g.title)"
        )[1]
        == []
    )


def test_hybrid_a_mismatch_of_the_snapshot_is_checked_with_the_database(
    falkordb_client,
):
    # the snapshot says `weight` holds strings (it did, when it was taken); it holds numbers now
    stale = Snapshot.from_schema(
        nodes={"Goal": [], "Indicator": []},
        relationships=[("Goal", "HAS_INDICATOR", "Indicator")],
        relationship_properties={"HAS_INDICATOR": ["weight"]},
        relationship_property_types={"HAS_INDICATOR": {"weight": ["String"]}},
    )
    validator = PropertiesValidator(falkordb_client, snapshot=stale, check_types=True)

    found = validator.validate(
        "MATCH ()-[r:HAS_INDICATOR]->() RETURN sum(r.weight)", "kg"
    )[1]

    assert found == []  # the database corrected the snapshot
    assert validator.stats.corrected_misses == 1


def test_the_issue_has_the_fields_of_its_type(falkordb_client):
    (issue,) = mismatches(
        issues_of(falkordb_client, "MATCH (g:Goal) RETURN sum(g.title)")
    )

    assert set(issue) == set(
        typing.get_type_hints(issue_types.AggregateTypeMismatchIssue)
    )
    assert issue_types.AggregateTypeMismatchIssue in typing.get_args(issue_types.Issue)
