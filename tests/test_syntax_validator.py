"""SyntaxValidator (and its async twin) against a real FalkorDB: syntax, parameters, what can write, conflicts.

Includes the ``read_only`` option, the ``parameters`` argument, the ``CYPHER a=1`` header, and the regressions found
in the review that were about this validator.
"""

import copy
import json
import os
import subprocess
import sys
import time
from typing import cast

import pytest
from redis.exceptions import ResponseError
from support import code_set, codes, missing_parameters

from cypher_guardrail import (
    AsyncSyntaxValidator,
    GraphNotFoundError,
    SyntaxValidator,
)
from cypher_guardrail._internal.core import io as _io
from cypher_guardrail._internal.core import queries as _queries
from cypher_guardrail.backend import get_async_backend, get_backend
from cypher_guardrail.issues import (
    PARAMETER_NOT_PROVIDED,
    SYNTAX_ERROR,
    WRITE_QUERY_ERROR,
    WRITE_QUERY_WARNING,
    ConflictingVariablesIssue,
)
from cypher_guardrail.utils import is_write

# ----------------------------------------------------------------------------------------------------
# The three validators against a real FalkorDB (FalkorDBLite), with a client and with a bound graph.


def conflicts(metadata) -> list[ConflictingVariablesIssue]:
    """The issues about conflicting variables, typed as such (they are the only ones that have ``variables``)."""
    return [cast("ConflictingVariablesIssue", issue) for issue in metadata]


# Cypher that FalkorDB cannot parse: label negation, '&' in a relationship type expression, node label alternatives
UNSUPPORTED_CYPHER = [
    "MATCH (n:!Goal) RETURN n",
    "MATCH (g:Goal)-[:HAS_TARGET&HAS]->(t) RETURN g",
    "MATCH (n:Goal|Target) RETURN n",
]


class TestSyntaxValidator:
    def test_valid_query(self, driver, db_name):
        assert SyntaxValidator(driver).validate(
            "MATCH (g:Goal) RETURN COUNT(g) AS output", db_name
        ) == (True, [])

    def test_missing_parameter(self, driver, db_name):
        is_valid, metadata = SyntaxValidator(driver).validate(
            "MATCH (g:Goal {code: $code}) RETURN g", db_name
        )
        assert not is_valid
        assert code_set(metadata) == {PARAMETER_NOT_PROVIDED}

    def test_division_by_zero(self, driver, db_name):
        is_valid, metadata = SyntaxValidator(driver).validate(
            "MATCH (g:Goal) RETURN g.code / 0", db_name
        )
        assert not is_valid
        assert metadata

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (g:Goal RETURN g",  # not closed
            "CREATE INDEX FOR (g:Goal ON (g.code)",  # a malformed index command
            "",  # nothing to parse
            *UNSUPPORTED_CYPHER,  # what FalkorDB does not have
        ],
    )
    def test_what_cannot_be_parsed_is_a_syntax_error(self, driver, db_name, query):
        is_valid, metadata = SyntaxValidator(driver).validate(query, db_name)
        assert not is_valid
        assert code_set(metadata) == {"FalkorDB.ClientError.Statement.SyntaxError"}

    def test_index_commands_are_valid_but_write(self, driver, db_name):
        for command in (
            "CREATE INDEX FOR (g:Goal) ON (g.code)",
            "CREATE VECTOR INDEX FOR (g:Goal) ON (g.embedding) OPTIONS {dimension: 2, similarityFunction: 'euclidean'}",
            "DROP INDEX FOR (g:Goal) ON (g.code)",
        ):
            # Valid, but they modify the graph: a warning, or an error when only reads are allowed
            is_valid, metadata = SyntaxValidator(driver, read_only=False).validate(
                command, db_name
            )
            assert is_valid
            assert code_set(metadata) == {"cypher_guardrail.Warning.WriteQuery"}
            is_valid, metadata = SyntaxValidator(driver).validate(command, db_name)
            assert not is_valid
            assert code_set(metadata) == {"cypher_guardrail.Error.WriteQuery"}

    def test_validation_does_not_execute_writes(self, driver, graph, db_name):
        SyntaxValidator(driver).validate("CREATE (:SyntaxProbe)", db_name)
        assert graph.ro_query("MATCH (n:SyntaxProbe) RETURN count(n)").result_set == [
            [0]
        ]

    @pytest.mark.parametrize(
        "query",
        [
            "RETURN 1/0",  # FalkorDB rejects a constant division by zero while planning
            "MATCH (g:Goal) RETURN g.code / 0",  # anything else is found by cypher-guardrail's own check
        ],
    )
    def test_division_by_zero_gives_the_same_metadata_either_way(
        self, driver, db_name, query
    ):
        is_valid, metadata = SyntaxValidator(driver).validate(query, db_name)
        assert not is_valid
        assert metadata == [
            {
                "code": "cypher_guardrail.Error.ArithmeticError",
                "description": "Division by zero.",
            }
        ]

    def test_error_messages_are_reported_whole(self, driver, db_name):
        # FalkorDB echoes the query in its error message. A word in the query must not cut the message short.
        is_valid, metadata = SyntaxValidator(driver).validate(
            "MATCH (n) EXPLAIN RETURN n", db_name
        )
        assert not is_valid
        (error,) = metadata
        assert error["code"] == "FalkorDB.ClientError.Statement.SyntaxError"
        # FalkorDB 4 starts the message with "errMsg: "
        assert error["description"].removeprefix("errMsg: ").startswith("Invalid input")
        assert "MATCH (n) EXPLAIN RETURN n" in error["description"]

    CONFLICT = "MATCH (g:Goal) MATCH (g:Target) RETURN g"

    def test_conflicting_labels(self, driver, db_name):
        is_valid, metadata = SyntaxValidator(
            driver, check_multilabeled_nodes=False
        ).validate(self.CONFLICT, db_name)
        assert not is_valid
        assert code_set(metadata) == {"cypher_guardrail.Error.ConflictingVariables"}
        assert (
            "Goal" in metadata[0]["description"]
            and "Target" in metadata[0]["description"]
        )

    def test_real_multilabel_nodes_are_not_conflicts(self, driver, db_name):
        # GeoArea and Area co-occur on one node in the graph
        query = "MATCH (g:GeoArea) MATCH (g:Area) RETURN g"
        assert SyntaxValidator(driver, check_multilabeled_nodes=True).validate(
            query, db_name
        ) == (True, [])
        assert not SyntaxValidator(driver, check_multilabeled_nodes=False).validate(
            query, db_name
        )[0]

    def test_unrelated_labels_still_conflict_with_multilabel_check(
        self, driver, db_name
    ):
        is_valid, metadata = SyntaxValidator(
            driver, check_multilabeled_nodes=True
        ).validate(self.CONFLICT, db_name)
        assert not is_valid
        assert code_set(metadata) == {"cypher_guardrail.Error.ConflictingVariables"}

    def test_constructor_does_not_need_a_graph_name(self, falkordb_client):
        SyntaxValidator(
            falkordb_client
        )  # multilabels are fetched lazily, once a graph name is known

    def test_unknown_graph_is_an_error(self, falkordb_client):
        with pytest.raises(GraphNotFoundError, match="no_such_graph"):
            SyntaxValidator(falkordb_client).validate(
                "MATCH (n) RETURN n", "no_such_graph"
            )

    def test_bound_graph_needs_no_database_name(self, graph):
        assert SyntaxValidator(graph).validate("MATCH (g:Goal) RETURN g") == (True, [])


