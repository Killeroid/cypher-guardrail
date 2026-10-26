"""A division by a zero that is in the text of the query fails for an integer and gives ``inf`` for a decimal, so a
property that is divided is looked at: the graph (online), or the snapshot (hybrid) says whether it has integers."""

import pytest

from cypher_guardrail import AsyncSyntaxValidator, Snapshot, SyntaxValidator
from cypher_guardrail.issues import ARITHMETIC_ERROR


@pytest.fixture(scope="module")
def numbers(falkordb_client):
    graph = falkordb_client.select_graph("numbers")
    graph.query(
        "CREATE (:N {whole: 1, decimal: 1.5, text: 'x', mixed: 1}), (:N {mixed: 2.5}), "
        "(:N)-[:R {weight: 2}]->(:N), (:N)-[:S {ratio: 0.5}]->(:N)"
    )
    yield "numbers"
    graph.delete()


ALWAYS = [
    "MATCH (n:N) RETURN n.whole / 0",
    "MATCH (n:N) RETURN n.mixed / 0",  # some of its values are integers
    "MATCH (n:N) RETURN n.nothing / 0",  # no value is known
    "MATCH (n) RETURN n.whole / 0",  # no label
    "MATCH (n:N)-[r:R]->() RETURN r.weight / (1 - 1)",
    "MATCH (n:N) RETURN n.whole % 0",
]
NEVER = [
    "MATCH (n:N) RETURN n.decimal / 0",
    "MATCH (n:N) RETURN n.decimal % (2 - 2)",
    "MATCH (n:N)-[r:S]->() RETURN r.ratio / 0",
    "MATCH (n:`N`) RETURN n.decimal / 0",
]


@pytest.mark.parametrize(("queries", "found"), [(ALWAYS, True), (NEVER, False)])
def test_the_values_of_the_property_say_whether_it_fails(
    falkordb_client, numbers, queries, found
):
    validator = SyntaxValidator(falkordb_client)

    for query in queries:
        issues = validator.validate(query, numbers)[1]
        assert (ARITHMETIC_ERROR in {i["code"] for i in issues}) is found, query


def test_a_snapshot_with_the_types_says_it_too(falkordb_client, numbers):
    snapshot = Snapshot.capture(falkordb_client, numbers, types=True)
    validator = SyntaxValidator(falkordb_client, snapshot=snapshot)

    for query in ALWAYS + NEVER:
        online = SyntaxValidator(falkordb_client).validate(query, numbers)
        assert validator.validate(query, numbers) == online, query


def test_the_async_validator_says_the_same(falkordb_client, numbers, run_async):
    async def ask(client):
        validator = AsyncSyntaxValidator(client)
        return [(await validator.validate(q, numbers))[0] for q in ALWAYS + NEVER]

    expected = [
        SyntaxValidator(falkordb_client).validate(q, numbers)[0] for q in ALWAYS + NEVER
    ]
    assert run_async(ask) == expected
    assert expected == [False] * len(ALWAYS) + [True] * len(NEVER)
