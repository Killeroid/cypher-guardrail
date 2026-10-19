"""The backend: running queries, EXPLAIN, schema lookups, index lookups and the mapping of errors, on FalkorDB."""

import falkordb
import pytest
from support import code_set

from cypher_guardrail._internal.core import queries as _queries
from cypher_guardrail.backend import (
    PARAMETER_NOT_PROVIDED,
    SYNTAX_ERROR,
    UNKNOWN_LABEL,
    UNKNOWN_PROPERTY,
    UNKNOWN_REL_TYPE,
    CypherError,
    FalkorDBBackend,
    get_backend,
)
from cypher_guardrail.utils import is_write, is_write_async

# ----------------------------------------------------------------------------------------------------
# From test_falkordb_backend.py: FalkorDBBackend: running queries, EXPLAIN, schema lookups, index lookups and error mapping.


@pytest.fixture
def backend(falkordb_client):
    return get_backend(falkordb_client)


class TestGetBackend:
    def test_falkordb_client(self, falkordb_client):
        assert isinstance(get_backend(falkordb_client), FalkorDBBackend)

    def test_graph_object(self, graph):
        assert isinstance(get_backend(graph), FalkorDBBackend)

    def test_backend_is_returned_unchanged(self, backend):
        assert get_backend(backend) is backend

    @pytest.mark.parametrize("bad", [None, "redis://localhost", object()])
    def test_other_clients_raise_type_error(self, bad):
        with pytest.raises(TypeError, match="not a valid FalkorDB client"):
            get_backend(bad)

    def test_direct_construction_rejects_other_clients(self):
        with pytest.raises(TypeError, match="not a valid FalkorDB client"):
            FalkorDBBackend(object())


class TestGraphName:
    def test_client_requires_a_graph_name(self, backend):
        with pytest.raises(ValueError, match="graph name"):
            backend.check_database(None)

    def test_client_accepts_explicit_graph_name(self, backend, db_name):
        backend.check_database(db_name)

    def test_bound_graph_supplies_the_default_name(self, graph):
        backend = get_backend(graph)
        backend.check_database(None)
        assert backend.execute("MATCH (g:Goal) RETURN g.code AS code") == [
            {"code": "1"}
        ]

    def test_bound_graph_can_target_another_graph(self, graph, falkordb_client):
        falkordb_client.select_graph("other").query("CREATE (:Other {x: 1})")
        backend = get_backend(graph)
        assert backend.execute("MATCH (o:Other) RETURN o.x AS x", "other") == [{"x": 1}]


class TestExecute:
    def test_rows_are_dicts_keyed_by_column(self, backend, db_name):
        rows = backend.execute(
            "MATCH (g:Goal) RETURN g.code AS code, g.title AS title", db_name
        )
        assert rows == [{"code": "1", "title": "End poverty"}]

    def test_syntax_error_becomes_cypher_error(self, backend, db_name):
        with pytest.raises(CypherError) as error:
            backend.execute("MATCH (g:Goal RETURN g", db_name)
        assert error.value.code == "FalkorDB.ClientError.Statement.SyntaxError"
        assert "Invalid input" in error.value.message

    def test_execution_error_becomes_cypher_error(self, backend, db_name):
        with pytest.raises(CypherError) as error:
            backend.execute("RETURN nonexistent_fn(1)", db_name)
        assert error.value.code == "FalkorDB.DatabaseError.Statement.ExecutionFailed"

    def test_division_by_zero_is_an_arithmetic_error(self, backend, db_name):
        with pytest.raises(CypherError) as error:
            backend.execute("RETURN 1/0", db_name)
        assert error.value.code == "cypher_guardrail.Error.ArithmeticError"
        assert error.value.message == "Division by zero."

    def test_empty_query_is_a_syntax_error(self, backend, db_name):
        with pytest.raises(CypherError) as error:
            backend.execute("", db_name)
        assert error.value.code == "FalkorDB.ClientError.Statement.SyntaxError"

    def test_writes_are_rejected_and_do_not_change_the_graph(
        self, backend, graph, db_name
    ):
        with pytest.raises(CypherError):
            backend.execute("CREATE (:Intruder)", db_name)
        assert graph.ro_query("MATCH (n:Intruder) RETURN count(n)").result_set == [[0]]

    def test_missing_graph_is_a_cypher_error(self, backend):
        with pytest.raises(CypherError):
            backend.execute("MATCH (n) RETURN n", "no_such_graph")