# ----------------------------------------------------------------------------------------------------
# SyntaxValidator and its ``read_only`` option: a query that can modify the graph is an error, or a warning.
#
# FalkorDB itself decides what is a write (``GRAPH.RO_QUERY`` refuses it), so nothing is parsed with a regex and
# a word inside a string (``'DELETE me'``) is not a write. The query is never executed.


WRITES = [
    "MATCH (n) DETACH DELETE n",
    "MATCH (n:Goal) SET n.x = 1 RETURN n",
    "CREATE (:X)",
    "MERGE (:X {a: 1})",
    "MATCH (n:Goal) REMOVE n.code",
    "CALL db.idx.fulltext.drop('Goal')",
    "CREATE INDEX FOR (n:Goal) ON (n.code)",
    "DROP INDEX ON :Goal(code)",
    "CYPHER a=1 CREATE (:X)",
]


READS = [
    "MATCH (g:Goal) RETURN g",
    "MATCH (g:Goal) WHERE g.title = 'DELETE me' RETURN g",
    "MATCH (g:Goal) WHERE g.title = 'CREATE (:X) SET' RETURN g // DELETE n",
    "CYPHER a='1' MATCH (g:Goal) RETURN g",
    "RETURN 1",
]


def validate_async(run_async, query, *args, **options):
    async def ask(client):
        return await AsyncSyntaxValidator(client, **options).validate(
            query, "kg", *args
        )

    return run_async(ask)


@pytest.mark.parametrize("query", WRITES)
def test_a_write_is_an_error_by_default(falkordb_client, run_async, query):
    valid, metadata = SyntaxValidator(falkordb_client).validate(query, "kg")

    assert valid is False
    assert codes(metadata) == [WRITE_QUERY_ERROR]
    assert metadata[0]["description"]
    assert validate_async(run_async, query) == (valid, metadata)


def test_validating_the_writes_leaves_the_graph_as_it_was(falkordb_client, graph):
    def contents():
        nodes = graph.ro_query("MATCH (n) RETURN labels(n), properties(n)")
        indexes = graph.ro_query("CALL db.indexes()")
        return sorted(map(str, nodes.result_set)), sorted(map(str, indexes.result_set))

    before = contents()
    validator = SyntaxValidator(falkordb_client)
    for query in WRITES:
        validator.validate(query, "kg")

    assert contents() == before


