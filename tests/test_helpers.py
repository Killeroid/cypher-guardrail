"""The Cypher text helpers: patterns, parsing helpers, and the validators' inputs that are hard on a regex."""

import random
import time
from types import SimpleNamespace

import pandas as pd
import pytest
import regex as re

from cypher_guardrail import SchemaValidator, patterns
from cypher_guardrail._internal.core.io import run_sync
from cypher_guardrail._internal.sources import online as _online
from cypher_guardrail.patterns import (
    IndexQuery,
    index_procedure_calls,
    mask_properties,
    strip_literals_and_comments,
    unmask_properties,
)
from cypher_guardrail.utils import (
    get_paths_after_inference,
    identify_variables_and_construct_query,
    replace_with_labels,
)
from cypher_guardrail.validators import properties_validator, schema_validator

# ----------------------------------------------------------------------------------------------------
# The Cypher patterns and text helpers: they match what the original patterns matched, and survive pathological input.


# (compiled pattern, source it was compiled from, findall flags)
PATTERNS = {
    "NODE": (patterns.NODE, patterns.NODE_SOURCE),
    "REL": (patterns.REL, patterns.REL_SOURCE),
    "PATH_ORIGINAL": (patterns.PATH_ORIGINAL, patterns.PATH_ORIGINAL_SOURCE),
    "PATH_EDITED": (patterns.PATH_EDITED, patterns.PATH_EDITED_SOURCE),
    "LABELED_NODE": (patterns.LABELED_NODE, patterns.LABELED_NODE_SOURCE),
    "LABELED_REL": (patterns.LABELED_REL, patterns.LABELED_REL_SOURCE),
}


HAND_WRITTEN = [
    "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g, t",
    "MATCH (g:Goal)<-[:HAS_TARGET]-(t:Target) RETURN g",
    "MATCH (a)-[r]->(b) RETURN a",
    "MATCH (a)--(b)-->(c)<--(d) RETURN a",
    "MATCH ( g : Goal { code : '1' } ) - [ r : HAS_TARGET | OTHER * 1 .. 3 ] -> ( t ) RETURN g",
    "MATCH (n:!Goal|!Target)-[:!HAS]-(m) RETURN n",
    "MATCH (:A)-[:R*]->(:B)-[:S*2]->(:C) RETURN 1",
    "MATCH (g:Goal {code:'1-2', t:'a -> b'})<-[:R]-() RETURN g",
    "MATCH ()-[]-() RETURN 1",
    "MATCH (",
    "MATCH (g:Goal)-[",
    "(a:B)-[:C]->(d:E) (f:G)-[:H]->(i:J)",
    "   (  )   -  [  ]  -  (  )   ",
]


TOKENS = [
    "(", ")", "[", "]", "{", "}", "-", "->", "<-", "--", ":", "|", "!", "*", "..", ",",
    "a", "Goal", "R", "1", "{code:'1'}", "'", " ", "  ", "\n", "\t",
]  # fmt: skip


def random_queries(count=4000, seed=1234):
    rng = random.Random(seed)
    for _ in range(count):
        yield "".join(rng.choice(TOKENS) for _ in range(rng.randint(1, 14)))


def plain(source):
    """The pattern as it was before whitespace was made possessive."""
    return re.compile(source, re.VERBOSE)


@pytest.mark.parametrize("name", PATTERNS)
def test_possessive_whitespace_matches_exactly_what_the_original_pattern_matched(name):
    possessive, source = PATTERNS[name]
    original = plain(source)
    for query in [*HAND_WRITTEN, *random_queries()]:
        assert possessive.findall(query, overlapped=True) == original.findall(
            query, overlapped=True
        ), f"{name} differs on {query!r}"


class TestStripLiteralsAndComments:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("RETURN 'a / 0'", "RETURN ''"),
            ('RETURN "x (n:Y)"', "RETURN ''"),
            ("RETURN 'it\\'s / 0' AS s, 1 / 2", "RETURN '' AS s, 1 / 2"),
            ("MATCH (n) // note: (a:B)\nRETURN n", "MATCH (n)  \nRETURN n"),
            ("MATCH (n) /* (a:B) \n 1/0 */ RETURN n", "MATCH (n)   RETURN n"),
            ("RETURN 'http://x' // c", "RETURN ''  "),
            ("MATCH (n:`odd 'label'`) RETURN n", "MATCH (n:`odd 'label'`) RETURN n"),
            ("RETURN 'unterminated / 0", "RETURN 'unterminated / 0"),
            ("MATCH (n) RETURN n", "MATCH (n) RETURN n"),
        ],
    )
    def test_strip(self, query, expected):
        assert strip_literals_and_comments(query) == expected


