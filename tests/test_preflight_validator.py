"""PreflightValidator: what it finds in the text of a query, with no database."""

import json
from pathlib import Path

import pytest

from cypher_guardrail import PreflightValidator
from cypher_guardrail.issues import (
    EMPTY_QUERY,
    NO_CYPHER_CLAUSE,
    NO_MATCH_CLAUSE,
    UNBALANCED_BRACKETS,
    WRITE_QUERY_ERROR,
    WRITE_QUERY_WARNING,
)


def codes(query, **options):
    return [i["code"] for i in PreflightValidator(**options).validate(query)[1]]


@pytest.mark.parametrize("query", ["", "   \n", "// nothing\n/* here */"])
def test_an_empty_query_is_an_error_and_nothing_else(query):
    assert PreflightValidator().validate(query) == (
        False,
        [
            {
                "code": EMPTY_QUERY,
                "description": "The query is empty (it has nothing but whitespace and comments).",
            }
        ],
    )


def test_a_text_with_no_clause_is_not_a_query():
    assert PreflightValidator().validate("hello world")[0] is False
    assert codes("hello world") == [NO_CYPHER_CLAUSE]


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n RETURN n",
        "MATCH (n) RETURN n)",
        "MATCH (a)-[r->(b) RETURN a",
        "MATCH (a) RETURN a]",
        "MATCH (a) RETURN (a]",  # closed by another kind
        "MATCH (a {x: 1) RETURN a",
    ],
)
def test_unbalanced_brackets_are_an_error(query):
    assert PreflightValidator().validate(query)[0] is False
    assert codes(query) == [UNBALANCED_BRACKETS]


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (a)-[r]->(b) RETURN a",
        "MATCH (n) WHERE n.t = '(' RETURN n",  # in a string
        "MATCH (n) RETURN n // )",  # in a comment
        "MATCH (n:`Two (Words`) RETURN n",  # in a quoted name
        "MATCH (n) RETURN {a: [1, (2)]}",
    ],
)
def test_brackets_in_text_that_is_not_cypher_are_not_counted(query):
    assert codes(query) == []


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) SET n.done = true",
        "MATCH (n) DETACH DELETE n",
        "match (n) delete n",
        "MATCH (n) REMOVE n.x",
        "CREATE (n:Goal)",
        "MERGE (n:Goal {id: 1})",
        "CREATE INDEX FOR (n:Goal) ON (n.id)",
        "DROP INDEX ON :Goal(id)",
        "MATCH (n) FOREACH (x IN [1] | DELETE n)",
        "UNWIND [1] AS x FOREACH (y IN [x] | CREATE (:A))",
        "CALL db.idx.fulltext.createNodeIndex('Goal', 'title')",
        "CALL db.idx.fulltext.drop('Goal')",
    ],
)
def test_a_query_that_writes_is_an_error_unless_writes_are_allowed(query):
    assert WRITE_QUERY_ERROR in codes(query)
    assert PreflightValidator().validate(query)[0] is False
    assert WRITE_QUERY_WARNING in codes(query, read_only=False)
    assert PreflightValidator(read_only=False).validate(query)[0] is True


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) WHERE n.title = 'Drop off package' RETURN n",
        'MATCH (n) WHERE n.title = "DELETE me" RETURN n',
        "MATCH (n) RETURN n.delete, n.set",
        "MATCH (n:Drop) RETURN n",
        "MATCH (n) RETURN n:Create",
        "MATCH (a)-[:KEEP|DELETE]->(b) RETURN a",
        "MATCH (a)-[r:KEEP|:DELETE]->(b) RETURN a",
        "MATCH (a)-[:`Weird Type`|DELETE]->(b) RETURN a",
        "MATCH (n) WHERE n.x = $delete RETURN n",
        "MATCH (n) RETURN {drop: 1, set: 2}",
        "MATCH (n) RETURN n.`create`",
        "MATCH (n) RETURN n // DELETE n",
        "MATCH (n) /* SET n.x = 1 */ RETURN n",
        "MATCH (n) RETURN [x IN n.xs | x]",
        "CALL db.idx.fulltext.queryNodes('Goal', 'x') YIELD node RETURN node",
        "CALL db.labels()",
    ],
)
def test_a_word_that_is_a_name_or_is_not_cypher_is_not_a_write(query):
    assert codes(query) == []


def test_a_pipe_outside_a_type_list_does_not_hide_a_write():
    assert WRITE_QUERY_ERROR in codes("MATCH (n) FOREACH (x IN [1] | DELETE n)")


@pytest.mark.parametrize(
    "query",
    [
        "CREATE (n:Goal)",
        "MERGE (n:Goal)",
        "CALL db.labels()",
        "UNWIND [1, 2] AS x RETURN x",
        "WITH 1 AS x RETURN x",
        "RETURN 1",
        "MATCH (n) RETURN n",
        "OPTIONAL MATCH (n) RETURN n",
    ],
)
def test_a_query_that_needs_no_match_is_not_warned(query):
    assert NO_MATCH_CLAUSE not in codes(query, read_only=False)


def test_a_query_that_does_not_start_with_a_clause_that_needs_no_match_is_warned():
    assert codes("SET n.x = 1", read_only=False) == [
        WRITE_QUERY_WARNING,
        NO_MATCH_CLAUSE,
    ]
    # a warning leaves the query valid
    assert PreflightValidator(read_only=False).validate("SET n.x = 1")[0] is True


def test_the_issues_come_in_a_fixed_order():
    assert codes("DELETE (n") == [
        UNBALANCED_BRACKETS,
        WRITE_QUERY_ERROR,
        NO_MATCH_CLAUSE,
    ]


# ----------------------------------------------------------------------------------------------------
# The corpus has what FalkorDB answered for each query: a query it says writes must be found here, and one it
# says only reads must not be.

CORPUS = [
    entry
    for name in ("syntax", "valid", "schema", "properties")
    for entry in json.loads(
        (Path(__file__).parent / "accuracy" / f"{name}.json").read_text()
    )
    if not any(
        i["code"].startswith("FalkorDB.") for i in entry.get("issues", [])
    )  # the database stopped at the error, so it said nothing about writes
]


@pytest.mark.parametrize("entry", CORPUS, ids=lambda e: e["id"])
def test_it_agrees_with_the_database_on_what_writes(entry):
    # the corpus writes the codes without the ``cypher_guardrail.`` prefix
    writes = {WRITE_QUERY_ERROR, WRITE_QUERY_WARNING}
    database_says = any(
        f"cypher_guardrail.{i['code']}" in writes for i in entry["issues"]
    )
    assert bool(writes.intersection(codes(entry["query"]))) == database_says
