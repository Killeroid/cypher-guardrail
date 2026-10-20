"""The type check (``PropertiesValidator(check_types=True)``): the literals it finds, the rule it applies, the warning."""

import json
import random
import typing

import pytest

from cypher_guardrail import (
    AsyncPropertiesValidator,
    PropertiesValidator,
    Snapshot,
    issues,
)
from cypher_guardrail._internal.core.literals import Comparison, _tokens, comparisons
from cypher_guardrail._internal.core.valuetypes import NUMERIC, fits
from cypher_guardrail.issues import PROPERTY_TYPE_MISMATCH

# ----------------------------------------------------------------------------------------------------
# Finding the comparisons of a property with a literal (``cypher_guardrail._internal.core.literals``).
#
# The check that uses them reports a type mismatch, which has to be a real one, so the second half of this file is
# about what must *not* be found: assignments, expressions, parameters, other clauses' maps. No database is needed.


def found(query):
    """``(variable, label, property, value, value_type)`` of each comparison, in order."""
    return [
        (c.variable, c.label, c.property, c.value, c.value_type)
        for c in comparisons(query)
    ]


# --------------------------------------------------------------------------- what is found


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "MATCH (g:Goal) WHERE g.year = '2020' RETURN g",
            [("g", None, "year", "2020", "String")],
        ),
        ('MATCH (g) WHERE g.year = "2020"', [("g", None, "year", "2020", "String")]),
        ("MATCH (g) WHERE g.year = 2020", [("g", None, "year", "2020", "Integer")]),
        ("MATCH (g) WHERE g.score = 1.5", [("g", None, "score", "1.5", "Float")]),
        ("MATCH (g) WHERE g.score = 1e3", [("g", None, "score", "1e3", "Float")]),
        ("MATCH (g) WHERE g.score = 2.5E-3", [("g", None, "score", "2.5E-3", "Float")]),
        ("MATCH (g) WHERE g.n = 0x1F", [("g", None, "n", "0x1F", "Integer")]),
        # the E of a hexadecimal number is a digit, not an exponent
        ("MATCH (g) WHERE g.n = 0x1E", [("g", None, "n", "0x1E", "Integer")]),
        ("MATCH (g) WHERE g.n = 0xE", [("g", None, "n", "0xE", "Integer")]),
        ("MATCH (g) WHERE g.ok = true", [("g", None, "ok", "true", "Boolean")]),
        ("MATCH (g) WHERE g.ok = FALSE", [("g", None, "ok", "FALSE", "Boolean")]),
        ("MATCH (g) WHERE g.n = -3", [("g", None, "n", "-3", "Integer")]),
        ("MATCH (g) WHERE g.n >= -1.5", [("g", None, "n", "-1.5", "Float")]),
        ("MATCH (g) WHERE g.n = 3 RETURN g", [("g", None, "n", "3", "Integer")]),
        # wrapped in parentheses, as it is when it is one of several conditions
        ("MATCH (n) WHERE (n.age = 'x') RETURN n", [("n", None, "age", "x", "String")]),
        ("MATCH (n) WHERE ('x' = n.age) RETURN n", [("n", None, "age", "x", "String")]),
        (
            "MATCH (n) WHERE n.a = 1 AND (n.b = 'x' OR n.c = 2)",
            [
                ("n", None, "a", "1", "Integer"),
                ("n", None, "b", "x", "String"),
                ("n", None, "c", "2", "Integer"),
            ],
        ),
        ("MATCH (n) WHERE NOT (n.age = 5)", [("n", None, "age", "5", "Integer")]),
        (
            "MATCH (n) WHERE ((n.age >= 1) AND (n.age < 'z'))",
            [("n", None, "age", "1", "Integer"), ("n", None, "age", "z", "String")],
        ),
    ],
)
def test_a_property_compared_with_a_literal(query, expected):
    assert found(query) == expected


@pytest.mark.parametrize("operator", ["=", "<>", "!=", "<", ">", "<=", ">="])
def test_every_comparison_operator(operator):
    assert found(f"MATCH (g) WHERE g.n {operator} 5") == [
        ("g", None, "n", "5", "Integer")
    ]
    assert found(f"MATCH (g) WHERE 5 {operator} g.n") == [
        ("g", None, "n", "5", "Integer")
    ]


def test_the_literal_on_the_left():
    assert found("MATCH (g) WHERE '2020' = g.year") == [
        ("g", None, "year", "2020", "String")
    ]
    assert found("MATCH (g) WHERE 2020 < g.year AND g.year < 2030") == [
        ("g", None, "year", "2020", "Integer"),
        ("g", None, "year", "2030", "Integer"),
    ]


def test_the_members_of_a_list():
    assert found("MATCH (g) WHERE g.year IN [2020, '2021', 1.5, true]") == [
        ("g", None, "year", "2020", "Integer"),
        ("g", None, "year", "2021", "String"),
        ("g", None, "year", "1.5", "Float"),
        ("g", None, "year", "true", "Boolean"),
    ]
    assert found("MATCH (g) WHERE g.year NOT IN ['a', 'b']") == [
        ("g", None, "year", "a", "String"),
        ("g", None, "year", "b", "String"),
    ]
    assert found("MATCH (g) WHERE g.year IN [-1, -2]") == [
        ("g", None, "year", "-1", "Integer"),
        ("g", None, "year", "-2", "Integer"),
    ]