class TestMaskProperties:
    def test_round_trip(self):
        text = "(a {x:'1-2'})-[r {w:1}]->(b)"
        masked, props = mask_properties(text)
        assert props == ["{x:'1-2'}", "{w:1}"]
        assert "{" not in masked and "-2" not in masked
        assert unmask_properties(masked, props) == text

    def test_no_properties(self):
        assert mask_properties("(a)-[r]->(b)") == ("(a)-[r]->(b)", [])


class TestVariableLength:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("[:R*]", "[:R]"),
            ("[:R*3]", "[:R]"),
            ("[:R*2..5]", "[:R]"),
            ("[:R*3..]", "[:R]"),  # no upper bound
            ("[:R*..5]", "[:R]"),  # no lower bound
            ("[:R*..]", "[:R]"),
            ("[:R * 2 .. 5]", "[:R ]"),  # whitespace before the '*' is left alone
            ("[:R]", "[:R]"),
        ],
    )
    def test_removed(self, text, expected):
        assert patterns.VARIABLE_LENGTH.sub("", text) == expected

    @pytest.mark.parametrize(
        "text", ["[:R*]", "[r:R*3]", "[:R*2..5]", "[:R*3..]", "[:R*..5]", "[:R|S*3..]"]
    )
    def test_every_form_of_length_is_read_in_a_relationship(self, text):
        assert patterns.REL.fullmatch(text)


# ----------------------------------------------------------------------------------------------------
# Characterisation tests for the regex-based Cypher helpers.
#
# They pin the current behaviour (no database needed unless noted) so the helpers can be
# refactored safely. Private helpers are reached through their name-mangled attributes.


def private(cls, name):
    """A double-underscore helper of ``cls`` (name mangling makes it ``_Cls__name``)."""
    return getattr(cls, f"_{cls.__name__}__{name}")


remove_prefix = schema_validator._without_prefix_before_colon


replace_placeholders = schema_validator._with_placeholders


blank_variables = schema_validator._without_variables


extend_dict = properties_validator._extend


extract_1hop = properties_validator._one_hop_paths


prepare_for_infer = properties_validator._paths_to_infer


inline_mapping = properties_validator._inline_mapping


outside_mapping = properties_validator._outside_mapping


diagnose_missing_path = private(SchemaValidator, "diagnose_missing_path")


class TestIdentifyVariables:
    def test_builds_a_label_and_type_inference_query(self):
        query, nodes, rels = identify_variables_and_construct_query("(a)-[r]->(b)")
        assert (nodes, rels) == (["a", "b"], ["r"])
        assert query == (
            "MATCH (a)-[r]->(b)\n"
            "WITH DISTINCT labels(a) AS a_labels, labels(b) AS b_labels, type(r) AS r_type\n"
            "UNWIND a_labels AS a\n"
            "UNWIND b_labels AS b\n"
            "UNWIND [r_type] AS r\n"
            "RETURN DISTINCT a, b, r"
        )

    def test_only_bare_variables_are_inferred(self):
        query, nodes, rels = identify_variables_and_construct_query(
            "(:Goal)-[:HAS_TARGET]->(t)"
        )
        assert (nodes, rels) == (["t"], [])
        assert query.startswith("MATCH (:Goal)-[:HAS_TARGET]->(t)\nWITH DISTINCT")
        assert query.endswith("RETURN DISTINCT t")

    def test_fully_labeled_path_has_nothing_to_infer(self):
        assert identify_variables_and_construct_query(
            "(:Goal)-[:HAS_TARGET]->(:Target)"
        ) == ("", [], [])


