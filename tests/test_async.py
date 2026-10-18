"""The async validators against the sync ones, on the same FalkorDB (FalkorDBLite).

The async client is created inside each test's event loop and attached to the embedded engine through its
unix socket, as the plain sync client of ``conftest.py`` is.
"""

import asyncio

import falkordb
import pytest
from falkordb.asyncio.graph import AsyncGraph

from cypher_guardrail import (
    AsyncPropertiesValidator,
    AsyncSchemaValidator,
    AsyncSyntaxValidator,
    PropertiesValidator,
    SchemaValidator,
    SyntaxValidator,
)
from cypher_guardrail.backend import AsyncFalkorDBBackend, CypherError, FalkorDBBackend
from cypher_guardrail.utils import (
    get_paths_after_inference,
    get_paths_after_inference_async,
)

# What the validators are asked, on every graph: valid queries, wrong labels, types, directions, paths and
# properties, unlabeled variables to infer, variable lengths, alternatives, a missing parameter, bad syntax
QUERIES = [
    "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g.code, t.code",
    "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target)-[:HAS_INDICATOR]->(i:Indicator) RETURN g, t, i",
    "MATCH (g)-[:HAS_TARGET]->(t) RETURN g.code, t.code",
    "MATCH (n:Goal) WHERE n.nonexistent = 1 RETURN n",
    "MATCH (g:Goal)<-[:HAS_TARGET]-(t:Target) RETURN g",
    "MATCH (a:Nope)-[:NOPE]->(b) RETURN a",
    "MATCH (n) WHERE n.code = $code RETURN n",
    "MATCH (n:Goal RETURN n",
    "MATCH (g:Goal)-[:HAS_TARGET*1..2]->(t:Target) RETURN g",
    "MATCH (g:Goal)-[:HAS_TARGET*2..3]->(t:Target) RETURN g",
    "MATCH (t:Target)-[:HAS_TARGET*1..2]->(g:Goal) RETURN t",
    "MATCH (g:Goal)-[:HAS_INDICATOR]->(i:Indicator) RETURN g",
    "MATCH (g:Goal)--(t:Target) RETURN g, t",
    "MATCH (g:Goal)-->(t) RETURN g.title, t.text",
    "MATCH (a)-[r]->(b) WHERE a.code = '1' AND b.nope = 2 RETURN r",
    "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target)-[:HAS_INDICATOR]->(i) RETURN i.unit, g.bogus",
    "MATCH (a:A)-[:R|S]->(b) RETURN a.p, b.q",
    "MATCH (x)-[:R]->(y) RETURN x.p, y.q",
    "MATCH (n:GeoArea:Area) RETURN n.name",
    "MATCH (n:Goal:Target) RETURN n",
    "MATCH (n) WHERE n.a / 0 > 1 RETURN n",
]

# Full-text and vector index query procedures, on the graph of the ``documents`` fixture
DOCUMENT_QUERIES = [
    "CALL db.idx.fulltext.queryNodes('Doc', 'alpha') YIELD node RETURN node.title",
    "CALL db.idx.fulltext.queryRelationships('CITES', 'x') YIELD relationship RETURN relationship.note",
    "CALL db.idx.vector.queryNodes('Doc', 'embedding', 3, vecf32([1.0, 2.0])) YIELD node RETURN node.title",
    "CALL db.idx.fulltext.queryNodes('Goal', 'x') YIELD node RETURN node",
    "MATCH (a:Doc)-[c:CITES]->(b:Doc) RETURN a.title, c.note, b.title",
    "MATCH (a:Doc)-[c:CITES]->(b:Doc) WHERE a.bogus = 1 RETURN c",
]

QUERIES_BY_GRAPH = {
    "kg": QUERIES,
    "ambiguous": QUERIES,
    "no_such_graph": QUERIES,
    "documents": DOCUMENT_QUERIES,
}

# name -> (sync class, async class, the methods that take a query)
VALIDATORS = {
    "syntax": (SyntaxValidator, AsyncSyntaxValidator, ["validate"]),
    "schema": (SchemaValidator, AsyncSchemaValidator, ["validate", "extract"]),
    "properties": (
        PropertiesValidator,
        AsyncPropertiesValidator,
        ["validate", "extract"],
    ),
}


def outcome(call):
    """What a call gives, or the error it raises (so that errors have to match as well)."""
    try:
        return "returned", call()
    except Exception as e:  # noqa: BLE001 - the error is the outcome being compared
        return "raised", type(e).__name__, str(e)


async def async_outcome(call):
    try:
        return "returned", await call()
    except Exception as e:  # noqa: BLE001 - the error is the outcome being compared
        return "raised", type(e).__name__, str(e)