def test_the_members_that_are_not_literals_are_skipped_and_the_others_kept():
    assert found(
        "MATCH (g) WHERE g.year IN [$a, 2020, g.other, 'x', 1 + 2, [3], f(4, 5), 6]"
    ) == [
        ("g", None, "year", "2020", "Integer"),
        ("g", None, "year", "x", "String"),
        ("g", None, "year", "6", "Integer"),
    ]
    assert found("MATCH (g) WHERE g.year IN $years") == []
    assert found("MATCH (g) WHERE g.year IN []") == []


@pytest.mark.parametrize(
    "operator",
    ["STARTS WITH", "ENDS WITH", "CONTAINS", "starts with", "Starts   With", "=~"],
)
def test_the_string_operators_need_a_string_property(operator):
    assert found(f"MATCH (g) WHERE g.name {operator} 'ab'") == [
        ("g", None, "name", "ab", "String")
    ]


@pytest.mark.parametrize("operator", ["STARTS WITH", "CONTAINS", "=~"])
def test_a_string_operator_with_something_else_than_a_string_is_not_a_comparison(
    operator,
):
    assert found(f"MATCH (g) WHERE g.name {operator} 5") == []
    assert found(f"MATCH (g) WHERE g.name {operator} $p") == []


def test_the_properties_in_the_map_of_a_pattern():
    assert found("MATCH (g:Goal {year: 2020, name: 'a', ok: true}) RETURN g") == [
        ("g", "Goal", "year", "2020", "Integer"),
        ("g", "Goal", "name", "a", "String"),
        ("g", "Goal", "ok", "true", "Boolean"),
    ]
    assert found("MATCH (:Goal {year: '2020'}) RETURN 1") == [
        (None, "Goal", "year", "2020", "String")
    ]
    assert found("MATCH (a:A {n: -1})-[r:R {w: 1.5}]->(b:B {code: 'x'}) RETURN a") == [
        ("a", "A", "n", "-1", "Integer"),
        ("r", "R", "w", "1.5", "Float"),
        ("b", "B", "code", "x", "String"),
    ]


def test_a_map_says_whether_it_is_of_a_node_or_a_relationship():
    nodes = comparisons("MATCH (a:A {n: 1}) RETURN a")
    relationships = comparisons("MATCH ()-[r:R {n: 1}]->() RETURN r")

    assert [c.entity for c in nodes] == ["node"]
    assert [c.entity for c in relationships] == ["relationship"]


def test_a_comparison_of_a_variable_has_no_entity_and_no_label():
    (comparison,) = comparisons("MATCH (g) WHERE g.year = 1")

    assert comparison == Comparison("g", None, None, "year", "1", "Integer")


def test_back_quoted_names():
    assert found("MATCH (g:`Goal Kind`) WHERE g.`the year` = 1 AND `g`.`x` = 2") == [
        ("g", None, "the year", "1", "Integer"),
        ("g", None, "x", "2", "Integer"),
    ]
    assert found("MATCH (g:`Goal Kind` {`the year`: 1})") == [
        ("g", "Goal Kind", "the year", "1", "Integer")
    ]


def test_each_comparison_once_in_the_order_of_the_query():
    query = "MATCH (g:G {year: 1}) WHERE g.year = 1 AND g.year = 1 AND g.name = 'x' RETURN g"

    assert found(query) == [
        ("g", "G", "year", "1", "Integer"),
        ("g", None, "year", "1", "Integer"),
        ("g", None, "name", "x", "String"),
    ]


def test_strings_and_comments_hide_what_is_inside_them():
    assert found("MATCH (g) WHERE g.name = 'a = 1 AND g.year = 2020'") == [
        ("g", None, "name", "a = 1 AND g.year = 2020", "String")
    ]
    assert found("MATCH (g) // WHERE g.year = 1\nWHERE g.n = 2") == [
        ("g", None, "n", "2", "Integer")
    ]
    assert found("MATCH (g) /* WHERE g.year = 1 */ WHERE g.n = 2") == [
        ("g", None, "n", "2", "Integer")
    ]
    assert found("MATCH (g) WHERE g.q = 'it\\'s' AND g.n = 2") == [
        ("g", None, "q", "it\\'s", "String"),
        ("g", None, "n", "2", "Integer"),
    ]
    assert found('MATCH (g) WHERE g.q = "say \\"hi\\"" AND g.n = 2')[-1] == (
        "g",
        None,
        "n",
        "2",
        "Integer",
    )


def test_a_comparison_after_a_clause_that_writes_is_found_once_a_clause_reads_again():
    assert found("MATCH (g) SET g.a = 1 WITH g WHERE g.year = 2020 RETURN g") == [
        ("g", None, "year", "2020", "Integer")
    ]
    assert (
        found("CREATE (g:G {n: 1}) WITH g MATCH (h {n: 'x'}) RETURN h") == []
    )  # (h) has no label
    assert found("MERGE (g:G {n: 1}) RETURN g.year = 2020") == [
        ("g", None, "year", "2020", "Integer")
    ]