@pytest.mark.parametrize("query", WRITES)
def test_a_write_is_only_a_warning_when_writes_are_allowed(
    falkordb_client, run_async, query
):
    validator = SyntaxValidator(falkordb_client, read_only=False)
    valid, metadata = validator.validate(query, "kg")

    assert valid is True  # a warning does not make the query invalid
    assert codes(metadata) == [WRITE_QUERY_WARNING]
    assert validate_async(run_async, query, read_only=False) == (valid, metadata)


@pytest.mark.parametrize("read_only", [True, False])
@pytest.mark.parametrize("query", READS)
def test_a_read_has_no_issue(falkordb_client, run_async, query, read_only):
    valid, metadata = SyntaxValidator(falkordb_client, read_only=read_only).validate(
        query, "kg"
    )

    assert (valid, metadata) == (True, [])
    assert validate_async(run_async, query, read_only=read_only) == (True, [])


@pytest.mark.parametrize("read_only", [True, False])
def test_a_query_that_starts_with_explain_is_valid_where_the_server_takes_it(
    falkordb_client, run_async, read_only
):
    query = "EXPLAIN MATCH (g:Goal) RETURN g"
    try:
        falkordb_client.connection.execute_command("GRAPH.EXPLAIN", "kg", query)
    except ResponseError:
        pytest.skip("FalkorDB 6 and later do not take EXPLAIN as part of a query")

    valid, metadata = SyntaxValidator(falkordb_client, read_only=read_only).validate(
        query, "kg"
    )

    assert (valid, metadata) == (True, [])
    assert validate_async(run_async, query, read_only=read_only) == (True, [])


def test_read_only_is_the_default(falkordb_client):
    assert SyntaxValidator(falkordb_client).read_only is True
    assert SyntaxValidator(falkordb_client, read_only=False).read_only is False


@pytest.mark.parametrize("read_only", [True, False])
def test_a_query_with_cypher_parameters_is_valid_and_not_taken_for_a_write(
    falkordb_client, read_only
):
    # The read-only form of the check does not get the parameters of the prefix and fails with "Missing
    # parameters": the plain EXPLAIN has the last word
    query = "CYPHER a='1' MATCH (g:Goal) WHERE g.code = $a RETURN g"

    assert SyntaxValidator(falkordb_client, read_only=read_only).validate(
        query, "kg"
    ) == (True, [])


@pytest.mark.parametrize("read_only", [True, False])
def test_a_syntax_error_in_a_write_is_the_only_issue(falkordb_client, read_only):
    valid, metadata = SyntaxValidator(falkordb_client, read_only=read_only).validate(
        "CREATE (n:X", "kg"
    )

    assert valid is False
    assert codes(metadata) == [SYNTAX_ERROR]


def test_a_write_with_a_missing_parameter_has_both_issues(falkordb_client):
    query = "MATCH (g:Goal) WHERE g.code = $code SET g.x = 1"

    valid, metadata = SyntaxValidator(falkordb_client).validate(query, "kg")
    assert valid is False
    assert sorted(codes(metadata)) == sorted(
        [PARAMETER_NOT_PROVIDED, WRITE_QUERY_ERROR]
    )

    # the warning for the write does not hide that the missing parameter makes the query invalid
    valid, metadata = SyntaxValidator(falkordb_client, read_only=False).validate(
        query, "kg"
    )
    assert valid is False
    assert sorted(codes(metadata)) == sorted(
        [PARAMETER_NOT_PROVIDED, WRITE_QUERY_WARNING]
    )


def test_validating_a_write_does_not_change_the_graph(falkordb_client):
    graph = falkordb_client.select_graph("kg")

    def state():
        nodes = graph.ro_query("MATCH (n) RETURN count(n)").result_set
        indexes = graph.ro_query("CALL db.indexes()").result_set
        return nodes, indexes

    before = state()
    for read_only in (True, False):
        for query in WRITES:
            SyntaxValidator(falkordb_client, read_only=read_only).validate(query, "kg")
    assert state() == before


def test_the_write_check_takes_one_round_trip_for_a_valid_read(
    falkordb_client, monkeypatch
):
    asked = []
    run = _io.SyncTransport.run
    monkeypatch.setattr(
        _io.SyncTransport,
        "run",
        lambda self, request: asked.append(request) or run(self, request),
    )

    SyntaxValidator(falkordb_client, check_multilabeled_nodes=False).validate(
        "MATCH (g:Goal) RETURN g", "kg"
    )

    assert len([r for r in asked if isinstance(r, _io.Explain)]) == 1