# ----------------------------------------------------------------------- parity
@pytest.mark.parametrize("graph_name", QUERIES_BY_GRAPH)
@pytest.mark.parametrize("validator", VALIDATORS)
def test_async_validators_give_the_results_of_the_sync_ones(
    request, falkordb_client, run_async, validator, graph_name
):
    if graph_name == "documents":
        request.getfixturevalue("documents")
    sync_class, async_class, methods = VALIDATORS[validator]
    queries = QUERIES_BY_GRAPH[graph_name]

    sync_validator = sync_class(falkordb_client)
    expected = {
        (method, query): outcome(
            lambda method=method, query=query: getattr(sync_validator, method)(
                query, graph_name
            )
        )
        for method in methods
        for query in queries
    }

    async def ask(client):
        async_validator = async_class(client)
        return {
            (method, query): await async_outcome(
                lambda method=method, query=query: getattr(async_validator, method)(
                    query, graph_name
                )
            )
            for method in methods
            for query in queries
        }

    actual = run_async(ask)

    assert {
        case: (expected[case], actual[case])
        for case in expected
        if expected[case] != actual[case]
    } == {}
    if graph_name == "kg" and validator != "syntax":
        # the comparison is not vacuous: queries do get problems reported, and some are left alone
        reported = [
            result[1][1]
            for case, result in expected.items()
            if case[0] == "validate" and result[0] == "returned"
        ]
        assert any(reported) and not all(reported)


@pytest.mark.parametrize("validator", ["schema", "properties"])
def test_async_makes_the_round_trips_of_the_sync_validators(
    falkordb_client, run_async, monkeypatch, validator
):
    sync_class, async_class, _ = VALIDATORS[validator]
    round_trips = {"sync": 0, "async": 0}
    sync_ro_query, async_ro_query = falkordb.Graph.ro_query, AsyncGraph.ro_query

    def counted_sync(self, *args, **kwargs):
        round_trips["sync"] += 1
        return sync_ro_query(self, *args, **kwargs)

    async def counted_async(self, *args, **kwargs):
        round_trips["async"] += 1
        return await async_ro_query(self, *args, **kwargs)

    monkeypatch.setattr(falkordb.Graph, "ro_query", counted_sync)
    monkeypatch.setattr(AsyncGraph, "ro_query", counted_async)

    sync_validator = sync_class(falkordb_client)
    for query in QUERIES:
        sync_validator.validate(query, "kg")

    async def ask(client):
        async_validator = async_class(client)
        for query in QUERIES:
            await async_validator.validate(query, "kg")

    run_async(ask)
    assert round_trips["sync"] > 0
    assert round_trips["async"] == round_trips["sync"]


# ------------------------------------------------------------------ snapshots
def test_validations_running_together_in_one_snapshot_share_their_lookups(
    run_async, monkeypatch
):
    """Five validations started at once ask the database what one validation asks, not five times that."""
    round_trips = {"n": 0}
    original = AsyncGraph.ro_query

    async def counted(self, *args, **kwargs):
        round_trips["n"] += 1
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(AsyncGraph, "ro_query", counted)
    query = "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g.code, t.code"

    async def one(client):
        await AsyncSchemaValidator(client).validate(query, "kg")
        return round_trips["n"]

    async def five_together(client):
        round_trips["n"] = 0
        validator = AsyncSchemaValidator(client)
        with validator.backend.snapshot():
            results = await asyncio.gather(
                *(validator.validate(query, "kg") for _ in range(5))
            )
        return round_trips["n"], results

    alone = run_async(one)
    together, results = run_async(five_together)
    assert alone > 0
    assert together == alone
    assert results == [(1.0, [])] * 5