def test_a_comparison_can_be_in_a_case_in_a_return_or_in_an_optional_match():
    assert found("MATCH (g) RETURN CASE WHEN g.year = 2020 THEN 1 ELSE 0 END") == [
        ("g", None, "year", "2020", "Integer")
    ]
    assert found("OPTIONAL MATCH (g:G {n: 1}) RETURN g") == [
        ("g", "G", "n", "1", "Integer")
    ]
    assert found("MATCH (g) WITH g WHERE g.year = 1 RETURN g") == [
        ("g", None, "year", "1", "Integer")
    ]


# ---------------------------------------------------------------- what must not be found


@pytest.mark.parametrize(
    "query",
    [
        # an assignment, a creation, a deletion: not comparisons
        "MATCH (g:Goal) SET g.year = '2020'",
        "MATCH (g:Goal) SET g.year = '2020', g.name = 'x'",
        "MATCH (g:Goal) SET g += {year: '2020'}",
        "MATCH (g:Goal) WHERE g.n = 1 SET g.year = '2020'".replace(
            "WHERE g.n = 1 ", ""
        ),
        "CREATE (g:Goal {year: '2020'})",
        "CREATE (a:A {n: 1})-[:R {w: 'x'}]->(b:B {m: 2})",
        "MERGE (g:Goal {year: '2020'})",
        "MERGE (g:Goal {code: 'x'}) ON CREATE SET g.year = '2020' ON MATCH SET g.seen = true",
        "MATCH (g:Goal) REMOVE g.year",
        "MATCH (g:Goal) DETACH DELETE g",
        "MATCH (g) FOREACH (x IN [1] | SET g.year = 'a')",
        # a parameter, another property, a variable, a function: not a literal
        "MATCH (g) WHERE g.year = $year",
        "MATCH (g) WHERE g.year = g.other",
        "MATCH (g) WHERE g.year = year",
        "MATCH (g) WHERE g.year = toInteger('2020')",
        "MATCH (g) WHERE g.year = null",
        "MATCH (g) WHERE g.year = NULL",
        "MATCH (g) WHERE g.year = date('2020-01-01')",
        "MATCH (g) WHERE g.year = [1, 2]",
        "MATCH (g) WHERE g.year = {a: 1}",
        "MATCH (g:Goal {year: $year})",
        "MATCH (g:Goal {year: other.year})",
        "MATCH (g:Goal {year: 1 + 2})",
        "MATCH (g:Goal {year: toInteger('1')})",
        # an expression on one side or the other
        "MATCH (g) WHERE g.year = 2019 + 1",
        "MATCH (g) WHERE g.year = 2021 - 1",
        "MATCH (g) WHERE g.year = 2 * 3",
        "MATCH (g) WHERE g.year + 1 = 2021",
        "MATCH (g) WHERE g.year * 2 = 4040",
        "MATCH (g) WHERE 1 + g.year = 2021",
        "MATCH (g) WHERE -g.year = 5",
        "MATCH (g) WHERE 2021 = g.year + 1",
        "MATCH (g) WHERE 1 + 2 = g.year",
        "MATCH (g) WHERE 2 * 3 = g.year",
        "MATCH (g) WHERE 10 - 2 < g.year",
        "MATCH (g) WHERE toString(g.year) = '2020'",
        "MATCH (g) WHERE size(g.tags) = 2",
        "MATCH (g) WHERE g.tags[0] = 'a'",
        "MATCH (g) WHERE g.name.length = 3",
        "MATCH (g) WHERE g.m.x = 3",
        "MATCH (g) WHERE g.name = 'a' + 'b'",
        "MATCH (g) WHERE g.name = 'abc'.length",
        "MATCH (g) WHERE db.idx.fulltext.queryNodes('Goal', 'x') = 1",
        "CALL db.idx.vector.queryNodes('Doc', 'embedding', 3, vecf32([1.0, 2.0])) YIELD node RETURN node",
        # a property that is only mentioned
        "MATCH (g) WHERE g.year IS NOT NULL RETURN g.year",
        "MATCH (g) RETURN g.year, g.name ORDER BY g.year",
        "MATCH (g) WHERE EXISTS(g.year)",
        "MATCH (g) WHERE g.year IN $years",
        # a pattern whose label is not one label
        "MATCH (g:A:B {year: 1})",
        "MATCH ()-[r:R*1..2 {w: 1}]->()",
        "MATCH ()-[r:R|S {w: 1}]->()",
        "MATCH (g {year: 1})",
        "MATCH ({year: 1})",
        # nothing to find
        "",
        "   ",
        "MATCH (g) RETURN g",
        "RETURN 1 = 1",
        "RETURN 'a' = 'a'",
        "UNWIND [1, 2] AS x RETURN x = 1",
    ],
)
def test_what_is_not_a_comparison_of_a_property_with_a_literal(query):
    assert found(query) == []