@pytest.mark.parametrize("query", ["", " ", "\n\t  \n"])
def test_an_empty_query_is_rejected_without_asking_the_database(
    falkordb_client, run_async, monkeypatch, query
):
    asked = []
    monkeypatch.setattr(
        _io.SyncTransport, "run", lambda self, request: asked.append(request)
    )
    monkeypatch.setattr(
        _io.AsyncTransport, "run", lambda self, request: asked.append(request)
    )

    valid, metadata = SyntaxValidator(falkordb_client).validate(query, "kg")

    assert valid is False
    assert metadata == [
        {"code": SYNTAX_ERROR, "description": "Error: empty query."},
    ]
    assert validate_async(run_async, query) == (valid, metadata)
    assert asked == []


def test_the_backend_explain_does_not_look_for_writes(falkordb_client):
    from cypher_guardrail.backend import get_backend

    assert get_backend(falkordb_client).explain("CREATE (:X)", "kg") == []


# ----------------------------------------------------------------------------------------------------
# ``validate(query, database_name, parameters)``: the parameters the caller will send with the query.
#
# A client sends parameters apart from the query (``graph.query(q, params={...})``). cypher-guardrail never sees
# them, so without ``parameters`` every ``$name`` is reported as not provided.


QUERY = "MATCH (g:Goal) WHERE g.code = $code AND g.title = $title RETURN g"


def test_without_parameters_every_parameter_is_missing(falkordb_client):
    valid, metadata = SyntaxValidator(falkordb_client).validate(QUERY, "kg")

    assert valid is False
    assert missing_parameters(metadata) == [["code", "title"]]


@pytest.mark.parametrize(
    "given",
    [
        {"code": "1", "title": "x"},  # what the client is given as params
        {"code": None, "title": None},
        {"code", "title"},
        frozenset({"code", "title"}),
        ["code", "title"],
        ("code", "title"),
        ["title", "code", "unused"],
    ],
)
def test_the_parameters_given_are_not_missing(falkordb_client, run_async, given):
    expected = (True, [])

    assert SyntaxValidator(falkordb_client).validate(QUERY, "kg", given) == expected
    assert validate_async(run_async, QUERY, given) == expected


@pytest.mark.parametrize(
    ("given", "reported"),
    [
        ({"code": "1"}, [["title"]]),
        ({"title": "x"}, [["code"]]),
        ({"other": 1}, [["code", "title"]]),
        ({}, [["code", "title"]]),
        (None, [["code", "title"]]),
        ([], [["code", "title"]]),
    ],
)
def test_the_parameters_not_given_are_still_missing(
    falkordb_client, run_async, given, reported
):
    valid, metadata = SyntaxValidator(falkordb_client).validate(QUERY, "kg", given)

    assert valid is False
    assert missing_parameters(metadata) == reported
    assert validate_async(run_async, QUERY, given) == (valid, metadata)


def test_the_parameters_and_a_cypher_header_add_up(falkordb_client):
    query = "CYPHER code='1' MATCH (g:Goal) WHERE g.code = $code AND g.x = $a AND g.y = $b RETURN g"

    _, metadata = SyntaxValidator(falkordb_client).validate(query, "kg", {"a": 1})

    assert missing_parameters(metadata) == [["b"]]


def test_the_parameters_do_not_hide_other_issues(falkordb_client):
    query = "MATCH (g:Goal) WHERE g.code = $code SET g.x = 1"

    valid, metadata = SyntaxValidator(falkordb_client).validate(
        query, "kg", {"code": "1"}
    )

    assert valid is False
    assert [i["code"] for i in metadata] == ["cypher_guardrail.Error.WriteQuery"]


def test_the_callers_parameters_are_not_changed(falkordb_client):
    given = {"code": "1", "title": ["x"]}
    before = copy.deepcopy(given)

    SyntaxValidator(falkordb_client).validate(QUERY, "kg", given)

    assert given == before


@pytest.mark.parametrize("given", ["code", b"code"])
def test_a_string_is_not_a_collection_of_names(falkordb_client, run_async, given):
    # "code" would be read as the names c, o, d and e
    with pytest.raises(TypeError, match="parameters"):
        SyntaxValidator(falkordb_client).validate(QUERY, "kg", given)
    with pytest.raises(TypeError, match="parameters"):
        validate_async(run_async, QUERY, given)


def test_the_backend_explain_takes_them_too(falkordb_client):
    backend = get_backend(falkordb_client)

    assert [i["parameters"] for i in backend.explain(QUERY, "kg")] == [
        ["code", "title"]
    ]
    assert [i["parameters"] for i in backend.explain(QUERY, "kg", {"code": 1})] == [
        ["title"]
    ]
    assert backend.explain(QUERY, "kg", ["code", "title"]) == []


