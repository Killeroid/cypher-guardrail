"""Calls of functions with a dotted name (``library.function``): which are found in a query, which are built in, and
what the three modes say of the ones the instance does not have (``Warning.UnknownFunction``)."""

import pytest

from cypher_guardrail import (
    AsyncSchemaValidator,
    SchemaValidator,
    Snapshot,
)
from cypher_guardrail._internal.core import functions
from cypher_guardrail._internal.core.errors import CypherError
from cypher_guardrail._internal.core.io import run_sync
from cypher_guardrail._internal.core.queries import missing_functions
from cypher_guardrail._internal.sources.online import functions_missing, resolve
from cypher_guardrail.issues import UNKNOWN_FUNCTION

CODE = "function boost(x) { return x * 2; }\nfalkor.register('boost', boost);"


@pytest.fixture
def scoring(falkordb_client):
    falkordb_client.execute_command("GRAPH.UDF", "LOAD", "REPLACE", "scoring", CODE)
    yield
    falkordb_client.execute_command("GRAPH.UDF", "DELETE", "scoring")


def names(issues):
    return [i["name"] for i in issues if i["code"] == UNKNOWN_FUNCTION]


# ----------------------------------------------------------------------------------------------------
# Finding the calls


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("RETURN scoring.boost(1)", ["scoring.boost"]),
        ("RETURN scoring . boost (1)", ["scoring.boost"]),
        ("RETURN `scoring`.boost(1)", ["scoring.boost"]),
        ("RETURN `my lib`.boost(1)", ["my lib.boost"]),
        ("RETURN a.b.c(1)", ["a.b.c"]),
        (
            "RETURN SCORING.boost(1)",
            ["SCORING.boost"],
        ),  # the names of UDFs are case-sensitive
        ("RETURN x.f(1), y.g(2), x.f(3)", ["x.f", "y.g"]),  # each once, in order
        (
            "MATCH (m) RETURN scoring.boost(m.rating)",
            ["scoring.boost"],
        ),  # m.rating is a property
        ("RETURN toUpper('a'), foo(1)", []),  # no dot: the database's business
        ("RETURN vec.cosineDistance(a, b)", []),  # built in
        ("RETURN VEC.cosinedistance(a, b), List.Sort(x)", []),  # in any case
        ("CALL db.labels() YIELD label RETURN label", []),  # a procedure
        ("CALL  \n apoc.x.y(1)", []),
        ("RETURN 'scoring.boost(1)' // geo.near(1)", []),  # a string and a comment
        (
            "MATCH (n) WHERE n.age > 1.5 AND (n.x < 2) RETURN n",
            [],
        ),  # a number, a property, a parenthesis
        ("MATCH (n) RETURN n.name", []),
        ("RETURN $p.f(1)", []),  # a parameter is not a library
    ],
)
def test_the_calls_with_a_dotted_name_that_are_not_built_in(query, expected):
    assert functions.called(query) == expected


def test_the_built_in_functions_with_a_dot_are_the_ones_the_engine_lists(graph):
    # a new FalkorDB with a new one would otherwise have it reported as a UDF that does not exist
    listed = {
        row[0].lower()
        for row in graph.ro_query("CALL dbms.functions()").result_set
        if "." in row[0]
    }

    assert listed == functions.BUILTIN_DOTTED


# ----------------------------------------------------------------------------------------------------
# What each mode says


QUERY = "MATCH (g:Goal) RETURN scoring.boost(g.title), nolib.f(1)"


def test_online_the_instance_is_asked(falkordb_client, scoring):
    issues = SchemaValidator(falkordb_client).validate(QUERY, "kg")[1]

    assert names(issues) == ["nolib.f"]


def test_online_a_query_with_no_dotted_call_asks_nothing_more(
    falkordb_client, requests
):
    SchemaValidator(falkordb_client).validate("MATCH (g:Goal) RETURN g.title", "kg")

    assert not any(type(r).__name__ == "ListUdfs" for r in requests)


def test_a_function_does_not_change_the_score(falkordb_client, scoring):
    score, issues = SchemaValidator(falkordb_client).validate(QUERY, "kg")

    assert names(issues) == ["nolib.f"]
    assert score == 1.0


def test_offline_the_snapshot_answers(falkordb_client, scoring):
    snapshot = Snapshot.capture(falkordb_client, "kg")

    issues = SchemaValidator(snapshot).validate(QUERY)[1]

    assert names(issues) == ["nolib.f"]


def test_offline_a_snapshot_that_did_not_ask_for_the_functions_says_nothing(
    falkordb_client, scoring
):
    snapshot = Snapshot.capture(falkordb_client, "kg", udfs=False)

    assert names(SchemaValidator(snapshot).validate(QUERY)[1]) == []


def test_offline_a_snapshot_written_by_hand_says_nothing_unless_it_has_the_functions():
    plain = Snapshot.from_schema(nodes={"Goal": ["title"]}, relationships=[])
    known = Snapshot.from_schema(
        nodes={"Goal": ["title"]}, relationships=[], udfs={"scoring": ["boost"]}
    )

    assert names(SchemaValidator(plain).validate(QUERY)[1]) == []
    assert names(SchemaValidator(known).validate(QUERY)[1]) == ["nolib.f"]


def test_hybrid_a_miss_is_checked_with_the_database(falkordb_client, scoring):
    # the snapshot was taken before the library was loaded: the database knows better
    stale = Snapshot.from_schema(nodes={"Goal": ["title"]}, relationships=[], udfs={})
    validator = SchemaValidator(falkordb_client, snapshot=stale)

    issues = validator.validate(QUERY, "kg")[1]

    assert names(issues) == [
        "nolib.f"
    ]  # scoring.boost is there, in spite of the snapshot
    assert validator.stats.corrected_misses >= 1


def test_hybrid_a_snapshot_that_does_not_know_the_functions_asks_the_database(
    falkordb_client, scoring
):
    unaware = Snapshot.from_schema(nodes={"Goal": ["title"]}, relationships=[])
    validator = SchemaValidator(falkordb_client, snapshot=unaware)

    assert names(validator.validate(QUERY, "kg")[1]) == ["nolib.f"]


def test_hybrid_with_verify_never_trusts_the_snapshot(falkordb_client, scoring):
    stale = Snapshot.from_schema(nodes={"Goal": ["title"]}, relationships=[], udfs={})
    validator = SchemaValidator(falkordb_client, snapshot=stale, verify="never")

    assert names(validator.validate(QUERY, "kg")[1]) == ["scoring.boost", "nolib.f"]


def test_the_async_validator_says_the_same(falkordb_client, run_async, scoring):
    async def ask(client):
        return (await AsyncSchemaValidator(client).validate(QUERY, "kg"))[1]

    assert names(run_async(ask)) == ["nolib.f"]


class Server:
    def __init__(self, answer):
        self.answer = answer

    def run(self, request):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_a_server_that_cannot_list_its_functions_reports_none():
    error = CypherError(
        "FalkorDB.DatabaseError.Statement.ExecutionFailed",
        "ERR unknown command 'GRAPH.UDF'",
    )

    assert run_sync(functions_missing("g", ("nolib.f",)), Server(error)) == ()


def test_a_failure_to_ask_is_raised():
    error = CypherError(
        "FalkorDB.DatabaseError.Statement.ExecutionFailed", "Connection reset by peer"
    )

    with pytest.raises(CypherError):
        run_sync(missing_functions("g", ("nolib.f",)), Server(error), resolve)