def test_a_keyword_used_as_a_name_is_not_a_clause():
    # `set` and `create` are a property and a label here; they must not make the rest a write
    assert found("MATCH (g:Create) WHERE g.set = 1 AND g.merge = 'x'") == [
        ("g", None, "set", "1", "Integer"),
        ("g", None, "merge", "x", "String"),
    ]
    assert found("MATCH (g {set: 1}) WHERE g.n = 2") == [
        ("g", None, "n", "2", "Integer")
    ]
    assert found("MATCH (g:`Set`) WHERE g.n = 2") == [("g", None, "n", "2", "Integer")]


def test_a_keyword_in_a_string_is_not_a_clause():
    assert found("MATCH (g) WHERE g.a = 'SET' AND g.n = 2") == [
        ("g", None, "a", "SET", "String"),
        ("g", None, "n", "2", "Integer"),
    ]


def test_the_arrow_of_a_pattern_is_not_a_comparison():
    assert found("MATCH (a)-[r]->(b) WHERE a.n = 1") == [
        ("a", None, "n", "1", "Integer")
    ]
    assert found("MATCH (a)<-[r]-(b) WHERE b.n = 1") == [
        ("b", None, "n", "1", "Integer")
    ]


# ----------------------------------------------------------------------------- the tokens


def test_the_tokens():
    tokens = _tokens("n.`a b` >= -1.5e3 AND $p <> 'x' // c")

    assert tokens == [
        ("id", "n"),
        ("punct", "."),
        ("qid", "a b"),
        ("op", ">="),
        ("punct", "-"),
        ("num", "1.5e3"),
        ("id", "AND"),
        ("param", "$p"),
        ("op", "<>"),
        ("str", "x"),
    ]


def test_an_unfinished_string_or_name_or_comment_does_not_break_anything():
    for query in [
        "MATCH (g) WHERE g.a = 'abc",
        'MATCH (g) WHERE g.a = "abc',
        "MATCH (`g",
        "MATCH (g) /* x",
        "'",
    ]:
        assert isinstance(comparisons(query), list)
    assert found("MATCH (g) WHERE g.a = 'abc") == [("g", None, "a", "abc", "String")]


def test_whatever_it_is_given_it_does_not_raise():
    pieces = [
        "MATCH", "WHERE", "SET", "CREATE", "(", ")", "[", "]", "{", "}", ":", ",", ".", "=", "<>", "<", ">", "-",
        "+", "n", "g", "year", "'x'", '"y"', "1", "2.5", "true", "null", "$p", "IN", "NOT", "STARTS", "WITH", "`q`",
        "//", "/*", "*/", "\n", "RETURN", "|", "*", "=~",
    ]  # fmt: skip
    rng = random.Random(7)
    for _ in range(3000):
        query = " ".join(rng.choice(pieces) for _ in range(rng.randint(0, 14)))
        for comparison in comparisons(query):
            assert comparison.value_type in {"String", "Integer", "Float", "Boolean"}
            assert comparison.property


# ----------------------------------------------------------------------------------------------------
# When a literal in a query can match the values a property has: ``fits`` (``cypher_guardrail._internal.core.valuetypes``).


@pytest.mark.parametrize(
    ("literal_type", "observed", "expected"),
    [
        ("String", ("String",), True),
        ("String", ("Integer",), False),
        ("Integer", ("Integer",), True),
        ("Boolean", ("Integer", "String"), False),
        ("Boolean", ("Boolean", "String"), True),
        # mixed data: any of the types is enough
        ("String", ("Integer", "String"), True),
        ("Integer", ("Integer", "String"), True),
        # integers and floats compare as numbers in FalkorDB (2020 = 2020.0)
        ("Integer", ("Float",), True),
        ("Float", ("Integer",), True),
        ("Float", ("Float",), True),
        ("Integer", ("String", "Float"), True),
        ("Float", ("String",), False),
        # a string is not a number, whatever it looks like
        ("String", ("Integer", "Float"), False),
        # a property with no value fits nothing
        ("String", (), False),
        ("Integer", (), False),
        # other types are compared by name
        ("String", ("Date",), False),
        ("String", ("Vectorf32",), False),
    ],
)
def test_when_a_literal_fits_the_types_of_a_property(literal_type, observed, expected):
    assert fits(literal_type, observed) is expected
    assert fits(literal_type, list(observed)) is expected
    assert fits(literal_type, set(observed)) is expected


def test_the_numbers_are_the_integers_and_the_floats():
    assert {"Integer", "Float"} == NUMERIC


# ----------------------------------------------------------------------------------------------------
# ``PropertiesValidator(check_types=True)``: a property compared with a literal of a type that none of its values has.
#
# FalkorDB does not report ``WHERE g.code = 1`` when ``code`` holds strings: it finds nothing. With ``check_types=True``
# the validator warns (``cypher_guardrail.Warning.PropertyTypeMismatch``), in every mode: online (the database says what types
# the values have), offline (a ``Snapshot`` captured with ``types=True`` does) and hybrid (both).


MISMATCH = "cypher_guardrail.Warning.PropertyTypeMismatch"