def test_the_async_backend_explain_takes_them_too(run_async):
    async def ask(client):
        backend = get_async_backend(client)
        return [
            await backend.explain(QUERY, "kg"),
            await backend.explain(QUERY, "kg", {"code": 1}),
            await backend.explain(QUERY, "kg", ["code", "title"]),
        ]

    everything, only_title, nothing = run_async(ask)
    assert [i["parameters"] for i in everything] == [["code", "title"]]
    assert [i["parameters"] for i in only_title] == [["title"]]
    assert nothing == []


# ----------------------------------------------------------------------------------------------------
# A ``CYPHER a=1 ...`` prefix gives the query its parameters, so they are not reported as missing.
#
# FalkorDB only says a parameter is missing when the query runs, so cypher-guardrail looks for ``$name`` in the text of
# the query, and has to know which names the prefix provides.


@pytest.mark.parametrize(
    ("query", "provided"),
    [
        ("MATCH (n) RETURN n", set()),
        ("CYPHER a=1 RETURN $a", {"a"}),
        ("cypher a=1 RETURN $a", {"a"}),
        ("  \n CYPHER a = 1 RETURN $a", {"a"}),
        ("CYPHER a=1 b='two' c=3.5 d=true e=null RETURN 1", set("abcde")),
        ("CYPHER a=-1.5 RETURN 1", {"a"}),
        ("CYPHER a=[1, 2] b={k: 'v', l: [3]} RETURN 1", {"a", "b"}),
        # a value can hold what looks like another parameter, a quote, a bracket or an equals sign
        ("CYPHER a='x y=z' b=\"it's\" c='}]' d=4 RETURN 1", set("abcd")),
        ("CYPHER a='it\\'s' b=2 RETURN 1", {"a", "b"}),
        ("CYPHER a=['x', 'y'] b={k: 'v'} c=3 RETURN 1", {"a", "b", "c"}),
        # the query after the parameters is not part of them
        ("CYPHER a=1 MATCH (n) WHERE n.x = 2 RETURN n", {"a"}),
        ("CYPHER a=1 RETURN b = 2", {"a"}),
        ("CYPHER a=1 // a comment\n RETURN 1", {"a"}),
        # only a prefix counts
        ("RETURN 'CYPHER a=1'", set()),
        ("MATCH (n) RETURN n // CYPHER a=1", set()),
        ("CYPHER", set()),
        ("CYPHERS a=1 RETURN 1", set()),
    ],
)
def test_the_parameters_a_query_provides(query, provided):
    assert _queries.provided_parameters(query) == provided


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "MATCH (m:Movie) WHERE m.title = $title RETURN m",
            "CYPHER title=0 MATCH (m:Movie) WHERE m.title = $title RETURN m",
        ),
        (
            "MATCH (m:Movie) RETURN m SKIP $s LIMIT $n",
            "CYPHER s=0 n=0 MATCH (m:Movie) RETURN m SKIP $s LIMIT $n",
        ),
        # a parameter used twice is given once; a parameter whose property is read is a map
        (
            "MATCH (m:Movie) WHERE m.a = $x OR m.b = $x OR m.c = $p.k RETURN m",
            "CYPHER x=0 p={} MATCH (m:Movie) WHERE m.a = $x OR m.b = $x OR m.c = $p.k RETURN m",
        ),
        # the parameters of a header are kept, the others are added to it
        (
            "CYPHER a=1 MATCH (m:Movie) WHERE m.t = $t AND m.a = $a RETURN m",
            "CYPHER a=1 t=0 MATCH (m:Movie) WHERE m.t = $t AND m.a = $a RETURN m",
        ),
        (
            "CYPHER a='x y' n=[1, 2] MATCH (m:Movie) WHERE m.t = $t RETURN m",
            "CYPHER a='x y' n=[1, 2] t=0 MATCH (m:Movie) WHERE m.t = $t RETURN m",
        ),
        # nothing to add
        ("CYPHER t='x' MATCH (m:Movie) WHERE m.t = $t RETURN m", None),
        ("MATCH (m:Movie) RETURN m", None),
        ("MATCH (m:Movie) WHERE m.t = '$t' RETURN m // $limit", None),
    ],
)
def test_a_placeholder_for_each_parameter_that_no_header_gives(query, expected):
    assert _queries.with_placeholder_parameters(query) == expected


@pytest.fixture
def indexed_graph(falkordb_client):
    graph = falkordb_client.select_graph("indexed_params")
    graph.query("CREATE (:Movie {title: 'a', released: 1, rating: 2.0})")
    graph.query("CREATE FULLTEXT INDEX FOR (m:Movie) ON (m.title)")
    yield "indexed_params"
    graph.delete()


