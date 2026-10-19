"""What a user sees: the names of the project, the fields of the issues, and the README's end-to-end checks."""

import importlib.metadata
import importlib.resources
import importlib.util
import json
import pathlib
import re
import typing
from typing import Any

import pytest

import cypher_guardrail
from cypher_guardrail import (
    AsyncPropertiesValidator,
    AsyncSchemaValidator,
    AsyncSyntaxValidator,
    PropertiesValidator,
    SchemaValidator,
    SyntaxValidator,
    backend,
    issues,
)
from cypher_guardrail.issues import (
    ARITHMETIC_ERROR,
    CONFLICTING_VARIABLES,
    EXECUTION_FAILED,
    INVALID_PROPERTY_ACCESS,
    PARAMETER_NOT_PROVIDED,
    PROPERTY_TYPE_MISMATCH,
    SYNTAX_ERROR,
    UNKNOWN_INDEX,
    UNKNOWN_LABEL,
    UNKNOWN_PATH,
    UNKNOWN_PROPERTY,
    UNKNOWN_REL_TYPE,
    VARIABLE_LENGTH_PATH,
    WRITE_QUERY_ERROR,
    WRITE_QUERY_WARNING,
    WRONG_DIRECTION_PATH,
)

# ----------------------------------------------------------------------------------------------------
# The fields of the issues in the metadata, so that nobody has to parse a description.
#
# Each case is a query, the validator and graph it is validated on, the code of the issue it gives and the
# fields that issue must have. The sync and the async validators must report the same issues.


VALIDATORS = {
    "syntax": (SyntaxValidator, AsyncSyntaxValidator),
    "schema": (SchemaValidator, AsyncSchemaValidator),
    "properties": (PropertiesValidator, AsyncPropertiesValidator),
}


# (validator, graph, query, code, the fields the issue has besides code and description)
CASES: dict[str, tuple[str, str, str, str, dict[str, Any]]] = {
    "unknown label": (
        "schema",
        "kg",
        "MATCH (g:Ghost)-[:HAS_TARGET]->(t:Target) RETURN g",
        UNKNOWN_LABEL,
        {"label": "Ghost"},
    ),
    "unknown relationship type": (
        "schema",
        "kg",
        "MATCH (g:Goal)-[:NOPE]->(t:Target) RETURN g",
        UNKNOWN_REL_TYPE,
        {"relationship_type": "NOPE"},
    ),
    "unknown property": (
        "properties",
        "kg",
        "MATCH (g:Goal) WHERE g.bogus = 1 RETURN g",
        UNKNOWN_PROPERTY,
        {"property": "bogus"},
    ),
    "invalid property access": (
        "properties",
        "kg",
        "MATCH (g:Goal) WHERE g.code = '1' AND g.bogus = 1 AND g.zzz = 2 RETURN g",
        INVALID_PROPERTY_ACCESS,
        {"kind": "node", "name": "Goal", "properties": ["bogus", "zzz"]},
    ),
    "parameters not provided": (
        "syntax",
        "kg",
        "MATCH (n) WHERE n.code = $code AND n.title = $title AND n.x = $code RETURN n",
        PARAMETER_NOT_PROVIDED,
        {"parameters": ["code", "title"]},
    ),
    "missing full-text index on nodes": (
        "schema",
        "kg",
        "CALL db.idx.fulltext.queryNodes('Goal', 'x') YIELD node RETURN node",
        UNKNOWN_INDEX,
        {"index": "fulltext", "on": "nodes", "label": "Goal", "attribute": None},
    ),
    "missing full-text index on relationships": (
        "schema",
        "kg",
        "CALL db.idx.fulltext.queryRelationships('HAS_TARGET', 'x') YIELD relationship RETURN relationship",
        UNKNOWN_INDEX,
        {
            "index": "fulltext",
            "on": "relationships",
            "label": "HAS_TARGET",
            "attribute": None,
        },
    ),
    "missing vector index": (
        "schema",
        "documents",
        "CALL db.idx.vector.queryNodes('Doc', 'nope', 3, vecf32([1.0, 2.0])) YIELD node RETURN node",
        UNKNOWN_INDEX,
        {"index": "vector", "on": "nodes", "label": "Doc", "attribute": "nope"},
    ),
    "conflicting variable": (
        "syntax",
        "kg",
        "MATCH (n:Goal) MATCH (n:Target) RETURN n",
        CONFLICTING_VARIABLES,
        {"variables": {"n": ["Goal", "Target"]}},
    ),
    "several conflicting variables are one issue": (
        "syntax",
        "kg",
        "MATCH (a:Goal) MATCH (a:Target) MATCH (b:Indicator) MATCH (b:Goal) RETURN a, b",
        CONFLICTING_VARIABLES,
        {"variables": {"a": ["Goal", "Target"], "b": ["Indicator", "Goal"]}},
    ),
    "unknown path": (
        "schema",
        "kg",
        "MATCH (g:Goal)-[:HAS_INDICATOR]->(i:Indicator) RETURN g",
        UNKNOWN_PATH,
        {
            "path": "(:Goal)-[:HAS_INDICATOR]->(:Indicator)",
            "original_path": "(g:Goal)-[:HAS_INDICATOR]->(i:Indicator)",
        },
    ),
    "variable length path": (
        "schema",
        "kg",
        "MATCH (g:Goal)-[:HAS_TARGET*2]->(t:Target) RETURN g",
        VARIABLE_LENGTH_PATH,
        {
            "path": "(:Goal)-[:HAS_TARGET*2]->(:Target)",
            "original_path": "(g:Goal)-[:HAS_TARGET*2]->(t:Target)",
            "suggested_path": "(g:Goal)-[:HAS_TARGET*]->(t:Target)",
        },
    ),
    "wrong direction path": (
        "schema",
        "kg",
        "MATCH (g:Goal)<-[:HAS_TARGET]-(t:Target) RETURN g",
        WRONG_DIRECTION_PATH,
        {
            "path": "(:Goal)<-[:HAS_TARGET]-(:Target)",
            "original_path": "(g:Goal)<-[:HAS_TARGET]-(t:Target)",
            "suggested_path": "(g:Goal)-[:HAS_TARGET]->(t:Target)",
        },
    ),
    "syntax error": ("syntax", "kg", "MATCH (n:Goal RETURN n", SYNTAX_ERROR, {}),
    "execution failed": (
        "syntax",
        "kg",
        "RETURN nonexistent_fn(1)",
        EXECUTION_FAILED,
        {},
    ),
    "division by zero": ("syntax", "kg", "RETURN 1 / 0", ARITHMETIC_ERROR, {}),
}