class TestExplain:
    def test_valid_query_has_no_notifications(self, backend, db_name):
        assert backend.explain("MATCH (g:Goal) RETURN g", db_name) == []

    def test_syntax_error(self, backend, db_name):
        with pytest.raises(CypherError):
            backend.explain("MATCH (g:Goal RETURN g", db_name)

    def test_explain_does_not_execute_the_query(self, backend, graph, db_name):
        # A write would be applied if the query were executed (EXPLAIN as a plain query *does* execute on FalkorDB).
        backend.explain("CREATE (:ExplainProbe)", db_name)
        assert graph.ro_query("MATCH (n:ExplainProbe) RETURN count(n)").result_set == [
            [0]
        ]

    def test_missing_parameter_is_reported(self, backend, db_name):
        notifications = backend.explain(
            "MATCH (g:Goal {code: $code, title: $title}) RETURN g", db_name
        )
        assert code_set(notifications) == {PARAMETER_NOT_PROVIDED}
        assert "code, title" in notifications[0]["description"]

    def test_parameter_inside_a_string_literal_is_ignored(self, backend, db_name):
        assert (
            backend.explain("MATCH (g:Goal {title: 'costs $5'}) RETURN g", db_name)
            == []
        )

    def test_constant_division_by_zero_is_an_error(self, backend, db_name):
        with pytest.raises(CypherError):
            backend.explain("RETURN 1/0", db_name)


INDEX_COMMANDS = [
    "CREATE INDEX FOR (g:Goal) ON (g.code)",
    "CREATE VECTOR INDEX FOR (g:Goal) ON (g.embedding) OPTIONS {dimension: 2, similarityFunction: 'euclidean'}",
    "CREATE FULLTEXT INDEX FOR (g:Goal) ON (g.title)",
    "DROP INDEX FOR (g:Goal) ON (g.code)",
]


class TestExplainIndexCommands:
    """GRAPH.EXPLAIN answers index commands with a plain string, not a plan."""

    @pytest.mark.parametrize("command", INDEX_COMMANDS)
    def test_a_valid_index_command_has_no_notifications(
        self, backend, db_name, command
    ):
        assert backend.explain(command, db_name) == []

    def test_a_malformed_index_command_is_a_syntax_error(self, backend, db_name):
        with pytest.raises(CypherError) as error:
            backend.explain("CREATE INDEX FOR (g:Goal ON (g.code)", db_name)
        assert error.value.code == SYNTAX_ERROR

    def test_explain_does_not_create_the_index(self, backend, graph, db_name):
        backend.explain("CREATE INDEX FOR (g:Goal) ON (g.title)", db_name)
        assert graph.ro_query("CALL db.indexes()").result_set == []