GOAL = "MATCH (g:Goal) WHERE g.code = 1 RETURN g"  # Goal.code holds strings


def mismatches(metadata):
    return [i for i in metadata if i["code"] == MISMATCH]


@pytest.fixture(scope="module")
def kg(falkordb_client):
    return Snapshot.capture(falkordb_client, "kg", types=True)


def validators(client, snapshot):
    """The same validator in every mode: name -> a function of the query."""
    online = PropertiesValidator(client, check_types=True)
    offline = PropertiesValidator(snapshot, check_types=True)
    hybrid = PropertiesValidator(client, snapshot=snapshot, check_types=True)
    never = PropertiesValidator(
        client, snapshot=snapshot, verify="never", check_types=True
    )
    return {
        "online": lambda q: online.validate(q, "kg"),
        "offline": lambda q: offline.validate(q),
        "hybrid": lambda q: hybrid.validate(q, "kg"),
        "hybrid-never": lambda q: never.validate(q, "kg"),
    }


# --------------------------------------------------------------------------------------- what is reported


def test_a_property_compared_with_a_literal_of_another_type_is_reported(
    falkordb_client,
):
    score, metadata = PropertiesValidator(falkordb_client, check_types=True).validate(
        GOAL, "kg"
    )

    (issue,) = mismatches(metadata)
    assert issue == {
        "code": MISMATCH,
        "description": issue["description"],
        "kind": "node",
        "name": "Goal",
        "property": "code",
        "expected_types": ["String"],
        "value": "1",
        "value_type": "Integer",
    }
    assert "code" in issue["description"]
    assert "Goal" in issue["description"]
    assert "String" in issue["description"]
    assert "Integer" in issue["description"]
    assert score == 1.0  # a warning: the property exists, so the score is as it was


def test_the_check_is_off_unless_asked_for(falkordb_client):
    assert (
        mismatches(PropertiesValidator(falkordb_client).validate(GOAL, "kg")[1]) == []
    )
    assert (
        mismatches(
            PropertiesValidator(falkordb_client, check_types=False).validate(
                GOAL, "kg"
            )[1]
        )
        == []
    )


def test_asking_for_the_check_changes_nothing_but_the_new_warning(falkordb_client):
    queries = [
        GOAL,
        "MATCH (g:Goal) WHERE g.code = '1' RETURN g.title",
        "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) WHERE g.nope = 1 AND t.code = 2 RETURN g",
        "MATCH (g) WHERE g.code = 1 RETURN g",
    ]
    for query in queries:
        plain = PropertiesValidator(falkordb_client).validate(query, "kg")
        checked = PropertiesValidator(falkordb_client, check_types=True).validate(
            query, "kg"
        )

        assert checked[0] == plain[0], query
        assert [i for i in checked[1] if i["code"] != MISMATCH] == plain[1], query


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # strings
        (
            "MATCH (g:Goal) WHERE g.code = 1 RETURN g",
            [("Goal", "code", "1", "Integer")],
        ),
        (
            "MATCH (g:Goal) WHERE g.code = 1.5 RETURN g",
            [("Goal", "code", "1.5", "Float")],
        ),
        (
            "MATCH (g:Goal) WHERE g.code = true RETURN g",
            [("Goal", "code", "true", "Boolean")],
        ),
        (
            "MATCH (g:Goal) WHERE 1 = g.code RETURN g",
            [("Goal", "code", "1", "Integer")],
        ),
        (
            "MATCH (g:Goal) WHERE g.code IN [1, '1', 2] RETURN g",
            [
                ("Goal", "code", "1", "Integer"),
                ("Goal", "code", "2", "Integer"),
            ],
        ),
        (
            "MATCH (g:Goal) WHERE g.code >= 1 RETURN g",
            [("Goal", "code", "1", "Integer")],
        ),
        # a pattern's map, of a node and of a relationship
        ("MATCH (g:Goal {code: 1}) RETURN g", [("Goal", "code", "1", "Integer")]),
        (
            "MATCH ()-[r:HAS_INDICATOR {weight: 'x'}]->() RETURN r",
            [("HAS_INDICATOR", "weight", "x", "String")],
        ),
        (
            "MATCH (a)-[r:HAS_INDICATOR]->(b) WHERE r.weight = 'x' RETURN r",
            [("HAS_INDICATOR", "weight", "x", "String")],
        ),
        # the weight is an integer: a string is not one
        (
            "MATCH ()-[r:HAS_INDICATOR]->() WHERE r.weight = '1' RETURN r",
            [("HAS_INDICATOR", "weight", "1", "String")],
        ),
        # a property of a string type used with a string operator is fine; of another type it is not
        ("MATCH (g:Goal) WHERE g.code STARTS WITH 'a' RETURN g", []),
        (
            "MATCH ()-[r:HAS_INDICATOR]->() WHERE r.weight STARTS WITH 'a' RETURN r",
            [("HAS_INDICATOR", "weight", "a", "String")],
        ),
        # two labels at once
        (
            "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) WHERE g.code = 1 AND t.code = 2 RETURN g",
            [
                ("Goal", "code", "1", "Integer"),
                ("Target", "code", "2", "Integer"),
            ],
        ),
    ],
)
def test_what_is_reported(falkordb_client, query, expected):
    _, metadata = PropertiesValidator(falkordb_client, check_types=True).validate(
        query, "kg"
    )

    assert [
        (i["name"], i["property"], i["value"], i["value_type"])
        for i in mismatches(metadata)
    ] == expected


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (g:Goal) WHERE g.code = '1' RETURN g",  # the right type
        "MATCH (g:Goal {code: '1'}) RETURN g",
        "MATCH ()-[r:HAS_INDICATOR]->() WHERE r.weight = 1 RETURN r",
        "MATCH ()-[r:HAS_INDICATOR]->() WHERE r.weight = 1.5 RETURN r",  # an integer and a float are numbers
        "MATCH ()-[r:HAS_INDICATOR]->() WHERE r.weight >= 0 AND r.weight < 10.0 RETURN r",
        "MATCH (g:Goal) WHERE g.code = $code RETURN g",  # a parameter
        "MATCH (g:Goal) WHERE g.code = g.title RETURN g",
        "MATCH (g:Goal) WHERE g.code = toString(1) RETURN g",
        "MATCH (g:Goal) WHERE g.code = null RETURN g",
        "MATCH (g:Goal) WHERE g.code = 1 + 1 RETURN g",
        "MATCH (g:Goal) WHERE g.code IS NOT NULL RETURN g",
        "MATCH (g:Goal) WHERE g.code IN $codes RETURN g",
        # a write is not a comparison
        "MATCH (g:Goal) SET g.code = 1",
        "CREATE (g:Goal {code: 1})",
        "MERGE (g:Goal {code: 1})",
        # a variable whose label is not in the query is not guessed
        "MATCH (g) WHERE g.code = 1 RETURN g",
        "MATCH (g)-[:HAS_TARGET]->(t) WHERE g.code = 1 RETURN g",
        # one variable has a label and the other has none: the other is not given the label of the first
        "MATCH (g:Goal)-[:HAS_TARGET]->(t) WHERE t.code = 1 RETURN g",
        # a label or a property that is not in the graph: other warnings say so
        "MATCH (g:Ghost) WHERE g.code = 1 RETURN g",
        "MATCH (g:Goal) WHERE g.nope = 1 RETURN g",
        "MATCH (g:Goal) WHERE g.year = 'x' RETURN g",
        # a label that is a relationship type is not a label of nodes
        "MATCH (g:HAS_TARGET) WHERE g.weight = 'x' RETURN g",
        "RETURN 1",
        "",
    ],
)
def test_what_is_not_reported(falkordb_client, query):
    _, metadata = PropertiesValidator(falkordb_client, check_types=True).validate(
        query, "kg"
    )

    assert mismatches(metadata) == []


