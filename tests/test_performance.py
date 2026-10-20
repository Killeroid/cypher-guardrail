"""Queries that are valid and may be expensive: ``SyntaxValidator(check_performance=...)``.

The corpus of ``tests/accuracy/performance.json`` is the measure of what is reported on a real database; these tests
are about the rules (on plans written out, so a change of FalkorDB's wording is seen), the option, and the requests.
"""

import typing

import pytest

from cypher_guardrail import AsyncSyntaxValidator, SyntaxValidator
from cypher_guardrail import issues as issue_types
from cypher_guardrail._internal.core import performance, plans
from cypher_guardrail.issues import (
    CARTESIAN_PRODUCT,
    UNBOUNDED_PATH,
    UNLABELED_NODE_SCAN,
)

ALL = performance.selected(True)


def plan(*lines):
    return list(lines)


def codes(found):
    return [issue["code"] for issue in found]


# ----------------------------------------------------------------------------------------------------
# Reading a plan


def test_a_plan_is_a_tree_of_operations():
    root = plans.parse(
        plan(
            "Results",
            "    Project",
            "        Cartesian Product",
            "            Node By Label Scan | (a:Person)",
            "            Conditional Traverse | (b)->(c:Movie)",
            "                Node By Label Scan | (b:Person)",
        )
    )

    assert root is not None
    assert [op.name for op in root.walk()] == [
        "Results",
        "Project",
        "Cartesian Product",
        "Node By Label Scan",
        "Conditional Traverse",
        "Node By Label Scan",
    ]
    product = next(op for op in root.walk() if op.name == "Cartesian Product")
    assert [op.detail for op in product.children] == ["(a:Person)", "(b)->(c:Movie)"]
    assert [leaf.detail for leaf in product.leaves()] == ["(a:Person)", "(b:Person)"]
    assert [a.name for a in product.leaves()[0].ancestors()] == [
        "Cartesian Product",
        "Project",
        "Results",
    ]


def test_the_variables_of_an_operation_are_named_ones_only():
    root = plans.parse(
        [
            "Results",
            "    Conditional Variable Length Traverse | (a)-[@anon_0:KNOWS*1..INF]->(b:P)",
        ]
    )

    assert root is not None
    assert root.children[0].variables == ["a", "b"]
    scan = plans.parse(["Results", "    Scan | (n)-[r*1..INF]->(m)"])
    assert scan is not None
    assert scan.children[0].variables == ["n", "r", "m"]


@pytest.mark.parametrize("reply", [None, [], "OK", 5, [""], [3], [b""]])
def test_what_is_not_a_plan_is_none_and_reports_nothing(reply):
    assert plans.parse(reply) is None
    assert performance.warnings("MATCH (n) RETURN n", reply, ALL) == []


def test_a_plan_in_bytes_is_read_too():
    root = plans.parse([b"Results", b"    Project"])

    assert root is not None
    assert [op.name for op in root.walk()] == ["Results", "Project"]


# ----------------------------------------------------------------------------------------------------
# The rules


PRODUCT = plan(
    "Results",
    "    Project",
    "        Cartesian Product",
    "            Node By Label Scan | (a:Person)",
    "            Node By Label Scan | (b:Movie)",
)


def test_a_cartesian_product_is_reported_with_the_variables_of_each_side():
    found = performance.warnings(
        "MATCH (b:Movie), (a:Person) RETURN a, b", PRODUCT, ALL
    )

    assert [(i["code"], i.get("variables")) for i in found] == [
        (CARTESIAN_PRODUCT, [["b"], ["a"]])  # as the query writes them
    ]


def test_a_product_of_index_lookups_is_left_alone():
    lookups = plan(
        "Results",
        "    Project",
        "        Cartesian Product",
        "            Node By Index Scan | (a:Person)",
        "            Filter",
        "                Node By Index Scan | (b:Person)",
    )

    assert (
        performance.warnings(
            "MATCH (a:Person {id: 1}), (b:Person {id: 2}) RETURN a", lookups, ALL
        )
        == []
    )


def test_a_product_with_one_scan_that_is_not_an_index_lookup_is_reported():
    mixed = plan(
        "Results",
        "    Cartesian Product",
        "        Node By Index Scan | (a:Person)",
        "        Node By Label Scan | (b:Movie)",
    )

    assert codes(
        performance.warnings("MATCH (a:Person {id: 1}), (b:Movie) RETURN a", mixed, ALL)
    ) == [CARTESIAN_PRODUCT]


def test_every_product_of_a_query_is_reported_once():
    two = plan(
        "Results",
        "    Join",
        "        Cartesian Product",
        "            Node By Label Scan | (a:A)",
        "            Node By Label Scan | (b:B)",
        "        Cartesian Product",
        "            Node By Label Scan | (c:C)",
        "            Node By Label Scan | (d:D)",
    )

    found = performance.warnings(
        "MATCH (a:A), (b:B) RETURN a UNION MATCH (c:C), (d:D) RETURN c", two, ALL
    )

    assert [i.get("variables") for i in found] == [[["a"], ["b"]], [["c"], ["d"]]]