class TestIndexExists:
    def test_kind_entity_label_and_attribute_must_all_match(
        self, falkordb_client, documents
    ):
        backend = get_backend(falkordb_client)
        exists = lambda *args: backend.index_exists(*args, database_name=documents)
        assert exists("fulltext", "nodes", "Doc")
        assert exists("fulltext", "nodes", "Doc", "title")
        assert not exists("fulltext", "nodes", "Doc", "embedding")
        assert exists("vector", "nodes", "Doc", "embedding")
        assert not exists("vector", "nodes", "Doc", "title")
        assert not exists("fulltext", "relationships", "Doc")  # a label, not a type
        assert exists("vector", "relationships", "CITES", "emb")
        assert not exists("fulltext", "nodes", "CITES")  # a type, not a label

    def test_a_graph_that_does_not_exist_has_no_indexes(self, falkordb_client):
        backend = get_backend(falkordb_client)
        assert not backend.index_exists(
            "fulltext", "nodes", "Doc", database_name="no_such_graph"
        )

    def test_the_indexes_are_fetched_once_per_snapshot(
        self, falkordb_client, documents
    ):
        backend = get_backend(falkordb_client)
        calls = []
        original = backend._graph(documents).__class__.ro_query

        def counting(self, query, *args, **kwargs):
            calls.append(query)
            return original(self, query, *args, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(falkordb.Graph, "ro_query", counting)
            with backend.snapshot():
                for _ in range(3):
                    backend.index_exists("fulltext", "nodes", "Doc", None, documents)
        assert calls == ["CALL db.indexes()"]


class TestQueryToDf:
    def test_columns_and_rows(self, backend, db_name):
        df = backend.query_to_df(
            "MATCH (t:Target) RETURN t.code AS code, labels(t) AS labels", db_name
        )
        assert list(df.columns) == ["code", "labels"]
        assert df.to_dict("records") == [{"code": "1.1", "labels": ["Target"]}]

    def test_empty_result_keeps_columns(self, backend, db_name):
        df = backend.query_to_df("MATCH (n:Nope) RETURN n.code AS code", db_name)
        assert list(df.columns) == ["code"]
        assert df.empty


class TestPathExists:
    @pytest.mark.parametrize(
        "pattern, expected",
        [
            ("(:Goal)", True),
            ("()-[:HAS_TARGET]-()", True),
            ("(:Goal)-[:HAS_TARGET]->(:Target)", True),
            ("(:Goal)<-[:HAS_TARGET]-(:Target)", False),  # wrong direction
            ("(:Goal)-[:HAS_TARGET]-(:Target)", True),  # undirected matches either way
            ("(:Goal)-[:HAS_TARGET*2]->()", False),
            ("(:Goal)-[:HAS_TARGET*]->()", True),
            ("(:Goal)-[:HAS_TARGET|NOPE]->()", True),
            ("(:Target)-[:HAS_INDICATOR]->(:Indicator)", True),
        ],
    )
    def test_existence(self, backend, db_name, pattern, expected):
        exists, _ = backend.path_exists(pattern, db_name)
        assert exists is expected

    def test_known_schema_has_no_notifications(self, backend, db_name):
        assert backend.path_exists("(:Goal)-[:HAS_TARGET]->(:Target)", db_name)[1] == []

    def test_unknown_label(self, backend, db_name):
        exists, notifications = backend.path_exists("(:Nope)", db_name)
        assert not exists
        assert code_set(notifications) == {UNKNOWN_LABEL}
        assert "Nope" in notifications[0]["description"]

    def test_unknown_relationship_type(self, backend, db_name):
        exists, notifications = backend.path_exists("()-[:NOPE]-()", db_name)
        assert not exists
        assert code_set(notifications) == {UNKNOWN_REL_TYPE}
        assert "NOPE" in notifications[0]["description"]

    def test_unknown_label_and_type_in_one_path(self, backend, db_name):
        _, notifications = backend.path_exists(
            "(:Ghost)-[:HAUNTS*1..2]->(:Goal)", db_name
        )
        assert code_set(notifications) == {UNKNOWN_LABEL, UNKNOWN_REL_TYPE}

    def test_multi_label_node(self, backend, db_name):
        exists, notifications = backend.path_exists("(:GeoArea:Area)", db_name)
        assert exists
        assert notifications == []

    def test_labels_in_property_strings_are_not_checked(self, backend, db_name):
        _, notifications = backend.path_exists("(g:Goal {title: '(:Fake)'})", db_name)
        assert notifications == []

    def test_invalid_pattern_raises(self, backend, db_name):
        with pytest.raises(CypherError):
            backend.path_exists("(:Goal", db_name)


class TestNameExists:
    @pytest.mark.parametrize(
        "kind, name, expected",
        [
            ("node", "Goal", True),
            ("node", "Area", True),
            ("relationship", "HAS_TARGET", True),
            ("node", "HAS_TARGET", False),  # a relationship type is not a node label
            ("relationship", "Goal", False),  # and a node label is not a type
            ("node", "Nope", False),
            ("relationship", "Nope", False),
        ],
    )
    def test_exists(self, backend, db_name, kind, name, expected):
        assert (
            backend.run(_queries.name_exists(backend.graph_name(db_name), kind, name))
            is expected
        )

    def test_missing_graph_has_no_names(self, backend):
        assert (
            backend.run(_queries.name_exists("no_such_graph", "node", "Goal")) is False
        )

    def test_name_is_matched_literally_not_interpolated(self, backend, db_name):
        assert (
            backend.run(
                _queries.name_exists(
                    backend.graph_name(db_name), "node", "Goal' OR 1=1 //"
                )
            )
            is False
        )


class TestPropertyExists:
    @pytest.mark.parametrize(
        "label, kind, prop, expected",
        [
            ("Goal", "node", "code", True),
            ("Goal", "node", "text", False),  # exists in the graph, but on Target
            ("Target", "node", "text", True),
            ("HAS_INDICATOR", "relationship", "weight", True),
            ("HAS_TARGET", "relationship", "weight", False),
        ],
    )
    def test_existence(self, backend, db_name, label, kind, prop, expected):
        exists, _ = backend.property_exists(label, kind, prop, db_name)
        assert exists is expected

    def test_known_property_key_has_no_notification(self, backend, db_name):
        # `text` is a property key in the graph, just not on Goal
        assert backend.property_exists("Goal", "node", "text", db_name)[1] == []

    def test_unknown_property_key_notifies(self, backend, db_name):
        exists, notifications = backend.property_exists("Goal", "node", "nope", db_name)
        assert not exists
        assert code_set(notifications) == {UNKNOWN_PROPERTY}
        assert "nope" in notifications[0]["description"]


def test_falkordb_package_types_are_what_we_expect(falkordb_client, graph):
    # Guards the isinstance checks in get_backend against upstream class moves.
    assert isinstance(falkordb_client, falkordb.FalkorDB)
    assert isinstance(graph, falkordb.Graph)


# ----------------------------------------------------------------------------------------------------
# is_write: does the query modify the graph? (the guard of SyntaxValidator, on its own)


@pytest.fixture
def scratch(falkordb_client):
    """A graph of its own: the database registers the names a write mentions (the property key of a `MERGE`) when it
    explains it, which the shared graphs must not see."""
    graph = falkordb_client.select_graph("is_write_scratch")
    graph.query("CREATE (:Goal {code: 1})")
    yield graph
    graph.delete()


WRITES = [
    "CREATE (:Z)",
    "MATCH (n:Goal) DELETE n",
    "MATCH (n:Goal) SET n.code = 1",
    "MERGE (:Z {a: 1})",
    "MATCH (n:Goal) REMOVE n.code",
    "CREATE INDEX FOR (n:Goal) ON (n.code)",
]
READS = [
    "MATCH (n:Goal) RETURN n",
    "CALL db.labels()",
    "MATCH (n:Goal) WHERE n.code = $code RETURN n",  # a parameter that is not given is not a write
    "RETURN 1",
]


@pytest.mark.parametrize("query", WRITES)
def test_is_write_finds_a_query_that_modifies_the_graph(
    falkordb_client, scratch, query
):
    assert is_write(falkordb_client, query, "is_write_scratch") is True


@pytest.mark.parametrize("query", READS)
def test_is_write_lets_a_query_that_only_reads_through(falkordb_client, scratch, query):
    assert is_write(falkordb_client, query, "is_write_scratch") is False


def test_is_write_does_not_run_the_query(falkordb_client, scratch):
    graph = falkordb_client.select_graph("is_write_scratch")
    before = graph.ro_query("MATCH (n) RETURN count(n)").result_set

    assert (
        is_write(falkordb_client, "MATCH (n) DETACH DELETE n", "is_write_scratch")
        is True
    )

    assert graph.ro_query("MATCH (n) RETURN count(n)").result_set == before


def test_is_write_raises_for_a_query_the_database_rejects(falkordb_client, scratch):
    # A query that cannot be read is not one that is safe: it is no answer
    with pytest.raises(CypherError):
        is_write(falkordb_client, "MATCH (n RETURN n", "is_write_scratch")


def test_is_write_takes_a_graph_object(falkordb_client, scratch):
    graph = falkordb_client.select_graph("is_write_scratch")

    assert is_write(graph, "CREATE (:Z)") is True
    assert is_write(graph, "MATCH (n) RETURN n") is False


def test_is_write_async(run_async, scratch):
    async def ask(client):
        return [
            await is_write_async(client, "CREATE (:Z)", "is_write_scratch"),
            await is_write_async(client, "MATCH (n) RETURN n", "is_write_scratch"),
        ]

    assert run_async(ask) == [True, False]

    async def reject(client):
        return await is_write_async(client, "MATCH (n RETURN n", "is_write_scratch")

    with pytest.raises(CypherError):
        run_async(reject)