@pytest.mark.parametrize(
    ("query", "names"),
    [
        ("MATCH (m:Movie) WHERE m.title = $title RETURN m", ["title"]),
        ("MATCH (m:Movie) WHERE m.rating > $min RETURN m", ["min"]),
        ("MATCH (m:Movie {title: $title}) RETURN m", ["title"]),
        ("MATCH (m:Movie) RETURN m SKIP $skip LIMIT $limit", ["skip", "limit"]),
        ("MATCH (m:Movie) WHERE m.title IN $titles RETURN m", ["titles"]),
    ],
)
def test_a_parameter_on_a_label_with_an_index_needs_no_value(
    falkordb_client, run_async, indexed_graph, query, names
):
    # FalkorDB cannot plan these without the value of the parameter: the validator gives it a placeholder
    validator = SyntaxValidator(falkordb_client)

    assert validator.validate(query, indexed_graph, parameters=names) == (True, [])
    valid, metadata = validator.validate(query, indexed_graph)
    assert valid is False
    assert codes(metadata) == ["cypher_guardrail.Warning.ParameterNotProvided"]
    assert missing_parameters(metadata) == [names]

    async def ask(client):
        validator = AsyncSyntaxValidator(client)
        return (
            await validator.validate(query, indexed_graph, parameters=names),
            await validator.validate(query, indexed_graph),
        )

    given, not_given = run_async(ask)
    assert given == (True, [])
    assert not_given == (valid, metadata)


def test_a_query_that_is_wrong_besides_its_parameters_is_still_wrong(
    falkordb_client, indexed_graph
):
    # the placeholders let the plan be made; they do not make a real error go away
    valid, metadata = SyntaxValidator(falkordb_client).validate(
        "MATCH (m:Movie) WHERE m.title = $title RETURN q.name",
        indexed_graph,
        parameters=["title"],
    )

    assert valid is False
    assert code_set(metadata) == {"cypher_guardrail.Error.UnboundVariable"}


@pytest.mark.parametrize(
    ("query", "reported"),
    [
        ("CYPHER a='1' MATCH (g:Goal) WHERE g.code = $a RETURN g", []),
        ("CYPHER a=1 b=[1] MATCH (g:Goal) WHERE g.x = $a AND g.y = $b RETURN g", []),
        ("CYPHER a=1 MATCH (g:Goal) WHERE g.x = $a AND g.y = $b RETURN g", [["b"]]),
        (
            "CYPHER a=1 MATCH (g:Goal) WHERE g.x = $b AND g.y = $c RETURN g",
            [["b", "c"]],
        ),
        ("MATCH (g:Goal) WHERE g.x = $a RETURN g", [["a"]]),
    ],
)
def test_only_the_parameters_the_prefix_does_not_give_are_missing(
    falkordb_client, run_async, query, reported
):
    valid, metadata = SyntaxValidator(falkordb_client).validate(query, "kg")

    assert missing_parameters(metadata) == reported
    assert valid is (not reported)

    async def ask(client):
        return await AsyncSyntaxValidator(client).validate(query, "kg")

    assert run_async(ask) == (valid, metadata)


def test_the_backend_explain_agrees(falkordb_client):
    from cypher_guardrail.backend import get_backend

    backend = get_backend(falkordb_client)
    assert backend.explain("CYPHER a=1 RETURN $a", "kg") == []
    assert [i["parameters"] for i in backend.explain("CYPHER a=1 RETURN $b", "kg")] == [
        ["b"]
    ]


# ----------------------------------------------------------------------------------------------------
# Regression tests for bugs found in the code review.


class TestHashSeedIndependence:
    """Python randomises str hashes per process, so anything ordered by a set differs between runs."""

    # The subprocess only connects to the database of the test session: starting a database for each seed
    # would take most of the time of the whole suite.
    SCRIPT = """
import json
import sys
import falkordb
from cypher_guardrail import SyntaxValidator

client = falkordb.FalkorDB(**json.loads(sys.argv[1]))
validator = SyntaxValidator(client, check_multilabeled_nodes=False)
print(validator.validate("MATCH (g:Zeta) MATCH (g:Alpha) MATCH (g:Mid) RETURN g", "hash_seed")[1][0]["description"])
"""

    def test_conflicting_label_message_does_not_depend_on_the_hash_seed(
        self, falkordb_client, connection
    ):
        graph = falkordb_client.select_graph("hash_seed")
        graph.query("CREATE (:Zeta), (:Alpha), (:Mid)")
        try:
            messages = {
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        self.SCRIPT,
                        json.dumps(connection),
                    ],
                    env={**os.environ, "PYTHONHASHSEED": str(seed)},
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                for seed in range(4)
            }
        finally:
            graph.delete()
        # Labels are listed in the order the query mentions them
        assert messages == {"Variable 'g' has conflicting labels: Zeta, Alpha, Mid"}