class TestReplaceWithLabels:
    @pytest.mark.parametrize(
        ("path", "replacements", "expected"),
        [
            (
                "(a)-[r]->(b)",
                {"a": "Goal", "r": "HAS", "b": ""},
                "(a:Goal)-[r:HAS]->(b)",
            ),
            ("(a)-[r]->(b)", {"a": "Goal"}, "(a:Goal)-[r]->(b)"),
            ("(a:X)-[r]->(b)", {"b": "T"}, "(a:X)-[r]->(b:T)"),
            ("(a)-[r]->(b)", {}, "(a)-[r]->(b)"),
            ("( a )-[ r ]->(b)", {"a": "G", "r": "R"}, "(a:G)-[r:R]->(b)"),
        ],
    )
    def test_replacements(self, path, replacements, expected):
        assert replace_with_labels(path, replacements) == expected


class TestSmallStringHelpers:
    def test_remove_prefix_before_colon(self):
        assert remove_prefix("(a:Goal)-[r:HAS]->(:T)") == "(:Goal)-[:HAS]->(:T)"
        assert remove_prefix("(a)") == "(a)"

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("(a)-()-(b)", "(a)-(var_1)-(b)"),
            ("(a)-[]->(b)", "(a)-[var_1]->(b)"),
            ("(a)<-[]-(b)--(c)", "(a)<-[var_1]-(b)--(c)"),
        ],
    )
    def test_replace_bracket_placeholders(self, query, expected):
        assert replace_placeholders(query) == expected

    def test_blank_variables(self):
        assert blank_variables("(a)-[r]->(:T)") == "()-[]->(:T)"
        assert blank_variables("(:G)-[:R*]->(b)") == "(:G)-[:R*]->()"


class TestExtendDict:
    def test_adds_a_new_key(self):
        assert extend_dict({}, "k", ["a"]) == {"k": ["a"]}

    def test_extends_without_duplicates(self):
        assert extend_dict({"k": ["a", "b"]}, "k", ["b", "c"]) == {"k": ["a", "b", "c"]}

    def test_does_not_keep_a_reference_to_the_callers_list(self):
        mine = ["a"]
        mapping = extend_dict({}, "k", mine)
        extend_dict(mapping, "k", ["b"])
        assert mine == ["a"]


class TestPropertiesValidatorQueryHelpers:
    def test_extract_1hop_paths_splits_multi_hop_paths(self):
        assert extract_1hop("MATCH (a:G)-[:R]->(b)-[:S]->(c) RETURN a") == [
            "(a:G)-[:R]->(b)",
            "(b)-[:S]->(c)",
        ]

    def test_extract_1hop_paths_keeps_single_nodes(self):
        assert extract_1hop("MATCH (a:G) RETURN a") == ["(a:G)"]

    def test_prepare_query_for_infer_keeps_only_paths_with_unknown_variables(self):
        paths = prepare_for_infer(
            {"a": ("node", "G")}, "MATCH (a)-[r]->(b)-->(c:T) RETURN a"
        )
        assert paths == ["(a:G)-[r]->(b)", "(b)-[]->(c:T)"]

    def test_inline_mapping(self):
        var_label, label_props = inline_mapping(
            "MATCH (g:Goal {code:'1', n:2})-[r:HAS {w:1}]->(t:T|U) RETURN g"
        )
        assert var_label == {
            "g": ("node", "Goal"),
            "t": ("node", "T|U"),
            "r": ("relationship", "HAS"),
        }
        assert {label: sorted(props) for label, props in label_props.items()} == {
            ("node", "Goal"): ["code", "n"],
            ("relationship", "HAS"): ["w"],
        }

    def test_inline_mapping_ignores_colons_inside_values(self):
        _, label_props = inline_mapping("MATCH (g:Goal {at:'12:30', code:1}) RETURN g")
        assert sorted(label_props[("node", "Goal")]) == ["at", "code"]

    def test_outside_mapping_splits_known_and_unknown_variables(self):
        label_props, var_props = outside_mapping(
            {"g": ("node", "Goal")}, {}, "RETURN g.code, x.name, 'y.z'"
        )
        assert label_props == {("node", "Goal"): ["code"]}
        assert var_props == {"x": ["name"]}


