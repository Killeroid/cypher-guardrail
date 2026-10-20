"""The fingerprint of a graph, on FalkorDB: what each part of it notices."""

import pytest


@pytest.fixture
def fingerprinted(falkordb_client):
    from cypher_guardrail._internal.capture_driver import fingerprint

    graph = falkordb_client.select_graph("live_fingerprint")
    graph.query("CREATE (:A {p: 1})-[:R]->(:B {q: 1}), (:A {p: 2})")
    yield graph, lambda: fingerprint(falkordb_client, "live_fingerprint")
    graph.delete()


def test_an_unchanged_graph_has_the_same_fingerprint(fingerprinted):
    _, fingerprint = fingerprinted

    assert fingerprint() == fingerprint()
    assert fingerprint()[4:6] == (3, 1)  # nodes, relationships


@pytest.mark.parametrize(
    "change",
    [
        "CREATE (:A {p: 3})",  # a node more, nothing else new: only the number of nodes
        "MATCH (a:A {p: 1}), (b:B) CREATE (a)-[:R]->(b)",  # a relationship more: only its number
        "CREATE (:C)",  # a label
        "MATCH (a:A {p: 1}), (b:B) CREATE (a)-[:S]->(b)",  # a relationship type
        "MATCH (a:A {p: 2}) SET a.fresh = 1",  # a property key
        "CREATE INDEX FOR (n:A) ON (n.p)",  # an index
        "MATCH (a:A {p: 2}) DELETE a",  # a node less
    ],
)
def test_each_part_of_the_fingerprint_notices_its_own_change(fingerprinted, change):
    graph, fingerprint = fingerprinted
    before = fingerprint()

    graph.query(change)

    assert fingerprint() != before


def test_the_fingerprint_of_a_graph_that_does_not_exist_is_empty(falkordb_client):
    from cypher_guardrail._internal.capture_driver import fingerprint

    assert fingerprint(falkordb_client, "live_no_such_graph") == (
        (),
        (),
        (),
        (),
        0,
        0,
        (),
    )


def test_removing_one_relationship_and_adding_another_is_not_noticed(fingerprinted):
    # the documented limit of a fingerprint: the counts are the same; `refresh(force=True)` and `full_every` exist for it
    graph, fingerprint = fingerprinted
    graph.query("MATCH (:A)-[r:R]->(:B) DELETE r")
    graph.query("MATCH (a:A {p: 2}), (b:B) CREATE (a)-[:R]->(b)")
    before = fingerprint()
    graph.query("MATCH (:A {p: 2})-[r:R]->(:B) DELETE r")
    graph.query("MATCH (a:A {p: 1}), (b:B) CREATE (a)-[:R]->(b)")

    assert fingerprint() == before


def test_the_async_fingerprint_is_the_same(falkordb_client, run_async, fingerprinted):
    from cypher_guardrail._internal.capture_driver import (
        fingerprint,
        fingerprint_async,
    )

    async def ask(client):
        return await fingerprint_async(client, "live_fingerprint")

    assert run_async(ask) == fingerprint(falkordb_client, "live_fingerprint")