class TestStringLiteralsAndComments:
    """Text inside string literals and comments is data, not Cypher, and must not trigger checks."""

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (g:Goal) WHERE g.title = 'a / 0' RETURN g",
            'MATCH (g:Goal) WHERE g.title = "x / (0)" RETURN g',
            "MATCH (g:Goal) WHERE g.title = '(n:X) (n:Y)' RETURN g",
            "MATCH (g:Goal) // (n:X) (n:Y) and 1 / 0\nRETURN g",
            "MATCH (g:Goal) /* (n:X) (n:Y) */ RETURN g",
            "MATCH (g:Goal) WHERE g.title = 'it\\'s (n:X) (n:Y)' RETURN g",
        ],
    )
    def test_no_false_positives(self, falkordb_client, db_name, query):
        validator = SyntaxValidator(falkordb_client, check_multilabeled_nodes=False)
        assert validator.validate(query, db_name) == (True, [])

    def test_real_conflicts_are_still_found_next_to_strings(
        self, falkordb_client, db_name
    ):
        validator = SyntaxValidator(falkordb_client, check_multilabeled_nodes=False)
        is_valid, metadata = validator.validate(
            "MATCH (g:Goal) MATCH (g:Target) WHERE g.title = 'x' RETURN g", db_name
        )
        assert not is_valid
        assert metadata[0]["code"] == "cypher_guardrail.Error.ConflictingVariables"

    def test_real_division_by_zero_is_still_found_next_to_strings(
        self, falkordb_client, db_name
    ):
        validator = SyntaxValidator(falkordb_client, check_multilabeled_nodes=False)
        is_valid, metadata = validator.validate(
            "MATCH (g:Goal) WHERE g.title = 'x' RETURN g.code / 0", db_name
        )
        assert not is_valid and metadata


# ----------------------------------------------------------------------------------------------------
# A divisor that is a number starting with 0 is not necessarily zero


@pytest.mark.parametrize(
    "divisor",
    [
        *["0.5", "0.25", "0.05", "0.0001", "(0.5)", "( 0.75 )", "0.1 + 1"],
        # a decimal zero gives inf in FalkorDB: it is no error
        *["0.0", "0.000", "( 0.0 )"],
    ],
)
def test_a_divisor_that_only_starts_with_zero_is_not_division_by_zero(
    falkordb_client, divisor
):
    valid, metadata = SyntaxValidator(falkordb_client).validate(
        f"MATCH (g:Goal) RETURN 10 / {divisor}", "kg"
    )

    assert (valid, metadata) == (True, [])


@pytest.mark.parametrize("divisor", ["0", "(0)", "(1 - 1)", "( 2 * 0 )", "0 + 1"])
def test_a_divisor_that_is_zero_is_division_by_zero(falkordb_client, divisor):
    valid, metadata = SyntaxValidator(falkordb_client).validate(
        f"MATCH (g:Goal) RETURN 10 / {divisor} AS x", "kg"
    )

    assert valid is False
    assert code_set(metadata) == {"cypher_guardrail.Error.ArithmeticError"}


# ----------------------------------------------------------------------------------------------------
# The branches of a UNION do not share variables


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n:Goal) RETURN n.code AS x UNION MATCH (n:Target) RETURN n.code AS x",
        "MATCH (n:Goal) RETURN n.code AS x UNION ALL MATCH (n:Target) RETURN n.code AS x",
        "MATCH (n:Goal) RETURN n.code AS x\nunion\nMATCH (n:Target) RETURN n.code AS x",
        (
            "MATCH (n:Goal) RETURN n.code AS x UNION MATCH (n:Target) RETURN n.code AS x "
            "UNION MATCH (n:Indicator) RETURN n.code AS x"
        ),
    ],
)
def test_a_variable_with_another_label_in_another_branch_is_not_a_conflict(
    falkordb_client, query
):
    valid, metadata = SyntaxValidator(falkordb_client).validate(query, "kg")

    assert (valid, metadata) == (True, [])


def test_a_conflict_inside_one_branch_of_a_union_is_still_one(falkordb_client):
    query = (
        "MATCH (n:Goal) RETURN n.code AS x UNION "
        "MATCH (n:Target) MATCH (n:Indicator) RETURN n.code AS x"
    )

    valid, metadata = SyntaxValidator(falkordb_client).validate(query, "kg")

    assert valid is False
    (issue,) = conflicts(metadata)
    assert issue["variables"] == {"n": ["Target", "Indicator"]}


def test_the_conflicts_of_several_branches_are_one_issue_with_all_of_them(
    falkordb_client,
):
    query = (
        "MATCH (a:Goal) MATCH (a:Target) RETURN a.code AS x UNION "
        "MATCH (b:Indicator) MATCH (b:Goal) RETURN b.code AS x"
    )

    valid, metadata = SyntaxValidator(falkordb_client).validate(query, "kg")

    assert valid is False
    (issue,) = conflicts(metadata)
    assert issue["variables"] == {"a": ["Goal", "Target"], "b": ["Indicator", "Goal"]}