# the issue of each code, as the types of cypher_guardrail.issues describe it
TYPES = {
    typing.get_args(typing.get_type_hints(kind)["code"])[0]: kind
    for kind in typing.get_args(issues.Issue)
    if len(typing.get_args(typing.get_type_hints(kind)["code"])) == 1
}


def issues_of(validator, client, graph, query):
    return validator(client).validate(query, graph)[1]


async def async_issues_of(validator, client, graph, query):
    return (await validator(client).validate(query, graph))[1]


@pytest.fixture
def graphs(request, falkordb_client):
    request.getfixturevalue("documents")  # the graph of the vector index case
    return falkordb_client


@pytest.mark.parametrize("case", CASES)
def test_the_issue_has_the_fields_of_its_code(graphs, run_async, case):
    validator, graph, query, code, fields = CASES[case]
    sync_class, async_class = VALIDATORS[validator]

    found = [
        i for i in issues_of(sync_class, graphs, graph, query) if i["code"] == code
    ]
    assert len(found) == 1, found
    (issue,) = found

    assert issue == {"code": code, "description": issue["description"], **fields}
    # the description is made from the same fields, so it never disagrees with them
    for name in (
        "label",
        "relationship_type",
        "property",
        "path",
        "original_path",
        "suggested_path",
    ):
        if name in fields:
            assert fields[name] in issue["description"]
    for name in ("properties", "parameters"):
        if name in fields:
            assert ", ".join(fields[name]) in issue["description"]

    async def ask(client):
        return await async_issues_of(async_class, client, graph, query)

    assert [i for i in run_async(ask) if i["code"] == code] == [issue]


@pytest.mark.parametrize("case", CASES)
def test_the_fields_are_the_ones_the_type_of_the_code_declares(graphs, case):
    validator, graph, query, code, _ = CASES[case]
    sync_class, _ = VALIDATORS[validator]
    (issue,) = [
        i for i in issues_of(sync_class, graphs, graph, query) if i["code"] == code
    ]

    kind = TYPES.get(code, issues.DatabaseErrorIssue)
    assert set(issue) == set(typing.get_type_hints(kind))