def test_a_mismatch_is_reported_once_however_often_the_query_makes_it(falkordb_client):
    query = "MATCH (g:Goal {code: 1}) WHERE g.code = 1 AND g.code = 1 AND 1 = g.code RETURN g"

    _, metadata = PropertiesValidator(falkordb_client, check_types=True).validate(
        query, "kg"
    )

    assert len(mismatches(metadata)) == 1


def test_the_warning_comes_after_the_others_and_keeps_the_score(falkordb_client):
    query = "MATCH (g:Goal) WHERE g.nope = 1 AND g.code = 1 RETURN g"

    score, metadata = PropertiesValidator(falkordb_client, check_types=True).validate(
        query, "kg"
    )

    assert score == 0.5
    assert [i["code"] for i in metadata][-1] == MISMATCH
    assert len(metadata) > 1


def test_the_issue_is_what_its_type_declares_and_is_plain_data(falkordb_client):
    _, metadata = PropertiesValidator(falkordb_client, check_types=True).validate(
        GOAL, "kg"
    )
    (issue,) = mismatches(metadata)

    assert set(issue) == set(typing.get_type_hints(issues.PropertyTypeMismatchIssue))
    assert issues.PropertyTypeMismatchIssue in typing.get_args(issues.Issue)
    assert PROPERTY_TYPE_MISMATCH == MISMATCH
    assert json.loads(json.dumps(issue)) == issue
    issue["expected_types"].append(
        "changed"
    )  # a copy: the next validation is not affected
    assert mismatches(
        PropertiesValidator(falkordb_client, check_types=True).validate(GOAL, "kg")[1]
    )[0]["expected_types"] == ["String"]


# ------------------------------------------------------------------------------------- the three modes


QUERIES = [
    GOAL,
    "MATCH (g:Goal) WHERE g.code = '1' RETURN g",
    "MATCH (g:Goal {code: 1}) RETURN g",
    "MATCH ()-[r:HAS_INDICATOR {weight: 'x'}]->() RETURN r",
    "MATCH ()-[r:HAS_INDICATOR]->() WHERE r.weight = 1.5 RETURN r",
    "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) WHERE g.code = 1 AND t.text = 2 RETURN g",
    "MATCH (g) WHERE g.code = 1 RETURN g",
    "MATCH (g:Goal) SET g.code = 1",
]