FILTERED = plan(
    "Results", "    Project", "        Filter", "            All Node Scan | (n)"
)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) WHERE n.name = 'x' RETURN n",
        "MATCH (n {name: 'x'}) RETURN n",
        "MATCH (n)  WHERE n . name = 'x' RETURN n",
    ],
)
def test_a_node_with_no_label_that_is_filtered_on_a_property_is_reported(query):
    found = performance.warnings(query, FILTERED, ALL)

    assert [(i["code"], i.get("variable")) for i in found] == [
        (UNLABELED_NODE_SCAN, "n")
    ]


def test_it_needs_a_filter_and_a_property_of_that_node():
    unfiltered = plan("Results", "    Project", "        All Node Scan | (n)")

    assert performance.warnings("MATCH (n) RETURN n", unfiltered, ALL) == []
    # a filter, but on another variable: the query does not read a property of n
    assert performance.warnings("MATCH (n) WHERE m.x = 1 RETURN n", FILTERED, ALL) == []
    # a property in a string is not read
    assert (
        performance.warnings("MATCH (n) WHERE m.x = 'n.x' RETURN n", FILTERED, ALL)
        == []
    )


UNBOUNDED = plan(
    "Results",
    "    Project",
    "        Conditional Variable Length Traverse | (a)-[@anon_0:KNOWS*1..INF]->(b)",
    "            Node By Label Scan | (a:Person)",
)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (a:Person)-[:KNOWS*]->(b) RETURN b",
        "MATCH (a:Person)-[:KNOWS*1..]->(b) RETURN b",
        "MATCH (a:Person)-[:KNOWS * ..]->(b) RETURN b",
    ],
)
def test_a_path_with_no_upper_limit_is_reported_as_written(query):
    found = performance.warnings(query, UNBOUNDED, ALL)

    assert [i.get("path") for i in found] == [
        query.split("MATCH ")[1].split(" RETURN")[0]
    ]


def test_the_path_of_the_plan_is_given_when_the_text_has_none_that_is_read():
    found = performance.warnings("MATCH p=(a)-[*]->(b) RETURN p", UNBOUNDED, ALL)

    assert [i["code"] for i in found] == [UNBOUNDED_PATH]


@pytest.mark.parametrize("hops", ["*1..3", "*2", "*..4", "*0..10", "* 2 .. 3"])
def test_a_path_with_an_upper_limit_is_not(hops):
    bounded = plan(
        "Results",
        "    Conditional Variable Length Traverse | (a)-[@anon_0:R*1..3]->(b)",
    )

    assert (
        performance.warnings(f"MATCH (a)-[:R{hops}]->(b) RETURN b", bounded, ALL) == []
    )


def test_each_check_can_be_left_out():
    everything = plan(
        "Results",
        "    Cartesian Product",
        "        Filter",
        "            All Node Scan | (n)",
        "        Conditional Variable Length Traverse | (a)-[@anon_0:R*1..INF]->(b)",
        "            Node By Label Scan | (a:A)",
    )
    query = "MATCH (n) WHERE n.x = 1 MATCH (a:A)-[:R*]->(b) RETURN n"

    assert codes(performance.warnings(query, everything, ALL)) == [
        CARTESIAN_PRODUCT,
        UNLABELED_NODE_SCAN,
        UNBOUNDED_PATH,
    ]
    for left_out, remaining in [
        ("cartesian_product", {UNLABELED_NODE_SCAN, UNBOUNDED_PATH}),
        ("unlabeled_scan", {CARTESIAN_PRODUCT, UNBOUNDED_PATH}),
        ("unbounded_path", {CARTESIAN_PRODUCT, UNLABELED_NODE_SCAN}),
    ]:
        checks = frozenset(performance.CHECKS) - {left_out}
        assert set(codes(performance.warnings(query, everything, checks))) == remaining


# ----------------------------------------------------------------------------------------------------
# The option


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        (False, set()),
        (None, set()),
        ([], set()),
        (True, set(performance.CHECKS)),
        (["unbounded_path"], {"unbounded_path"}),
        (
            {"cartesian_product", "unlabeled_scan"},
            {"cartesian_product", "unlabeled_scan"},
        ),
        (("unbounded_path",), {"unbounded_path"}),
    ],
)
def test_check_performance_is_true_false_or_the_names_of_the_checks(option, expected):
    assert performance.selected(option) == expected


def test_a_name_that_is_not_a_check_is_refused():
    with pytest.raises(ValueError, match="nope"):
        performance.selected(["nope"])


@pytest.mark.parametrize("option", ["unbounded_path", 3, 1.5])
def test_a_string_or_a_number_is_refused(option):
    with pytest.raises(TypeError, match="check_performance"):
        performance.selected(option)


def test_the_validators_refuse_a_wrong_option(falkordb_client, run_async):
    with pytest.raises(ValueError, match="nope"):
        SyntaxValidator(falkordb_client, check_performance=["nope"])
    with pytest.raises(TypeError, match="check_performance"):
        SyntaxValidator(falkordb_client, check_performance="unbounded_path")

    async def ask(client):
        with pytest.raises(ValueError, match="nope"):
            AsyncSyntaxValidator(client, check_performance=["nope"])

    run_async(ask)