def test_every_code_has_a_type_and_the_types_name_their_codes():
    constants = {
        ARITHMETIC_ERROR,
        CONFLICTING_VARIABLES,
        EXECUTION_FAILED,
        INVALID_PROPERTY_ACCESS,
        PARAMETER_NOT_PROVIDED,
        PROPERTY_TYPE_MISMATCH,
        SYNTAX_ERROR,
        UNKNOWN_INDEX,
        UNKNOWN_LABEL,
        UNKNOWN_PATH,
        UNKNOWN_PROPERTY,
        UNKNOWN_REL_TYPE,
        VARIABLE_LENGTH_PATH,
        WRITE_QUERY_ERROR,
        WRITE_QUERY_WARNING,
        WRONG_DIRECTION_PATH,
        issues.WRONG_DIRECTION_VARIABLE_LENGTH_PATH,
    }
    declared = set(TYPES) | set(
        typing.get_args(typing.get_type_hints(issues.DatabaseErrorIssue)["code"])
    )
    assert declared == constants
    assert len(constants) == 17


def test_different_issues_of_one_code_are_all_reported(graphs):
    query = "MATCH (a:Spirit)-[:HAS_TARGET]->(b:Ghost) RETURN a"
    found = [
        i
        for i in issues_of(SchemaValidator, graphs, "kg", query)
        if i["code"] == UNKNOWN_LABEL
    ]
    # the unknown labels of a pattern are reported in alphabetical order
    assert [i["label"] for i in found] == ["Ghost", "Spirit"]


def test_the_same_issue_is_reported_once(graphs):
    query = "MATCH (a:Ghost)-[:HAS_TARGET]->(b:Target), (c:Ghost)-[:HAS_TARGET]->(d:Target) RETURN a"
    found = [
        i
        for i in issues_of(SchemaValidator, graphs, "kg", query)
        if i["code"] == UNKNOWN_LABEL
    ]
    assert [i["label"] for i in found] == ["Ghost"]


def test_the_issues_can_be_turned_into_json_as_they_are(graphs):
    everything = [
        issue
        for validator, graph, query, _, _ in CASES.values()
        for issue in issues_of(VALIDATORS[validator][0], graphs, graph, query)
    ]
    assert len(everything) >= len(CASES)
    assert json.loads(json.dumps(everything)) == everything


def test_an_issue_is_a_copy_the_caller_may_change(graphs):
    query = "MATCH (g:Goal) WHERE g.bogus = 1 AND g.zzz = 2 RETURN g"
    first = issues_of(PropertiesValidator, graphs, "kg", query)
    for issue in first:
        issue.pop("description")
        for value in issue.values():
            if isinstance(value, list):
                value.clear()
    again = issues_of(PropertiesValidator, graphs, "kg", query)
    assert all(issue["description"] for issue in again)
    assert any(i.get("properties") == ["bogus", "zzz"] for i in again)


# ----------------------------------------------------------------------------------------------------
# From test_package_metadata.py: The names of the project agree: the distribution, the import name, the version and the metadata codes.


tomllib = pytest.importorskip("tomllib")  # Python >= 3.11


PYPROJECT = tomllib.loads(
    (pathlib.Path(__file__).parents[1] / "pyproject.toml").read_text()
)


def test_the_distribution_is_called_cypher_guardrail():
    assert PYPROJECT["project"]["name"] == "cypher-guardrail"
    assert importlib.metadata.metadata("cypher-guardrail")["Name"] == "cypher-guardrail"


def test_the_version_comes_from_the_installed_distribution():
    # __init__ looks the version up by distribution name; a wrong name silently gives "unknown"
    assert cypher_guardrail.__version__ != "unknown"
    assert (
        cypher_guardrail.__version__
        == importlib.metadata.version("cypher-guardrail")
        == PYPROJECT["project"]["version"]
    )


def test_the_import_name_is_the_module_name_in_pyproject():
    assert PYPROJECT["tool"]["uv"]["build-backend"]["module-name"] == "cypher_guardrail"
    assert pathlib.Path(cypher_guardrail.__file__).parent.name == "cypher_guardrail"


def test_the_codes_of_cypher_guardrail_share_one_namespace():
    ours = [
        backend.UNKNOWN_LABEL,
        backend.UNKNOWN_REL_TYPE,
        backend.UNKNOWN_PROPERTY,
        backend.UNKNOWN_INDEX,
        backend.PARAMETER_NOT_PROVIDED,
        backend.ARITHMETIC_ERROR,
    ]
    assert all(code.startswith("cypher_guardrail.") for code in ours)
    # the errors of the database keep the database's name
    assert backend.SYNTAX_ERROR.startswith("FalkorDB.")
    assert backend.EXECUTION_FAILED.startswith("FalkorDB.")