class TestGetPathsAfterInference:
    """Needs a database: the inference query runs against the graph."""

    def test_infers_labels_for_a_single_path(self, falkordb_client, db_name):
        (df,) = get_paths_after_inference(
            falkordb_client, ["(g)-[:HAS_TARGET]->(t)"], db_name
        )
        assert df.to_dict("records") == [{"g": "Goal", "t": "Target"}]

    def test_paths_sharing_a_variable_are_joined(self, falkordb_client, db_name):
        paths = ["(g)-[:HAS_TARGET]->(t)", "(t)-[:HAS_INDICATOR]->(i)"]
        (df,) = get_paths_after_inference(falkordb_client, paths, db_name)
        assert df.to_dict("records") == [{"g": "Goal", "t": "Target", "i": "Indicator"}]

    def test_independent_paths_stay_separate(self, falkordb_client, db_name):
        paths = ["(g)-[:HAS_TARGET]->(t)", "(a)-[:HAS_NUTS1]->(n)"]
        frames = get_paths_after_inference(falkordb_client, paths, db_name)
        assert [sorted(df.columns) for df in frames] == [["g", "t"], ["a", "n"]]

    def test_a_query_error_gives_an_empty_frame_with_the_variables(
        self, falkordb_client
    ):
        (df,) = get_paths_after_inference(
            falkordb_client, ["(g)-[:HAS_TARGET]->(t)"], "no_such_graph"
        )
        assert df.empty
        assert list(df.columns) == ["g", "t"]

    def test_return_type(self, falkordb_client, db_name):
        frames = get_paths_after_inference(
            falkordb_client, ["(g)-[:HAS_TARGET]->(t)"], db_name
        )
        assert all(isinstance(df, pd.DataFrame) for df in frames)


class TestDiagnoseMissingPath:
    def test_a_path_that_exists_backwards_with_any_length_is_reported_as_both(
        self, falkordb_client
    ):
        """FalkorDB matches an undirected variable-length path of any length once its ends are connected,
        so on a real graph this diagnosis is never reached: the direction is blamed first. The existence
        checks are answered here instead, so that the code which asks them is still exercised."""

        class OnlyTheBackwardsAnyLengthPathExists:
            def run(self, request):
                rows = [[1]] if "<-[:R*]-" in request.query else []
                return SimpleNamespace(header=[(1, "1")], result_set=rows)

        metadata = []
        run_sync(
            diagnose_missing_path(
                SchemaValidator(falkordb_client),
                "(:C)-[:R*2]->(:A)",  # the extracted path
                ["(c:C)-[:R*2]->(a:A)"],  # as written in the query
                "chain",
                metadata,
            ),
            OnlyTheBackwardsAnyLengthPathExists(),
            _online.resolve,
        )

        (warning,) = metadata
        assert (
            warning["code"]
            == "cypher_guardrail.Warning.WrongDirectionVariableLengthPath"
        )
        assert "should be changed to (c:C)<-[:R*]-(a:A)" in warning["description"]
        assert warning["path"] == "(:C)-[:R*2]->(:A)"
        assert warning["original_path"] == "(c:C)-[:R*2]->(a:A)"
        assert warning["suggested_path"] == "(c:C)<-[:R*]-(a:A)"


# ----------------------------------------------------------------------------------------------------
# FalkorDB's full-text and vector index procedures take a label (or type) and an attribute as strings.


