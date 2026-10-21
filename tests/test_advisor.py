"""``Advisor``: what a person can do about a query that is valid and may be slow.

Every suggestion is checked against FalkorDB itself: its example must be a valid query, and must not need the advice
that it gave. The graph has a label that tells the properties apart (``born`` is on ``Person`` only), one that does not
(``name`` is on ``Person`` and ``Genre``), a relationship that connects two labels and one that does not, and an index.
"""

import pytest

from cypher_guardrail import Advisor, AsyncAdvisor, Snapshot, SyntaxValidator
from cypher_guardrail._internal.core.advice import bound_paths
from cypher_guardrail.suggestions import (
    ADD_LABEL,
    BOUND_PATH,
    CONNECT_PATTERNS,
    USE_INDEX,
)

GRAPH = "advice"


@pytest.mark.parametrize(
    ("path", "bounded"),
    [
        ("(a)-[:R*]->(b)", "(a)-[:R*1..5]->(b)"),
        ("(a)-[*]->(b)", "(a)-[*1..5]->(b)"),
        ("(a)-[r:R * ]->(b)", "(a)-[r:R *1..5]->(b)"),
        ("(a)-[:R*0..]->(b)", "(a)-[:R*0..5]->(b)"),
        ("(a)-[:R*2..]->(b)", "(a)-[:R*2..5]->(b)"),
        ("(a)-[:R*7..]->(b)", "(a)-[:R*7..7]->(b)"),
    ],
)
def test_the_example_limit_is_the_one_the_path_lacks(path, bounded):
    query = f"MATCH {path} RETURN b"

    (suggestion,) = bound_paths(query)

    assert suggestion["example"] == f"MATCH {bounded} RETURN b"


@pytest.fixture(scope="module")
def advice_graph(falkordb_client):
    graph = falkordb_client.select_graph(GRAPH)
    graph.query(
        "CREATE (a:Person {name: 'Ann', born: 1})-[:ACTED_IN]->(m:Movie {title: 'A', year: 2}), "
        "(m)-[:IN_GENRE]->(:Genre {name: 'x'}), (:Island {size: 3})"
    )
    graph.query("CREATE INDEX FOR (m:Movie) ON (m.title)")
    yield graph
    graph.delete()


@pytest.fixture
def advisor(falkordb_client, advice_graph):
    return Advisor(falkordb_client)


def advise(advisor, query):
    return advisor.advise(query, GRAPH)


def codes(suggestions):
    return [s["code"] for s in suggestions]


# ------------------------------------------------------------------------------------------------ the cases
# (query, code, fragment, the example, whether the result may change)
CASES = [
    (
        "MATCH (a:Person)-[:ACTED_IN*]->(m:Movie) RETURN m",
        BOUND_PATH,
        "(a:Person)-[:ACTED_IN*]->(m:Movie)",
        "MATCH (a:Person)-[:ACTED_IN*1..5]->(m:Movie) RETURN m",
    ),
    (
        "MATCH (a:Person)-[:ACTED_IN*1..]->(m:Movie) RETURN m",
        BOUND_PATH,
        "(a:Person)-[:ACTED_IN*1..]->(m:Movie)",
        "MATCH (a:Person)-[:ACTED_IN*1..5]->(m:Movie) RETURN m",
    ),
    (
        "MATCH (n) WHERE n.born = 1 RETURN n",
        ADD_LABEL,
        "(n)",
        "MATCH (n:Person) WHERE n.born = 1 RETURN n",
    ),
    (
        "MATCH (n {born: 1}) RETURN n",
        ADD_LABEL,
        "(n {born: 1})",
        "MATCH (n:Person {born: 1}) RETURN n",
    ),
    (
        "MATCH (a:Person), (m:Movie) RETURN a, m",
        CONNECT_PATTERNS,
        "(a:Person), (m:Movie)",
        "MATCH (a:Person)-[:ACTED_IN]->(m:Movie) RETURN a, m",
    ),
    (
        "MATCH (m:Movie), (a:Person) RETURN a, m",
        CONNECT_PATTERNS,
        "(m:Movie), (a:Person)",
        "MATCH (m:Movie)<-[:ACTED_IN]-(a:Person) RETURN a, m",
    ),
    (
        "MATCH (m:Movie) WHERE toLower(m.title) = 'a' RETURN m",
        USE_INDEX,
        "toLower(m.title)",
        "MATCH (m:Movie) WHERE m.title = 'a' RETURN m",
    ),
]