def test_the_old_name_is_gone():
    old = "Cy" + "Ver"
    assert importlib.util.find_spec(old) is None
    assert importlib.util.find_spec(old.lower()) is None


# ----------------------------------------------------------------------------------------------------
# From test_validators_falkordb.py: The three validators against a real FalkorDB (FalkorDBLite), with a client and with a bound graph.


def test_kg_valid_query_metric(driver, db_name):
    """The README's three-part 'KG Valid Query' check, end to end."""
    query = "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) WHERE g.code = '1' RETURN t"
    assert SyntaxValidator(driver).validate(query, db_name)[0] is True
    assert SchemaValidator(driver).validate(query, db_name)[0] == 1.0
    assert PropertiesValidator(driver).validate(query, db_name)[0] in (1.0, None)

    bad = "MATCH (g:Goal)<-[:HAS_TARGET]-(t:Target) WHERE g.text = 1 RETURN t"
    assert SchemaValidator(driver).validate(bad, db_name)[0] < 1
    assert PropertiesValidator(driver).validate(bad, db_name)[0] < 1


def test_stock_falkordb_client(plain_falkordb_client, db_name):
    """Same behaviour with the plain falkordb.FalkorDB client class, connected over a socket."""
    import falkordb

    assert type(plain_falkordb_client) is falkordb.FalkorDB
    query = "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) WHERE g.code = '1' RETURN t"
    assert SyntaxValidator(plain_falkordb_client).validate(query, db_name) == (True, [])
    assert SchemaValidator(plain_falkordb_client).validate(query, db_name) == (1.0, [])
    assert PropertiesValidator(plain_falkordb_client).validate(query, db_name) == (
        1.0,
        [],
    )
    assert not SyntaxValidator(plain_falkordb_client).validate(
        "MATCH (g:Goal RETURN g", db_name
    )[0]


# ----------------------------------------------------------------------------------------------------
# What every validator does with the driver and the graph name it is given

CALLS_NEEDING_A_GRAPH_NAME = [
    (SyntaxValidator, "validate"),
    (SchemaValidator, "validate"),
    (SchemaValidator, "extract"),
    (PropertiesValidator, "validate"),
    (PropertiesValidator, "extract"),
]


@pytest.mark.parametrize(("validator", "method"), CALLS_NEEDING_A_GRAPH_NAME)
def test_a_client_without_a_graph_name_is_an_error(falkordb_client, validator, method):
    # (a falkordb.Graph carries its name, and the async validators say the same: test_async)
    with pytest.raises(ValueError, match="graph name"):
        getattr(validator(falkordb_client), method)("MATCH (n) RETURN n")


@pytest.mark.parametrize(
    "validator", [SyntaxValidator, SchemaValidator, PropertiesValidator]
)
def test_a_validator_rejects_a_driver_that_is_not_a_falkordb_client(validator):
    with pytest.raises(TypeError):
        validator("not a driver")


# ----------------------------------------------------------------------------------------------------
# The package as it is shipped


def test_the_package_says_it_is_typed():
    assert (importlib.resources.files("cypher_guardrail") / "py.typed").is_file()


def test_what_the_package_imports_at_run_time_is_a_declared_dependency():
    import tomllib

    project = tomllib.loads(
        (pathlib.Path(__file__).parent.parent / "pyproject.toml").read_text()
    )["project"]
    declared = {
        re.split(r"[<>=!~ \[]", requirement, maxsplit=1)[0].lower()
        for requirement in project["dependencies"]
    }

    # redis is imported for its exceptions (it also comes with falkordb, but the package should not rely on that)
    assert {"regex", "pandas", "falkordb", "redis"} <= declared


@pytest.mark.parametrize(
    "validator",
    [
        cypher_guardrail.AsyncSyntaxValidator,
        cypher_guardrail.AsyncSchemaValidator,
        cypher_guardrail.AsyncPropertiesValidator,
    ],
)
def test_max_concurrency_is_keyword_only_in_every_async_validator(validator):
    import inspect

    parameter = inspect.signature(validator.__init__).parameters["max_concurrency"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == 8
