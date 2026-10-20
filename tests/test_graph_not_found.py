"""A graph that does not exist: FalkorDB creates a graph with its first write and answers "empty key" to everything
until then. A validation that asked the database about such a graph ends with ``GraphNotFoundError``: a mistake of the
caller (usually the name of the graph), not an answer about the query."""

import pytest

from cypher_guardrail import (
    AsyncPropertiesValidator,
    AsyncSchemaValidator,
    AsyncSyntaxValidator,
    CypherError,
    GraphNotFoundError,
    PropertiesValidator,
    SchemaValidator,
    Snapshot,
    SyntaxValidator,
)
from cypher_guardrail.utils import is_write, is_write_async

GHOST = "graph_that_does_not_exist"
QUERY = "MATCH (g:Goal) WHERE g.code = '1' RETURN g"

VALIDATORS = [
    (SyntaxValidator, AsyncSyntaxValidator),
    (SchemaValidator, AsyncSchemaValidator),
    (PropertiesValidator, AsyncPropertiesValidator),
]


@pytest.mark.parametrize(
    ("sync", "asynchronous"), VALIDATORS, ids=["syntax", "schema", "properties"]
)
def test_a_validator_says_that_the_graph_does_not_exist(
    falkordb_client, run_async, sync, asynchronous
):
    with pytest.raises(GraphNotFoundError) as raised:
        sync(falkordb_client).validate(QUERY, GHOST)

    assert raised.value.graph == GHOST
    assert GHOST in str(raised.value)

    async def ask(client):
        return await asynchronous(client).validate(QUERY, GHOST)

    with pytest.raises(GraphNotFoundError) as raised_async:
        run_async(ask)
    assert str(raised_async.value) == str(raised.value)


def test_the_error_is_also_a_cypher_error_and_a_value_error():
    # is_write raised a CypherError for such a graph before, and a wrong graph name is a ValueError offline
    error = GraphNotFoundError(GHOST)

    assert isinstance(error, CypherError)
    assert isinstance(error, ValueError)
    assert error.code == "FalkorDB.DatabaseError.Statement.ExecutionFailed"


def test_is_write_says_so_too(falkordb_client, run_async):
    with pytest.raises(GraphNotFoundError):
        is_write(falkordb_client, "CREATE (:X)", GHOST)

    async def ask(client):
        return await is_write_async(client, "CREATE (:X)", GHOST)

    with pytest.raises(GraphNotFoundError):
        run_async(ask)


def test_a_graph_that_exists_is_as_before(falkordb_client, db_name):
    assert SyntaxValidator(falkordb_client).validate(QUERY, db_name) == (True, [])
    assert SchemaValidator(falkordb_client).validate(QUERY, db_name) == (1.0, [])
    assert PropertiesValidator(falkordb_client).validate(QUERY, db_name) == (1.0, [])


@pytest.mark.parametrize("sync", [SchemaValidator, PropertiesValidator])
def test_a_query_that_asks_nothing_about_the_graph_is_no_error(falkordb_client, sync):
    # nothing was asked of the database, so there is nothing to say about the graph
    assert sync(falkordb_client).validate("RETURN 1", GHOST) in [(1, []), (None, [])]


def test_extracting_what_the_text_says_needs_no_graph(falkordb_client):
    # explicit labels are read from the query: the database is not asked
    nodes, _, _ = SchemaValidator(falkordb_client).extract(QUERY, GHOST)
    _, labels = PropertiesValidator(falkordb_client).extract(QUERY, GHOST)

    assert nodes == ["(:Goal)"]
    assert labels == {"node": {"Goal": ["code"]}, "relationship": {}}


def test_a_snapshot_of_a_graph_that_does_not_exist_is_still_empty(falkordb_client):
    snapshot = Snapshot.capture(falkordb_client, GHOST)

    assert snapshot.labels == ()
    # offline nothing asks the database: the names are unknown
    _, metadata = SchemaValidator(snapshot).validate(QUERY)
    assert [i["code"] for i in metadata] == ["cypher_guardrail.Warning.UnknownLabel"]


def test_the_database_is_not_asked_again_to_find_out(falkordb_client):
    # the error comes from the requests the validation made anyway: no request is added for it
    seen = []
    from cypher_guardrail._internal.core import io as _io

    run = _io.SyncTransport.run

    def spy(self, request):
        seen.append(request)
        return run(self, request)

    _io.SyncTransport.run = spy
    try:
        with pytest.raises(GraphNotFoundError):
            SchemaValidator(falkordb_client).validate(QUERY, GHOST)
    finally:
        _io.SyncTransport.run = run

    assert seen
    assert all("GRAPH.LIST" not in str(request) for request in seen)