class TestParsing:
    def test_fulltext_nodes(self):
        query = (
            "CALL db.idx.fulltext.queryNodes('Goal', 'poverty') YIELD node RETURN node"
        )
        assert index_procedure_calls(query) == [
            IndexQuery("fulltext", "nodes", "Goal", None, "node")
        ]

    def test_fulltext_relationships(self):
        query = "CALL db.idx.fulltext.queryRelationships('HAS_TARGET', 'x') YIELD relationship RETURN relationship"
        assert index_procedure_calls(query) == [
            IndexQuery("fulltext", "relationships", "HAS_TARGET", None, "relationship")
        ]

    def test_vector_has_an_attribute_and_nested_parentheses_are_fine(self):
        query = (
            "CALL db.idx.vector.queryNodes('Doc', \"embedding\", 3, vecf32([1.0, 2.0])) "
            "YIELD node, score RETURN node.title"
        )
        assert index_procedure_calls(query) == [
            IndexQuery("vector", "nodes", "Doc", "embedding", "node")
        ]

    @pytest.mark.parametrize(
        ("yield_clause", "variable"),
        [
            ("YIELD node", "node"),
            ("YIELD node, score", "node"),
            ("YIELD score, node", "node"),
            ("YIELD node AS d, score", "d"),
            ("YIELD score AS s, node AS d", "d"),
            ("yield node as d", "d"),
        ],
    )
    def test_the_yielded_variable_is_found(self, yield_clause, variable):
        query = f"CALL db.idx.fulltext.queryNodes('Goal', 'x') {yield_clause} RETURN 1"
        assert index_procedure_calls(query)[0].variable == variable

    def test_no_yield_means_no_variable(self):
        assert index_procedure_calls(
            "CALL db.idx.fulltext.queryNodes('Goal', 'x')"
        ) == [IndexQuery("fulltext", "nodes", "Goal", None, None)]

    def test_several_calls_come_in_query_order(self):
        query = (
            "CALL db.idx.fulltext.queryNodes('A', 'x') YIELD node AS a "
            "CALL db.idx.fulltext.queryNodes('B', 'y') YIELD node AS b RETURN a, b"
        )
        assert [call.label for call in index_procedure_calls(query)] == ["A", "B"]

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (g:Goal) RETURN g",
            "CALL db.labels()",
            "CALL db.idx.fulltext.queryNodes($label, 'x') YIELD node RETURN node",
            "CALL db.idx.fulltext.queryNodes('two words', 'x') YIELD node RETURN node",
            "// CALL db.idx.fulltext.queryNodes('Goal', 'x') YIELD node\nRETURN 1",
            "RETURN 'CALL db.idx.fulltext.queryNodes(\\'Goal\\', \\'x\\') YIELD node'",
        ],
    )
    def test_everything_else_is_ignored(self, query):
        assert index_procedure_calls(query) == []

    def test_call_is_case_insensitive(self):
        query = "call db.idx.fulltext.queryNodes('Goal', 'x') yield node return node"
        assert index_procedure_calls(query) == [
            IndexQuery("fulltext", "nodes", "Goal", None, "node")
        ]


# ----------------------------------------------------------------------------------------------------
# Regression tests for bugs found in the code review.


class TestPathologicalInput:
    """Degenerate (e.g. looping LLM) output must not hang the validators."""

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (" + " " * 20000 + "x:Goal RETURN 1",
            "MATCH (g:Goal)" + " " * 20000 + "-[:HAS_TARGET]->(t:Target) RETURN t",
            "MATCH (g:Goal)-[" + " " * 20000 + ":HAS_TARGET RETURN g",
        ],
    )
    def test_whitespace_runs_are_linear(self, falkordb_client, db_name, query):
        start = time.perf_counter()
        SchemaValidator(falkordb_client).extract(query, db_name)
        assert time.perf_counter() - start < 2


# ----------------------------------------------------------------------------------------------------
# Hiding strings and comments from the patterns, and giving them back


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n:Person {name: 'Ann'}) RETURN n",
        "MATCH (n {name: \"O'Brien\", city: 'Paris'}) RETURN n",
        "RETURN 'a', 'b', \"c\", 'it\\'s'",
        "MATCH (n) WHERE n.x = '' RETURN n",
        "MATCH (n) // a comment with 'quotes'\nRETURN n",
        "MATCH (n:`My Label` {x: 'y'}) RETURN n",
        "",
        "RETURN 1",
    ],
)
def test_what_is_hidden_is_given_back_whole(query):
    text, hidden = patterns.hide_literals(query)

    assert patterns.show_literals(text, hidden) == _without_comments(query)


def _without_comments(query):
    # a comment becomes a space and is not given back; everything else is
    return re.sub(r"//[^\n]*|/\*.*?\*/", " ", query, flags=re.DOTALL)