def test_a_union_in_a_string_does_not_cut_the_query_for_the_conflict_check(
    falkordb_client,
):
    query = "MATCH (n:Goal) MATCH (n:Target) WHERE n.code = 'a UNION b' RETURN n"

    valid, metadata = SyntaxValidator(falkordb_client).validate(query, "kg")

    assert valid is False
    assert [i["code"] for i in metadata] == [
        "cypher_guardrail.Error.ConflictingVariables"
    ]


def test_a_variable_that_conflicts_in_two_branches_has_the_labels_of_both(
    falkordb_client,
):
    query = (
        "MATCH (n:Goal) MATCH (n:Target) RETURN n.code AS x UNION "
        "MATCH (n:Indicator) MATCH (n:Goal) RETURN n.code AS x"
    )

    valid, metadata = SyntaxValidator(falkordb_client).validate(query, "kg")

    assert valid is False
    (issue,) = conflicts(metadata)
    assert issue["variables"] == {"n": ["Goal", "Target", "Indicator"]}


# ----------------------------------------------------------------------------------------------------
# A name that a WITH dropped, or that a CALL { } does not import, is a new variable when it is declared again


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n:Goal) WITH n.name AS name MATCH (n:Target) RETURN n, name",
        "MATCH (n:Goal) WITH count(*) AS c MATCH (n:Target) RETURN n, c",
        "MATCH (n:Goal) CALL { MATCH (n:Target) RETURN count(*) AS c } RETURN c",
        "MATCH (n:Goal) CALL { MATCH (n:Target) RETURN n AS t } RETURN n, t",
        "CALL { MATCH (n:Goal) RETURN n AS g UNION MATCH (n:Target) RETURN n AS g } RETURN g",
    ],
)
def test_a_name_that_is_declared_again_in_another_scope_does_not_conflict(
    falkordb_client, query
):
    assert SyntaxValidator(falkordb_client, check_multilabeled_nodes=False).validate(
        query, "kg"
    ) == (True, [])


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n:Goal) WITH n MATCH (n:Target) RETURN n",
        "MATCH (n:Goal) WITH * MATCH (n:Target) RETURN n",
        "MATCH (n:Goal) CALL { WITH n MATCH (n:Target) RETURN count(*) AS c } RETURN c",
        "MATCH (n:Goal) MATCH (n:Target) RETURN n",
    ],
)
def test_a_name_that_is_carried_on_still_conflicts(falkordb_client, query):
    valid, metadata = SyntaxValidator(
        falkordb_client, check_multilabeled_nodes=False
    ).validate(query, "kg")

    assert valid is False
    assert [i["variables"] for i in conflicts(metadata)] == [{"n": ["Goal", "Target"]}]


def test_the_conflict_is_reported_under_the_name_the_query_gives(falkordb_client):
    query = (
        "MATCH (n:Goal) MATCH (n:Target) WITH count(*) AS c "
        "MATCH (n:Goal) MATCH (n:Indicator) RETURN n, c"
    )

    _, metadata = SyntaxValidator(
        falkordb_client, check_multilabeled_nodes=False
    ).validate(query, "kg")

    assert [i["variables"] for i in conflicts(metadata)] == [
        {"n": ["Goal", "Target", "Indicator"]}
    ]


# ----------------------------------------------------------------------------------------------------
# A query that only reads is not run to ask whether it can write


@pytest.fixture
def heavy_graph(falkordb_client):
    graph = falkordb_client.select_graph("heavy_read")
    graph.query("UNWIND range(1, 3000) AS i CREATE (:P {i: i})")
    yield "heavy_read"
    graph.delete()


HEAVY = "MATCH (a:P), (b:P), (c:P) WHERE a.i + b.i + c.i = -1 RETURN count(*)"


def test_a_query_that_would_run_for_a_minute_is_validated_in_a_moment(
    falkordb_client, heavy_graph
):
    # FalkorDB runs a read query that is sent as `EXPLAIN ...` in a read-only request: it took a minute here
    started = time.monotonic()

    valid, metadata = SyntaxValidator(falkordb_client).validate(HEAVY, heavy_graph)

    assert (valid, metadata) == (True, [])
    assert time.monotonic() - started < 10


def test_is_write_does_not_run_the_query_either(falkordb_client, heavy_graph):
    started = time.monotonic()

    assert is_write(falkordb_client, HEAVY, heavy_graph) is False
    assert time.monotonic() - started < 10


def test_a_heavy_query_that_writes_is_still_refused_as_a_write(
    falkordb_client, heavy_graph
):
    query = HEAVY.replace("RETURN count(*)", "CREATE (:Z) RETURN count(*)")

    valid, metadata = SyntaxValidator(falkordb_client).validate(query, heavy_graph)

    assert valid is False
    assert [i["code"] for i in metadata] == [WRITE_QUERY_ERROR]