def test_every_mode_gives_the_online_answer(falkordb_client, kg):
    modes = validators(falkordb_client, kg)

    for query in QUERIES:
        expected = modes["online"](query)
        for name, validate in modes.items():
            assert validate(query) == expected, (name, query)
    assert any(mismatches(modes["online"](q)[1]) for q in QUERIES)


def test_the_async_validators_give_the_same_answers(falkordb_client, kg, run_async):
    modes = validators(falkordb_client, kg)
    expected = [modes["online"](q) for q in QUERIES]

    async def ask(client):
        online = AsyncPropertiesValidator(client, check_types=True)
        offline = AsyncPropertiesValidator(kg, check_types=True)
        hybrid = AsyncPropertiesValidator(client, snapshot=kg, check_types=True)
        return [
            [
                await online.validate(q, "kg"),
                await offline.validate(q),
                await hybrid.validate(q, "kg"),
            ]
            for q in QUERIES
        ]

    for want, got in zip(expected, run_async(ask), strict=True):
        assert got == [want, want, want]


def test_offline_never_asks_the_database(falkordb_client, kg, requests):
    PropertiesValidator(kg, check_types=True).validate(GOAL)

    assert requests == []


def test_hybrid_asks_nothing_for_a_comparison_that_fits(falkordb_client, kg, requests):
    hybrid = PropertiesValidator(falkordb_client, snapshot=kg, check_types=True)
    hybrid.validate("MATCH (g:Goal) WHERE g.code = '1' RETURN g.title", "kg")

    assert requests == []


def test_online_asks_what_the_types_are_once_for_each_property(
    falkordb_client, requests
):
    query = "MATCH (g:Goal) WHERE g.code = 1 AND g.code = 2 AND g.title = 3 RETURN g"
    PropertiesValidator(falkordb_client, check_types=True).validate(query, "kg")

    typeofs = [r for r in requests if "typeof" in getattr(r, "query", "")]
    # code with an integer, and title with an integer: the value is not part of the question, its type is
    assert len(typeofs) == 2
    assert len({r.query for r in typeofs}) == 2


def test_the_check_costs_nothing_when_no_property_is_compared_with_a_literal(
    falkordb_client, requests
):
    query = "MATCH (g:Goal) WHERE g.code = $code RETURN g.title"
    PropertiesValidator(falkordb_client, check_types=True).validate(query, "kg")
    with_check = len(requests)
    requests.clear()
    PropertiesValidator(falkordb_client).validate(query, "kg")

    assert with_check == len(requests)


def test_a_snapshot_without_the_types_says_nothing_offline_and_asks_the_database_in_the_hybrid(
    falkordb_client, requests
):
    untyped = Snapshot.capture(falkordb_client, "kg")
    assert not untyped.has_property_types

    offline = PropertiesValidator(untyped, check_types=True).validate(GOAL)
    hybrid = PropertiesValidator(falkordb_client, snapshot=untyped, check_types=True)
    answer = hybrid.validate(GOAL, "kg")

    assert mismatches(offline[1]) == []
    assert len(mismatches(answer[1])) == 1
    assert any("typeof" in getattr(r, "query", "") for r in requests)


# ------------------------------------------------------------------ mixed data, and a snapshot that went stale


@pytest.fixture
def lab(falkordb_client):
    graph = falkordb_client.select_graph("typed_lab")
    graph.query(
        "CREATE (:Doc {year: 2020, tag: 'a'}), (:Doc {year: '2021'}), (:Doc {year: 2.5})"
    )
    yield falkordb_client
    graph.delete()


def test_a_property_with_several_types_accepts_a_literal_of_any_of_them(lab):
    snapshot = Snapshot.capture(lab, "typed_lab", types=True)
    assert snapshot.property_types_of("Doc")["year"] == ("Float", "Integer", "String")
    modes = [
        PropertiesValidator(lab, check_types=True),
        PropertiesValidator(snapshot, check_types=True),
        PropertiesValidator(lab, snapshot=snapshot, check_types=True),
    ]

    for validator in modes:
        for value in ("2020", "'2021'", "2.5", "1e3"):
            _, metadata = (
                validator.validate(
                    f"MATCH (d:Doc) WHERE d.year = {value} RETURN d", "typed_lab"
                )
                if validator.mode != "offline"
                else validator.validate(
                    f"MATCH (d:Doc) WHERE d.year = {value} RETURN d"
                )
            )
            assert mismatches(metadata) == [], (validator.mode, value)
        _, metadata = (
            validator.validate(
                "MATCH (d:Doc) WHERE d.year = true RETURN d", "typed_lab"
            )
            if validator.mode != "offline"
            else validator.validate("MATCH (d:Doc) WHERE d.year = true RETURN d")
        )
        (issue,) = mismatches(metadata)
        assert issue["expected_types"] == ["Float", "Integer", "String"]