@pytest.mark.parametrize(("query", "code", "fragment", "example"), CASES)
def test_the_advice_names_what_it_is_about_and_shows_the_query_with_it_changed(
    advisor, query, code, fragment, example
):
    (suggestion,) = advise(advisor, query)

    assert suggestion["code"] == code
    assert suggestion["fragment"] == fragment
    assert suggestion["example"] == example
    assert suggestion["changes_results"] is True
    assert suggestion["description"]


@pytest.mark.parametrize(("query", "code", "fragment", "example"), CASES)
def test_an_example_is_a_valid_query_that_needs_no_more_of_the_advice(
    falkordb_client, advisor, query, code, fragment, example
):
    valid, issues = SyntaxValidator(falkordb_client).validate(example, GRAPH)

    assert valid, issues
    assert code not in codes(advise(advisor, example))


# ------------------------------------------------------------------------------------------ what it leaves alone
@pytest.mark.parametrize(
    "query",
    [
        "MATCH (a:Person)-[:ACTED_IN]->(m:Movie) RETURN m",
        "MATCH (a:Person)-[:ACTED_IN*1..3]->(m:Movie) RETURN m",
        "MATCH (a:Person), (m:Movie) WHERE a.born = m.year RETURN a",  # a hash join, not a product
        "MATCH (m:Movie) WHERE m.title = 'a' RETURN m",
        "MATCH (m:Movie {title: 'a'}) RETURN m",
        "MATCH (m:Movie) WHERE toLower(m.year) = 'a' RETURN m",  # no index on year
        "MATCH (n:Person) WHERE n.born = 1 RETURN n",
    ],
)
def test_a_query_that_needs_no_advice_gets_none(advisor, query):
    assert advise(advisor, query) == []


def test_a_query_the_database_rejects_gets_no_advice(advisor):
    assert advise(advisor, "MATCH (n RETURN n") == []


# ------------------------------------------------------------------------------------- when the advice has no example
def test_several_labels_that_fit_are_listed_and_none_is_chosen(advisor):
    (suggestion,) = advise(advisor, "MATCH (n) WHERE n.name = 'Ann' RETURN n")

    assert suggestion["code"] == ADD_LABEL
    assert suggestion["labels"] == ["Genre", "Person"]
    assert "example" not in suggestion


def test_a_property_that_no_label_has_is_not_a_reason_to_add_one(advisor):
    assert advise(advisor, "MATCH (n) WHERE n.nope = 1 RETURN n") == []


def test_two_labels_that_nothing_connects_are_said_so(advisor):
    (suggestion,) = advise(advisor, "MATCH (a:Person), (i:Island) RETURN a, i")

    assert suggestion["code"] == CONNECT_PATTERNS
    assert suggestion["relationship_types"] == []
    assert "example" not in suggestion


def test_a_fragment_that_is_written_twice_has_no_example(advisor):
    query = "MATCH (a:Person)-[:ACTED_IN*]->(m:Movie) RETURN m UNION MATCH (a:Person)-[:ACTED_IN*]->(m:Movie) RETURN m"

    (suggestion,) = advise(advisor, query)

    assert suggestion["code"] == BOUND_PATH
    assert "example" not in suggestion


def test_a_shortest_path_is_not_unbounded(advisor):
    query = "MATCH (a:Person), (m:Movie) RETURN shortestPath((a)-[:ACTED_IN*]->(m))"

    assert BOUND_PATH not in codes(advise(advisor, query))


# ------------------------------------------------------------------------------------------------ the modes
def test_with_a_snapshot_the_advice_is_the_same(falkordb_client, advisor, advice_graph):
    snapshot = Snapshot.capture(falkordb_client, GRAPH)
    hybrid = Advisor(falkordb_client, snapshot=snapshot)

    for query, *_ in CASES:
        assert advise(hybrid, query) == advise(advisor, query)


def test_the_async_advisor_says_the_same(falkordb_client, advisor, run_async):
    async def ask(client):
        advisor = AsyncAdvisor(client)
        return [await advisor.advise(query, GRAPH) for query, *_ in CASES]

    assert run_async(ask) == [advise(advisor, query) for query, *_ in CASES]


def test_an_advisor_needs_a_database(falkordb_client, advice_graph):
    snapshot = Snapshot.capture(falkordb_client, GRAPH)

    with pytest.raises(TypeError, match="needs a database"):
        Advisor(snapshot)  # ty: ignore[invalid-argument-type]


def test_a_node_that_is_written_twice_is_labelled_without_an_example(advisor):
    query = "MATCH (n) WHERE n.born = 1 WITH n MATCH (n)-[:ACTED_IN]->(m) RETURN m"

    suggestion = next(s for s in advise(advisor, query) if s["code"] == ADD_LABEL)

    assert suggestion["labels"] == ["Person"]
    assert "example" not in suggestion