# ----------------------------------------------------------------------------------------------------
# On a database


QUERY = "MATCH (a:Goal), (b:Target) RETURN a, b"


def test_the_warnings_leave_the_query_valid(falkordb_client):
    valid, metadata = SyntaxValidator(falkordb_client, check_performance=True).validate(
        QUERY, "kg"
    )

    assert valid is True
    assert codes(metadata) == [CARTESIAN_PRODUCT]


def test_nothing_is_asked_that_is_not_wanted(falkordb_client, requests):
    SyntaxValidator(falkordb_client).validate(QUERY, "kg")
    plain = list(requests)
    requests.clear()

    SyntaxValidator(falkordb_client, check_performance=True).validate(QUERY, "kg")

    assert [type(r).__name__ for r in requests[: len(plain)]] == [
        type(r).__name__ for r in plain
    ]
    assert len(requests) == len(plain) + 1  # the plan


def test_with_a_parameter_that_needs_a_value_the_plan_is_still_had(falkordb_client):
    graph = falkordb_client.select_graph("perf_params")
    graph.query("CREATE (:P {id: 1}), (:Q {id: 2})")
    graph.query("CREATE INDEX FOR (p:P) ON (p.id)")
    try:
        query = "MATCH (a:P), (b:Q) WHERE a.id = $id RETURN a, b"

        valid, metadata = SyntaxValidator(
            falkordb_client, check_performance=True
        ).validate(query, "perf_params", parameters=["id"])

        assert (valid, codes(metadata)) == (True, [CARTESIAN_PRODUCT])
    finally:
        graph.delete()


def test_the_async_validator_says_the_same(falkordb_client, run_async):
    async def ask(client):
        return await AsyncSyntaxValidator(client, check_performance=True).validate(
            QUERY, "kg"
        )

    assert run_async(ask) == SyntaxValidator(
        falkordb_client, check_performance=True
    ).validate(QUERY, "kg")


def test_in_the_hybrid_mode_too(falkordb_client):
    from cypher_guardrail import Snapshot

    snapshot = Snapshot.capture(falkordb_client, "kg")
    validator = SyntaxValidator(
        falkordb_client, snapshot=snapshot, check_performance=True
    )

    assert codes(validator.validate(QUERY, "kg")[1]) == [CARTESIAN_PRODUCT]


def test_the_operations_of_the_plan_are_still_called_what_the_rules_read(
    falkordb_client,
):
    # the rules read FalkorDB's own words: if they change, this says which
    graph = falkordb_client.select_graph("perf_words")
    graph.query("CREATE (:A {x: 1})-[:R]->(:B {x: 2})")
    try:

        def lines(query):
            return falkordb_client.execute_command("GRAPH.EXPLAIN", "perf_words", query)

        assert any(
            "Cartesian Product" in line
            for line in lines("MATCH (a:A), (b:B) RETURN a, b")
        )
        assert any(
            "All Node Scan | (n)" in line
            for line in lines("MATCH (n) WHERE n.x = 1 RETURN n")
        )
        assert any(
            "Filter" in line for line in lines("MATCH (n) WHERE n.x = 1 RETURN n")
        )
        assert any(
            "Variable Length Traverse" in line and "INF" in line
            for line in lines("MATCH (a:A)-[:R*]->(b) RETURN b")
        )
        assert any("Node By Index Scan" in line for line in indexed(graph, lines))
    finally:
        graph.delete()


def indexed(graph, lines):
    graph.query("CREATE INDEX FOR (a:A) ON (a.x)")
    return lines("MATCH (a:A {x: 1}), (b:A {x: 2}) RETURN a, b")


# ----------------------------------------------------------------------------------------------------
# The types of the issues


@pytest.mark.parametrize(
    ("code", "issue"),
    [
        (CARTESIAN_PRODUCT, issue_types.CartesianProductIssue),
        (UNLABELED_NODE_SCAN, issue_types.UnlabeledNodeScanIssue),
        (UNBOUNDED_PATH, issue_types.UnboundedPathIssue),
    ],
)
def test_the_issues_have_the_fields_of_their_types(code, issue):
    everything = plan(
        "Results",
        "    Cartesian Product",
        "        Filter",
        "            All Node Scan | (n)",
        "        Conditional Variable Length Traverse | (a)-[@anon_0:R*1..INF]->(b)",
        "            Node By Label Scan | (a:A)",
    )

    found = performance.warnings(
        "MATCH (n) WHERE n.x = 1 MATCH (a:A)-[:R*]->(b) RETURN n", everything, ALL
    )

    (one,) = (i for i in found if i["code"] == code)
    assert set(one) == set(typing.get_type_hints(issue))
    assert typing.get_args(typing.get_type_hints(issue)["code"]) == (code,)
    assert issue in typing.get_args(issue_types.Issue)