def test_a_stale_snapshot_is_wrong_offline_and_right_in_the_hybrid(lab):
    snapshot = Snapshot.capture(lab, "typed_lab", types=True)
    # since the capture, the tags have been given numbers too
    lab.select_graph("typed_lab").query("CREATE (:Doc {tag: 7})")
    query = "MATCH (d:Doc) WHERE d.tag = 7 RETURN d"

    online = PropertiesValidator(lab, check_types=True).validate(query, "typed_lab")
    offline = PropertiesValidator(snapshot, check_types=True).validate(query)
    hybrid = PropertiesValidator(lab, snapshot=snapshot, check_types=True)
    never = PropertiesValidator(
        lab, snapshot=snapshot, verify="never", check_types=True
    )

    assert mismatches(online[1]) == []
    assert len(mismatches(offline[1])) == 1  # it has not seen a number there
    assert (
        hybrid.validate(query, "typed_lab") == online
    )  # a mismatch is checked before it is reported
    assert hybrid.stats.corrected_misses == 1
    assert never.validate(query, "typed_lab") == offline


def test_a_snapshot_written_by_hand_with_types(falkordb_client):
    by_hand = Snapshot.from_schema(
        nodes={"Goal": ["code", "year"]},
        relationships=[],
        node_property_types={"Goal": {"code": ["String"], "year": ["Integer"]}},
    )

    _, metadata = PropertiesValidator(by_hand, check_types=True).validate(
        "MATCH (g:Goal) WHERE g.code = 1 AND g.year = '2020' RETURN g"
    )

    assert [(i["property"], i["expected_types"]) for i in mismatches(metadata)] == [
        ("code", ["String"]),
        ("year", ["Integer"]),
    ]


def test_a_property_of_a_relationship_type_and_a_label_of_the_same_name_do_not_mix(
    falkordb_client,
):
    graph = falkordb_client.select_graph("typed_same_name")
    graph.query("CREATE (:K {n: 'a'})-[:K {n: 1}]->(:Z)")
    try:
        snapshot = Snapshot.capture(falkordb_client, "typed_same_name", types=True)
    finally:
        graph.delete()

    assert snapshot.property_types_of("K", "NODE") == {"n": ("String",)}
    assert snapshot.property_types_of("K", "RELATIONSHIP") == {"n": ("Integer",)}
    relationship = PropertiesValidator(snapshot, check_types=True).validate(
        "MATCH ()-[r:K {n: 1}]->() RETURN r"
    )
    assert mismatches(relationship[1]) == []


def test_alternatives_of_relationship_types_are_checked_once_they_narrow_to_one(
    falkordb_client,
):
    validator = PropertiesValidator(falkordb_client, check_types=True)
    # only HAS_INDICATOR has a weight: the alternatives narrow to it, and its weight is an integer
    narrowed = validator.validate(
        "MATCH ()-[r:HAS_TARGET|HAS_INDICATOR]->() WHERE r.weight = 'x' RETURN r", "kg"
    )
    fits = validator.validate(
        "MATCH ()-[r:HAS_TARGET|HAS_INDICATOR]->() WHERE r.weight = 1 RETURN r", "kg"
    )
    # neither has it: the alternatives stay, and nothing is said about the type
    tie = validator.validate(
        "MATCH ()-[r:HAS_TARGET|HAS_NUTS1]->() WHERE r.weight = 'x' RETURN r", "kg"
    )

    assert [(i["kind"], i["name"]) for i in mismatches(narrowed[1])] == [
        ("relationship", "HAS_INDICATOR")
    ]
    assert mismatches(fits[1]) == []
    assert mismatches(tie[1]) == []


def test_a_label_the_graph_does_not_have_is_not_asked_about(falkordb_client, requests):
    query = "MATCH (g:Ghost) WHERE g.code = 1 RETURN g"

    PropertiesValidator(falkordb_client, check_types=True).validate(query, "kg")

    assert [r for r in requests if "typeof" in getattr(r, "query", "")] == []


# ----------------------------------------------------------------------------------------------------
# The branches of a UNION do not share variables


def test_a_comparison_is_checked_against_the_label_of_its_own_branch(falkordb_client):
    # n is a Goal in the first branch and a Target in the second; both have a `code` that holds strings
    query = (
        "MATCH (n:Goal) WHERE n.code = 1 RETURN n.code AS c "
        "UNION MATCH (n:Target) WHERE n.code = '1' RETURN n.code AS c"
    )

    _, metadata = PropertiesValidator(falkordb_client, check_types=True).validate(
        query, "kg"
    )

    assert [(i["name"], i["value"]) for i in mismatches(metadata)] == [("Goal", "1")]


def test_every_branch_of_a_union_has_its_comparisons_checked(falkordb_client):
    query = (
        "MATCH (n:Goal) WHERE n.code = 1 RETURN n.code AS c "
        "UNION ALL MATCH (n:Target) WHERE n.code = 2 RETURN n.code AS c "
        "UNION ALL MATCH (n:Indicator) WHERE n.code = '3' RETURN n.code AS c"
    )

    _, metadata = PropertiesValidator(falkordb_client, check_types=True).validate(
        query, "kg"
    )

    assert [i["name"] for i in mismatches(metadata)] == ["Goal", "Target"]