def test_a_hidden_string_is_inert_to_the_patterns():
    text, hidden = patterns.hide_literals(
        "MATCH (n {a: '(x:Fake)-[:R]->(y)', b: \"}{\"}) // (z:Zed)\nRETURN n.c"
    )

    assert hidden == ["'(x:Fake)-[:R]->(y)'", '"}{"']
    assert "Fake" not in text and "Zed" not in text and "}{" not in text
    assert re.findall(r"\w+", text) == ["MATCH", "n", "a", "b", "RETURN", "n", "c"]
    assert (
        text.count("{") == 1 and text.count("}") == 1
    )  # the braces of the map, not those of the string


def test_quoted_identifiers_stay_where_they_are():
    text, hidden = patterns.hide_literals("MATCH (n:`Odd 'Label'`) RETURN n.`x y`")

    assert text == "MATCH (n:`Odd 'Label'`) RETURN n.`x y`"
    assert hidden == []


def test_the_strings_are_given_back_in_texts_lists_tuples_and_dicts():
    text, hidden = patterns.hide_literals("RETURN 'one', \"two\"")
    one, two = text.split(", ")[0][-3:], text.split(", ")[1]

    assert patterns.show_literals(one, hidden) == "'one'"
    assert patterns.show_literals(two, hidden) == '"two"'
    nested = {text: [text, (text,)], "k": 3}
    assert patterns.show_literals(nested, hidden) == {
        "RETURN 'one', \"two\"": ["RETURN 'one', \"two\"", ("RETURN 'one', \"two\"",)],
        "k": 3,
    }


def test_nothing_hidden_means_nothing_to_give_back():
    text, hidden = patterns.hide_literals("MATCH (n) RETURN n")

    assert (text, hidden) == ("MATCH (n) RETURN n", [])
    value = ["x", {"y": ("z",)}]
    assert patterns.show_literals(value, hidden) is value


def test_a_placeholder_that_was_never_hidden_is_left_alone():
    _, hidden = patterns.hide_literals("RETURN 'only'")
    stray = "'" + chr(0xF0000 + 5) + "'"  # the sixth string, of which there is no sixth

    assert patterns.show_literals(stray, hidden) == stray


def test_more_strings_than_placeholders_are_hidden_as_empty_ones(monkeypatch):
    monkeypatch.setattr(patterns, "_HIDDEN_LIMIT", 2)

    text, hidden = patterns.hide_literals("RETURN 'a', 'b', 'c', \"d\"")

    assert hidden == ["'a'", "'b'"]
    assert text.endswith(
        "'', \"\""
    )  # the third and the fourth, as strip_literals_and_comments would
    assert patterns.show_literals(text, hidden).startswith("RETURN 'a', 'b', ")


def test_hiding_is_how_the_validators_take_a_string_in_a_path_and_what_they_report_agrees():
    query = "MATCH (a:A {k: '-[:X]->'})-[:R]->(b:B) RETURN a"
    text, hidden = patterns.hide_literals(query)

    assert "-[:X]->" not in text
    assert patterns.show_literals(text, hidden) == query


# ----------------------------------------------------------------------------------------------------
# Cutting a query at its top-level UNION


@pytest.mark.parametrize(
    ("query", "branches"),
    [
        ("MATCH (n) RETURN n", ["MATCH (n) RETURN n"]),
        ("", [""]),
        (
            "MATCH (n:A) RETURN n UNION MATCH (n:B) RETURN n",
            ["MATCH (n:A) RETURN n ", " MATCH (n:B) RETURN n"],
        ),
        (
            "MATCH (n:A) RETURN n UNION ALL MATCH (n:B) RETURN n",
            ["MATCH (n:A) RETURN n ", " MATCH (n:B) RETURN n"],
        ),
        (
            "RETURN 1 AS x\nunion\nRETURN 2 AS x\nUnion All\nRETURN 3 AS x",
            ["RETURN 1 AS x\n", "\nRETURN 2 AS x\n", "\nRETURN 3 AS x"],
        ),
        # not a union: in a string, a comment, a quoted name, a property, a label, a longer word
        ("RETURN 'a UNION b'", ["RETURN 'a UNION b'"]),
        ('RETURN "UNION"', ['RETURN "UNION"']),
        ("RETURN 1 // UNION\n", ["RETURN 1 // UNION\n"]),
        ("RETURN 1 /* UNION */", ["RETURN 1 /* UNION */"]),
        ("MATCH (n:`UNION`) RETURN n", ["MATCH (n:`UNION`) RETURN n"]),
        ("MATCH (n) RETURN n.UNION", ["MATCH (n) RETURN n.UNION"]),
        ("MATCH (n:UNION) RETURN n", ["MATCH (n:UNION) RETURN n"]),
        ("RETURN UNIONS", ["RETURN UNIONS"]),
        ("MATCH (n {union: 1}) RETURN n", ["MATCH (n {union: 1}) RETURN n"]),
        # not at the top: inside a subquery, a list, a map, a parenthesis
        (
            "CALL { MATCH (a) RETURN a UNION MATCH (b) RETURN a } RETURN a",
            ["CALL { MATCH (a) RETURN a UNION MATCH (b) RETURN a } RETURN a"],
        ),
        ("RETURN [x IN [1] | x UNION 2]", ["RETURN [x IN [1] | x UNION 2]"]),
        # and after a subquery, at the top again
        (
            "CALL { RETURN 1 AS a UNION RETURN 2 AS a } RETURN a UNION RETURN 3 AS a",
            ["CALL { RETURN 1 AS a UNION RETURN 2 AS a } RETURN a ", " RETURN 3 AS a"],
        ),
    ],
)
def test_a_query_is_cut_only_at_a_union_of_its_own(query, branches):
    assert patterns.split_union(query) == branches


