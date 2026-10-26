"""Fixtures: a FalkorDB (a Docker container, a running server or FalkorDBLite) with small graphs the tests validate queries against."""

import asyncio
import contextlib
import os

import pytest
import redis
from servers import (
    BACKEND_VAR,
    IMAGE,
    Unavailable,
    graph_version,
    major_version,
    running_falkordb,
)

GRAPH_NAME = "kg"
# The name of a graph where an unlabeled variable has more than one valid label combination
AMBIGUOUS_GRAPH_NAME = "ambiguous"
AMBIGUOUS_SEED = """
CREATE (:A {p: 1})-[:R]->(:B {q: 1}), (:C {p: 1})-[:R]->(:D {q: 1})
"""

# A small knowledge graph shaped like the SustainGraph examples in the README:
#   Goal -HAS_TARGET-> Target -HAS_INDICATOR-> Indicator, and GeoArea:Area -HAS_NUTS1-> Nuts1
SEED = """
CREATE (g:Goal {code: '1', title: 'End poverty'})
CREATE (t:Target {code: '1.1', text: 'Eradicate extreme poverty'})
CREATE (i:Indicator {code: '1.1.1', unit: 'percent'})
CREATE (a:GeoArea:Area {code: 'EL', name: 'Greece'})
CREATE (n:Nuts1 {code: 'EL3', name: 'Attiki'})
CREATE (g)-[:HAS_TARGET]->(t)
CREATE (t)-[:HAS_INDICATOR {weight: 1}]->(i)
CREATE (a)-[:HAS_NUTS1]->(n)
"""


@pytest.fixture(scope="session")
def falkordb_server(tmp_path_factory):
    """The FalkorDB the tests run on: a Docker container, a running server or FalkorDBLite (see ``servers.py``)."""
    try:
        with running_falkordb(tmp_path_factory.mktemp("falkordb")) as server:
            yield server
    except Unavailable as error:
        pytest.skip(f"no FalkorDB to test against: {error}")


@pytest.fixture(scope="session")
def falkordb_client(falkordb_server):
    """The client of that FalkorDB, holding the seeded `kg` and `ambiguous` graphs."""
    client = falkordb_server.client
    for name in (GRAPH_NAME, AMBIGUOUS_GRAPH_NAME):
        # a server that was used before may still have them
        with contextlib.suppress(redis.ResponseError):
            client.select_graph(name).delete()
    client.select_graph(GRAPH_NAME).query(SEED)
    client.select_graph(AMBIGUOUS_GRAPH_NAME).query(AMBIGUOUS_SEED)
    return client


@pytest.fixture(scope="session")
def falkordb_major(falkordb_server):
    """The major version of the FalkorDB of the tests ("4" or "6"): some of its answers differ between them."""
    return major_version(falkordb_server.client)


@pytest.fixture(scope="session")
def falkordb_version(falkordb_server):
    """The version of the FalkorDB of the tests as a number: 41803 is 4.18.3, 60003 is 6.0.3."""
    return graph_version(falkordb_server.client)


@pytest.fixture(scope="session")
def connection(falkordb_server, falkordb_client):
    """What ``falkordb.FalkorDB`` takes to connect to the same server (host and port, or a unix socket)."""
    return falkordb_server.connection


@pytest.fixture(scope="session")
def plain_falkordb_client(connection):
    """The stock `falkordb.FalkorDB` client (not the FalkorDBLite subclass) talking to the same server."""
    import falkordb

    client = falkordb.FalkorDB(**connection)
    yield client
    client.close()


def pytest_report_header(config):
    return (
        f"falkordb: {os.environ.get(BACKEND_VAR, 'auto')} ({IMAGE} when it is Docker)"
    )


@pytest.fixture
def graph(falkordb_client):
    return falkordb_client.select_graph(GRAPH_NAME)


@pytest.fixture
def db_name():
    return GRAPH_NAME


@pytest.fixture
def ambiguous_name():
    return AMBIGUOUS_GRAPH_NAME


@pytest.fixture
def documents(falkordb_client):
    """A graph of documents and citations, with full-text and vector indexes on both."""
    graph = falkordb_client.select_graph("documents")
    graph.query(
        "CREATE (:Doc {title: 'alpha', embedding: vecf32([1.0, 2.0])})"
        "-[:CITES {emb: vecf32([1.0, 2.0]), note: 'x'}]->"
        "(:Doc {title: 'beta', embedding: vecf32([2.0, 1.0])})"
    )
    options = "OPTIONS {dimension: 2, similarityFunction: 'euclidean'}"
    graph.query("CREATE FULLTEXT INDEX FOR (d:Doc) ON (d.title)")
    graph.query(f"CREATE VECTOR INDEX FOR (d:Doc) ON (d.embedding) {options}")
    graph.query("CREATE FULLTEXT INDEX FOR ()-[c:CITES]-() ON (c.note)")
    graph.query(f"CREATE VECTOR INDEX FOR ()-[c:CITES]-() ON (c.emb) {options}")
    yield "documents"
    graph.delete()


@pytest.fixture
def run_async(connection):
    """``run_async(fn)``: await ``fn(client)`` on a ``falkordb.asyncio`` client, in a fresh event loop."""

    from falkordb.asyncio import FalkorDB as AsyncFalkorDB

    def run(fn):
        async def main():
            client = AsyncFalkorDB(**connection)
            try:
                return await fn(client)
            finally:
                await client.aclose()

        return asyncio.run(main())

    return run


@pytest.fixture
def requests(monkeypatch):
    """What reaches the database: the list of the requests of the sync and the async transports, in order."""
    from cypher_guardrail._internal.core import io as _io

    asked = []
    run = _io.SyncTransport.run
    monkeypatch.setattr(
        _io.SyncTransport,
        "run",
        lambda self, request: asked.append(request) or run(self, request),
    )
    arun = _io.AsyncTransport.run

    async def spy(self, request):
        asked.append(request)
        return await arun(self, request)

    monkeypatch.setattr(_io.AsyncTransport, "run", spy)
    return asked


@pytest.fixture(params=["client", "graph"])
def driver(request, falkordb_client, graph):
    """Validators accept a FalkorDB client (needs database_name) or a Graph (name optional)."""
    return falkordb_client if request.param == "client" else graph