def test_a_validation_that_times_out_leaves_no_query_running(run_async, monkeypatch):
    running = {"now": 0, "started": 0}
    original = AsyncGraph.ro_query
    delay = {"seconds": 5}

    async def slow(self, *args, **kwargs):
        running["now"] += 1
        running["started"] += 1
        try:
            await asyncio.sleep(delay["seconds"])
            return await original(self, *args, **kwargs)
        finally:
            running["now"] -= 1

    monkeypatch.setattr(AsyncGraph, "ro_query", slow)
    query = "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g"

    async def ask(client):
        validator = AsyncSchemaValidator(client, max_concurrency=1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(validator.validate(query, "kg"), 0.05)
        await asyncio.sleep(0.05)  # let the cancelled query unwind
        left_running = running["now"]
        delay["seconds"] = 0
        # the only slot is free again, so the next validation is not held up
        result = await asyncio.wait_for(validator.validate(query, "kg"), 5)
        return left_running, result

    left_running, result = run_async(ask)
    assert running["started"] > 0
    assert left_running == 0
    assert result == (1.0, [])


# ----------------------------------------------------------------- concurrency
# Queries whose lookups are independent: several paths, and variables without a label that access properties
THREE_PATHS = (
    "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target)-[:HAS_INDICATOR]->(i:Indicator), "
    "(a:GeoArea)-[:HAS_NUTS1]->(n:Nuts1) RETURN g, t, i, a, n"
)
UNLABELED_PROPERTIES = (
    "MATCH (g)-[:HAS_TARGET]->(t)-[r]->(i) "
    "WHERE g.code = '1' AND t.text = 'x' AND i.unit = 'y' RETURN g, t, i"
)


@pytest.mark.parametrize(
    ("validator", "query"),
    [
        (AsyncSchemaValidator, THREE_PATHS),
        (AsyncSchemaValidator, UNLABELED_PROPERTIES),
        (AsyncPropertiesValidator, UNLABELED_PROPERTIES),
    ],
)
def test_one_validation_asks_independent_lookups_at_the_same_time(
    run_async, monkeypatch, validator, query
):
    """The lookups of a validation that do not depend on each other overlap, up to max_concurrency; the
    limit changes how many are in flight, never what is asked or what is answered."""
    now = {"in_flight": 0, "peak": 0, "sent": 0}
    original = AsyncGraph.ro_query

    async def tracked(self, *args, **kwargs):
        now["sent"] += 1
        now["in_flight"] += 1
        now["peak"] = max(now["peak"], now["in_flight"])
        try:
            await asyncio.sleep(
                0.005
            )  # a round trip, so that overlapping requests are seen
            return await original(self, *args, **kwargs)
        finally:
            now["in_flight"] -= 1

    monkeypatch.setattr(AsyncGraph, "ro_query", tracked)

    def validate_with(max_concurrency):
        async def ask(client):
            now.update(in_flight=0, peak=0, sent=0)
            result = await validator(client, max_concurrency=max_concurrency).validate(
                query, "kg"
            )
            return result, now["peak"], now["sent"]

        return run_async(ask)

    result, peak, sent = validate_with(8)
    assert peak > 1
    for limit in (1, 2, 3):
        limited_result, limited_peak, limited_sent = validate_with(limit)
        assert limited_peak == min(limit, peak)
        assert limited_result == result
        assert limited_sent == sent


# ------------------------------------------------------------ clients and errors
def test_sync_validators_turn_an_async_client_away(run_async):
    async def ask(client):
        for validator in (SyntaxValidator, SchemaValidator, PropertiesValidator):
            for driver in (client, client.select_graph("kg")):
                with pytest.raises(TypeError, match="Async"):
                    validator(driver)
        with pytest.raises(TypeError, match="Async"):
            get_paths_after_inference(client, [], "kg")

    run_async(ask)


def test_async_validators_turn_a_sync_client_away(falkordb_client, graph):
    for validator in (
        AsyncSyntaxValidator,
        AsyncSchemaValidator,
        AsyncPropertiesValidator,
    ):
        for driver in (falkordb_client, graph):
            with pytest.raises(TypeError, match="async FalkorDB client"):
                validator(driver)


def test_max_concurrency_must_be_at_least_one(run_async):
    async def ask(client):
        for validator in (
            AsyncSyntaxValidator,
            AsyncSchemaValidator,
            AsyncPropertiesValidator,
        ):
            with pytest.raises(ValueError, match="max_concurrency"):
                validator(client, max_concurrency=0)
            validator(client, max_concurrency=1)

    run_async(ask)


def test_a_bound_graph_needs_no_database_name(graph, run_async):
    query = "MATCH (g:Goal)<-[:HAS_TARGET]-(t:Target) WHERE g.nope = 1 RETURN g"
    expected = {
        "syntax": SyntaxValidator(graph).validate(query),
        "schema": SchemaValidator(graph).validate(query),
        "properties": PropertiesValidator(graph).validate(query),
    }

    async def ask(client):
        bound = client.select_graph("kg")
        return {
            "syntax": await AsyncSyntaxValidator(bound).validate(query),
            "schema": await AsyncSchemaValidator(bound).validate(query),
            "properties": await AsyncPropertiesValidator(bound).validate(query),
        }

    assert run_async(ask) == expected


def test_a_client_without_a_graph_name_is_an_error(run_async):
    async def ask(client):
        for validator in (
            AsyncSyntaxValidator,
            AsyncSchemaValidator,
            AsyncPropertiesValidator,
        ):
            with pytest.raises(ValueError, match="graph name"):
                await validator(client).validate("MATCH (n) RETURN n")

    run_async(ask)


# --------------------------------------------------------------- backend and utils
@pytest.mark.usefixtures("documents")
def test_the_async_backend_does_what_the_sync_backend_does(falkordb_client, run_async):
    sync_backend = FalkorDBBackend(falkordb_client)

    def sync_calls():
        return {
            "execute": outcome(
                lambda: sync_backend.execute(
                    "MATCH (t:Target) RETURN t.code AS code", "kg"
                )
            ),
            "execute error": outcome(
                lambda: sync_backend.execute("MATCH (n RETURN n", "kg")
            ),
            "missing graph": outcome(
                lambda: sync_backend.execute("MATCH (n) RETURN n", "no_such_graph")
            ),
            "explain": outcome(
                lambda: sync_backend.explain("MATCH (n) WHERE n.x = $x RETURN n", "kg")
            ),
            "query_to_df": outcome(
                lambda: sync_backend.query_to_df(
                    "MATCH (t:Target) RETURN t.code AS code", "kg"
                ).to_dict("records")
            ),
            "path_exists": outcome(
                lambda: sync_backend.path_exists(
                    "(:Goal)-[:HAS_TARGET]->(:Target)", "kg"
                )
            ),
            "path_exists unknown": outcome(
                lambda: sync_backend.path_exists("(:Ghost)-[:NOPE]->(:Target)", "kg")
            ),
            "label_kind": outcome(
                lambda: [
                    sync_backend.label_kind(name, "kg")
                    for name in ("Goal", "HAS_TARGET", "Ghost")
                ]
            ),
            "property_exists": outcome(
                lambda: [
                    sync_backend.property_exists("Goal", "node", prop, "kg")
                    for prop in ("code", "nope")
                ]
            ),
            "index_exists": outcome(
                lambda: [
                    sync_backend.index_exists("fulltext", "nodes", "Doc", None, name)
                    for name in ("documents", "kg", "no_such_graph")
                ]
            ),
        }

    async def async_calls(client):
        backend = AsyncFalkorDBBackend(client)
        return {
            "execute": await async_outcome(
                lambda: backend.execute("MATCH (t:Target) RETURN t.code AS code", "kg")
            ),
            "execute error": await async_outcome(
                lambda: backend.execute("MATCH (n RETURN n", "kg")
            ),
            "missing graph": await async_outcome(
                lambda: backend.execute("MATCH (n) RETURN n", "no_such_graph")
            ),
            "explain": await async_outcome(
                lambda: backend.explain("MATCH (n) WHERE n.x = $x RETURN n", "kg")
            ),
            "query_to_df": await async_outcome(
                lambda: _records(
                    backend.query_to_df("MATCH (t:Target) RETURN t.code AS code", "kg")
                )
            ),
            "path_exists": await async_outcome(
                lambda: backend.path_exists("(:Goal)-[:HAS_TARGET]->(:Target)", "kg")
            ),
            "path_exists unknown": await async_outcome(
                lambda: backend.path_exists("(:Ghost)-[:NOPE]->(:Target)", "kg")
            ),
            "label_kind": await async_outcome(
                lambda: _all(
                    backend.label_kind(name, "kg")
                    for name in ("Goal", "HAS_TARGET", "Ghost")
                )
            ),
            "property_exists": await async_outcome(
                lambda: _all(
                    backend.property_exists("Goal", "node", prop, "kg")
                    for prop in ("code", "nope")
                )
            ),
            "index_exists": await async_outcome(
                lambda: _all(
                    backend.index_exists("fulltext", "nodes", "Doc", None, name)
                    for name in ("documents", "kg", "no_such_graph")
                )
            ),
        }

    expected = sync_calls()
    actual = run_async(async_calls)
    assert actual == expected
    assert expected["explain"][1][0]["code"].endswith("ParameterNotProvided")
    assert expected["execute error"][0] == "raised"
    assert expected["index_exists"] == ("returned", [True, False, False])


async def _all(awaitables):
    return [await awaitable for awaitable in awaitables]


async def _records(awaitable):
    return (await awaitable).to_dict("records")


def test_the_async_backend_raises_the_same_cypher_error(falkordb_client, run_async):
    async def ask(client):
        with pytest.raises(CypherError) as error:
            await AsyncFalkorDBBackend(client).execute("MATCH (n RETURN n", "kg")
        return error.value.code

    with pytest.raises(CypherError) as sync_error:
        FalkorDBBackend(falkordb_client).execute("MATCH (n RETURN n", "kg")
    assert run_async(ask) == sync_error.value.code


@pytest.mark.parametrize("graph_name", ["kg", "no_such_graph"])
def test_paths_after_inference_async_gives_the_frames_of_the_sync_function(
    falkordb_client, run_async, graph_name
):
    paths = ["(g)-[:HAS_TARGET]->(t)", "(t)-[r]->(i)", "(a)-[:NOPE]->(b)", "(n"]
    expected = get_paths_after_inference(falkordb_client, paths, graph_name)

    async def ask(client):
        return await get_paths_after_inference_async(client, paths, graph_name)

    actual = run_async(ask)
    assert len(actual) == len(expected)
    for frame, expected_frame in zip(actual, expected, strict=True):
        assert list(frame.columns) == list(expected_frame.columns)
        assert frame.to_dict("records") == expected_frame.to_dict("records")