def test_the_branches_put_back_together_are_the_query_without_its_unions():
    query = "MATCH (a) RETURN a UNION ALL MATCH (b) RETURN b UNION MATCH (c) RETURN c"

    branches = patterns.split_union(query)

    assert len(branches) == 3
    assert "UNION" not in "".join(branches)
    assert [b.strip() for b in branches] == [
        "MATCH (a) RETURN a",
        "MATCH (b) RETURN b",
        "MATCH (c) RETURN c",
    ]


def test_a_query_that_ends_or_starts_with_a_union_keeps_what_there_is():
    assert patterns.split_union("RETURN 1 UNION") == ["RETURN 1 "]
    assert patterns.split_union("UNION RETURN 1") == [" RETURN 1"]
    assert patterns.split_union("UNION") == [
        "UNION"
    ]  # nothing else: the query as it was


# ----------------------------------------------------------------------------------------------------
# Property keys between back-quotes


def test_a_quoted_property_key_is_given_a_plain_name():
    text, written = patterns.hide_quoted_keys(
        "MATCH (n:`My Label` {`odd key`: 1, ok: 2, `b c`: 3}) WHERE n.`odd key` = 1 RETURN n.`b c`"
    )

    assert text == (
        "MATCH (n:`My Label` {cypherguardrailkey0: 1, ok: 2, cypherguardrailkey1: 3}) "
        "WHERE n.cypherguardrailkey0 = 1 RETURN n.cypherguardrailkey1"
    )  # the label is left, and a key that comes again has the name it had
    assert written == {
        "cypherguardrailkey0": "`odd key`",
        "cypherguardrailkey1": "`b c`",
    }


@pytest.mark.parametrize(
    "text",
    [
        "MATCH (n:`My Label`) RETURN n",
        "MATCH ()-[:`Odd Type`]->() RETURN 1",
        "MATCH (n) RETURN n AS `odd name`",
        "RETURN f(a, `x`)",
        "MATCH (n) WHERE n.name = 'a.`b`' RETURN n",
        "MATCH (n) // n.`b`\nRETURN n",
        "MATCH (n) /* {`b`: 1} */ RETURN n",
    ],
)
def test_what_is_not_a_property_key_is_left_as_it_is(text):
    assert patterns.hide_quoted_keys(text) == (text, {})


def test_a_plain_name_for_a_key_is_not_one_that_the_query_has():
    text, written = patterns.hide_quoted_keys("RETURN n.cypherguardrailkey0, n.`a b`")

    assert text == "RETURN n.cypherguardrailkey0, n.cypherguardrailkey1"
    assert written == {"cypherguardrailkey1": "`a b`"}


def test_unquote_name():
    assert patterns.unquote_name("`odd key`") == "odd key"
    assert patterns.unquote_name("plain") == "plain"
    assert patterns.unquote_name("`") == "`"
    assert patterns.unquote_name("``") == ""
