"""SchemaValidator against a real FalkorDB: labels, relationship types, paths, directions and index procedures."""

import pytest
from support import code_set

from cypher_guardrail import PropertiesValidator, SchemaValidator, Snapshot
from cypher_guardrail.issues import UNKNOWN_INDEX, UNKNOWN_LABEL, UNKNOWN_REL_TYPE

# ----------------------------------------------------------------------------------------------------
# The three validators against a real FalkorDB (FalkorDBLite), with a client and with a bound graph.


class TestSchemaValidator:
    def test_extract(self, driver, db_name):
        nodes, rels, paths = SchemaValidator(driver).extract(
            "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g, t", db_name
        )
        # the lists are sorted, so their order is the same on every run
        assert nodes == ["(:Goal)", "(:Target)"]
        assert rels == ["()-[:HAS_TARGET]-()"]
        assert paths == ["(:Goal)-[:HAS_TARGET]->(:Target)"]

    def test_valid_query_scores_one(self, driver, db_name):
        score, metadata = SchemaValidator(driver).validate(
            "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g, t", db_name
        )
        assert score == 1.0
        assert metadata == []

    def test_wrong_direction(self, driver, db_name):
        score, metadata = SchemaValidator(driver).validate(
            "MATCH (g:Goal)<-[:HAS_TARGET]-(t:Target) RETURN g", db_name
        )
        assert score == pytest.approx(2 / 3)
        assert code_set(metadata) == {"cypher_guardrail.Warning.WrongDirectionPath"}
        assert "(g:Goal)-[:HAS_TARGET]->(t:Target)" in metadata[0]["description"]

    @pytest.mark.parametrize(
        ("query", "code"),
        [
            ("MATCH (g:Ghost)-[:HAS_TARGET]->(t:Target) RETURN g", UNKNOWN_LABEL),
            ("MATCH (g:Goal)-[:NOPE]->(t:Target) RETURN g", UNKNOWN_REL_TYPE),
        ],
    )
    def test_a_name_the_graph_does_not_have_is_reported(
        self, driver, db_name, query, code
    ):
        score, metadata = SchemaValidator(driver).validate(query, db_name)
        assert score < 1
        assert code in code_set(metadata)

    def test_valid_labels_but_no_such_path(self, driver, db_name):
        score, metadata = SchemaValidator(driver).validate(
            "MATCH (g:Goal)-[:HAS_INDICATOR]->(i:Indicator) RETURN g", db_name
        )
        assert score < 1
        assert code_set(metadata) == {"cypher_guardrail.Warning.UnknownPath"}

    def test_variable_length_suggestion(self, driver, db_name):
        score, metadata = SchemaValidator(driver).validate(
            "MATCH (g:Goal)-[:HAS_TARGET*2]->(t) RETURN g", db_name
        )
        assert score < 1
        assert code_set(metadata) == {"cypher_guardrail.Warning.VariableLengthPath"}
        assert "[:HAS_TARGET*]" in metadata[0]["description"]

    def test_unlabeled_variables_are_inferred(self, driver, db_name):
        _, _, paths = SchemaValidator(driver).extract(
            "MATCH (g:Goal)-[r]->(t) RETURN g, r, t", db_name
        )
        assert paths == ["(:Goal)-[:HAS_TARGET]->(:Target)"]
        score, _ = SchemaValidator(driver).validate(
            "MATCH (g:Goal)-[r]->(t) RETURN g, r, t", db_name
        )
        assert score == 1.0


# ----------------------------------------------------------------------------------------------------
# FalkorDB's full-text and vector index procedures take a label (or type) and an attribute as strings.


class TestSchemaValidatorIndexProcedures:
    def test_label_of_an_existing_index_is_valid(self, falkordb_client, documents):
        query = "CALL db.idx.fulltext.queryNodes('Doc', 'alpha') YIELD node RETURN node"
        validator = SchemaValidator(falkordb_client)
        assert validator.extract(query, documents)[0] == ["(:Doc)"]
        assert validator.validate(query, documents) == (1.0, [])

    def test_unknown_label_is_reported(self, falkordb_client, documents):
        # FalkorDB itself answers this with an empty result, without an error
        query = (
            "CALL db.idx.fulltext.queryNodes('Docs', 'alpha') YIELD node RETURN node"
        )
        score, metadata = SchemaValidator(falkordb_client).validate(query, documents)
        assert score == 0.0
        assert {m["code"] for m in metadata} == {UNKNOWN_LABEL, UNKNOWN_INDEX}

    def test_relationship_type_is_checked_as_a_type(self, falkordb_client, documents):
        known = "CALL db.idx.fulltext.queryRelationships('CITES', 'x') YIELD relationship RETURN relationship"
        unknown = known.replace("CITES", "CITED")
        validator = SchemaValidator(falkordb_client)
        assert validator.extract(known, documents)[1] == ["()-[:CITES]-()"]
        assert validator.validate(known, documents) == (1.0, [])
        score, metadata = validator.validate(unknown, documents)
        assert score == 0.0
        assert {m["code"] for m in metadata} == {UNKNOWN_REL_TYPE, UNKNOWN_INDEX}

    def test_it_is_combined_with_the_patterns_of_the_query(
        self, falkordb_client, db_name
    ):
        query = (
            "CALL db.idx.fulltext.queryNodes('Goal', 'poverty') YIELD node AS g "
            "MATCH (g)-[:HAS_TARGET]->(t:Target) RETURN t"
        )
        nodes, relationships, _ = SchemaValidator(falkordb_client).extract(
            query, db_name
        )
        assert nodes == ["(:Goal)", "(:Target)"]
        assert relationships == ["()-[:HAS_TARGET]-()"]


class TestIndexProceduresAreIndexes:
    """The label exists, but does an index back the call? (FalkorDB answers a full-text query
    on an unindexed label with an empty result, and a vector query with an error.)"""

    @pytest.mark.parametrize(
        "call",
        [
            "db.idx.fulltext.queryNodes('Doc', 'x') YIELD node",
            "db.idx.vector.queryNodes('Doc', 'embedding', 1, vecf32([1.0, 2.0])) YIELD node",
            "db.idx.fulltext.queryRelationships('CITES', 'x') YIELD relationship",
            "db.idx.vector.queryRelationships('CITES', 'emb', 1, vecf32([1.0, 2.0])) YIELD relationship",
        ],
    )
    def test_an_existing_index_is_valid(self, falkordb_client, documents, call):
        assert SchemaValidator(falkordb_client).validate(
            f"CALL {call} RETURN 1", documents
        ) == (1.0, [])

    @pytest.mark.parametrize(
        "call",
        [
            # the label exists, but has no full-text index
            "db.idx.fulltext.queryNodes('Goal', 'x') YIELD node",
            # the attribute has a full-text index, not a vector index
            "db.idx.vector.queryNodes('Doc', 'title', 1, vecf32([1.0, 2.0])) YIELD node",
            # the label has a vector index, but on another attribute
            "db.idx.vector.queryNodes('Doc', 'nope', 1, vecf32([1.0, 2.0])) YIELD node",
            # the relationship type has a vector index, not a full-text one on this attribute type
            "db.idx.vector.queryRelationships('CITES', 'note', 1, vecf32([1.0, 2.0])) YIELD relationship",
        ],
    )
    def test_a_missing_index_is_reported(
        self, falkordb_client, documents, db_name, call
    ):
        graph = documents if "Doc" in call or "CITES" in call else db_name
        score, metadata = SchemaValidator(falkordb_client).validate(
            f"CALL {call} RETURN 1", graph
        )
        assert score == 0.5  # the label exists, the index does not
        assert [m["code"] for m in metadata] == [UNKNOWN_INDEX]

    def test_the_description_names_the_index(self, falkordb_client, documents):
        query = "CALL db.idx.vector.queryNodes('Doc', 'nope', 1, vecf32([1.0, 2.0])) YIELD node RETURN node"
        _, metadata = SchemaValidator(falkordb_client).validate(query, documents)
        assert "vector index" in metadata[0]["description"]
        assert "Doc" in metadata[0]["description"]
        assert "nope" in metadata[0]["description"]

    def test_a_vector_call_without_a_literal_attribute_needs_any_vector_index(
        self, falkordb_client, documents
    ):
        query = "CALL db.idx.vector.queryNodes('Doc', $attribute, 1, $vector) YIELD node RETURN node"
        assert SchemaValidator(falkordb_client).validate(query, documents) == (1.0, [])

    def test_each_index_is_counted_once(self, falkordb_client, documents):
        call = "CALL db.idx.fulltext.queryNodes('Doc', '{}') YIELD node AS {} "
        query = call.format("a", "a") + call.format("b", "b") + "RETURN a, b"
        assert SchemaValidator(falkordb_client).validate(query, documents) == (1.0, [])

    def test_the_score_averages_the_patterns_and_the_indexes(
        self, falkordb_client, documents
    ):
        # (:Doc) exists; one index exists, one does not
        query = (
            "CALL db.idx.fulltext.queryNodes('Doc', 'x') YIELD node AS a "
            "CALL db.idx.vector.queryNodes('Doc', 'title', 1, $v) YIELD node AS b RETURN a, b"
        )
        score, metadata = SchemaValidator(falkordb_client).validate(query, documents)
        assert score == 0.75
        assert [m["code"] for m in metadata] == [UNKNOWN_INDEX]

    def test_queries_without_index_procedures_are_scored_as_before(
        self, falkordb_client, db_name
    ):
        query = "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g"
        assert SchemaValidator(falkordb_client).validate(query, db_name) == (1.0, [])

    def test_a_graph_without_indexes_has_no_index(self, falkordb_client):
        graph = falkordb_client.select_graph("without_indexes")
        graph.query("CREATE (:Doc {title: 'a'})")
        query = "CALL db.idx.fulltext.queryNodes('Doc', 'x') YIELD node RETURN node"
        try:
            score, metadata = SchemaValidator(falkordb_client).validate(
                query, "without_indexes"
            )
        finally:
            graph.delete()
        assert score == 0.5  # the label exists, the index does not
        assert UNKNOWN_INDEX in {m["code"] for m in metadata}


# ----------------------------------------------------------------------------------------------------
# Regression tests for bugs found in the code review.


class TestWrongDirectionSuggestion:
    """The suggested fix must be the original path with the arrow flipped, whatever its properties contain."""

    @pytest.mark.parametrize(
        ("original", "suggested"),
        [
            (
                "(g:Goal {code:'12'})<-[:HAS_TARGET]-(t:Target)",
                "(g:Goal {code:'12'})-[:HAS_TARGET]->(t:Target)",
            ),
            (
                "(g:Goal {code:'1-2'})<-[:HAS_TARGET]-(t:Target)",
                "(g:Goal {code:'1-2'})-[:HAS_TARGET]->(t:Target)",
            ),
            (
                "(g:Goal)<-[:HAS_TARGET]-(t:Target {text:'a - b -> c'})",
                "(g:Goal)-[:HAS_TARGET]->(t:Target {text:'a - b -> c'})",
            ),
            (
                "(t:Target)-[:HAS_TARGET]->(g:Goal)",
                "(t:Target)<-[:HAS_TARGET]-(g:Goal)",
            ),
        ],
    )
    def test_suggested_path(self, falkordb_client, db_name, original, suggested):
        _, metadata = SchemaValidator(falkordb_client).validate(
            f"MATCH {original} RETURN g", db_name
        )
        (warning,) = [
            m
            for m in metadata
            if m["code"] == "cypher_guardrail.Warning.WrongDirectionPath"
        ]
        assert (
            f"The path {original} in the Cypher statement should be changed to  {suggested}."
            in warning["description"]
        )


class TestRepeatedVariables:
    """A variable used more than once in a pattern used to crash with 'column label is not unique'."""

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (g)<-[:HAS_TARGET]-(a)-[:HAS_TARGET]-(a) RETURN g",  # node repeated
            "MATCH (a)-[r]->(b)<-[r]-(c) RETURN a",  # relationship repeated
            "MATCH (a)-[:HAS_TARGET]->(b)-[:HAS_INDICATOR]->(c)<-[:HAS_INDICATOR]-(b) RETURN a",
        ],
    )
    def test_schema_validation_does_not_crash(self, falkordb_client, db_name, query):
        score, _ = SchemaValidator(falkordb_client).validate(query, db_name)
        assert 0.0 <= score <= 1.0

    def test_a_repeated_variable_is_inferred_once(self, falkordb_client, db_name):
        # (a) is a Target in both places
        _, _, paths = SchemaValidator(falkordb_client).extract(
            "MATCH (g)-[:HAS_TARGET]->(a)<-[:HAS_TARGET]-(g) RETURN a", db_name
        )
        assert paths == [
            "(:Goal)-[:HAS_TARGET]->(:Target)",
            "(:Target)<-[:HAS_TARGET]-(:Goal)",
        ]

    def test_a_variable_that_is_both_node_and_relationship_does_not_crash(
        self, falkordb_client, db_name
    ):
        # Invalid Cypher (FalkorDB: "alias 'x' was specified for both a node and a relationship")
        score, _ = SchemaValidator(falkordb_client).validate(
            "MATCH (a)<-[x]-(x)-->(b) RETURN a", db_name
        )
        assert 0.0 <= score <= 1.0


# ----------------------------------------------------------------------------------------------------
# Text inside strings and comments is not Cypher (on a snapshot: no database needed)

MOVIES = Snapshot.from_schema(
    nodes={"Person": ["name"], "Movie": ["title"]},
    relationships=[("Person", "ACTED_IN", "Movie")],
    relationship_properties={"ACTED_IN": ["role"]},
    graph="g",
)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n:Person) WHERE n.name = '(x:Fake)' RETURN n",
        'MATCH (n:Person) WHERE n.name = "(x:Fake)-[:NOPE]->(y:Ghost)" RETURN n',
        "MATCH (n:Person) // (x:Fake)\nRETURN n",
        "MATCH (n:Person) /* (x:Fake)-[:NOPE]->(y:Ghost) */ RETURN n",
        "MATCH (n:Person {name: '(x:Fake)'}) RETURN n",
        "MATCH (n:Person {name: 'a}b'})-[:ACTED_IN]->(m:Movie) RETURN n",  # a brace in a string
        "MATCH (n:Person)-[:ACTED_IN]->(m:Movie) WHERE m.title = 'x -[:NOPE]-> (y:Ghost)' RETURN n",
        "MATCH (n:Person) WHERE n.name = 'it\\'s (x:Fake)' RETURN n",
    ],
)
def test_what_is_in_a_string_or_a_comment_is_not_a_label_or_a_path(query):
    score, metadata = SchemaValidator(MOVIES).validate(query)

    assert (score, metadata) == (1.0, [])
    nodes, rels, paths = SchemaValidator(MOVIES).extract(query)
    assert "Fake" not in str(nodes) and "Ghost" not in str(nodes + rels + paths)


def test_a_path_with_a_string_in_its_map_is_reported_as_it_is_written():
    # the path does not exist the way round it is written: its text, with the string, is in the issue
    query = "MATCH (m:Movie {title: 'a (b) {c}'})-[:ACTED_IN]->(p:Person {name: 'x'}) RETURN m"

    score, metadata = SchemaValidator(MOVIES).validate(query)

    assert score < 1
    (issue,) = [i for i in metadata if "original_path" in i]
    assert (
        issue["original_path"]
        == "(m:Movie {title: 'a (b) {c}'})-[:ACTED_IN]->(p:Person {name: 'x'})"
    )
    assert "'a (b) {c}'" in issue["description"]


def test_the_label_of_an_index_procedure_is_still_read_from_its_string():
    query = "CALL db.idx.fulltext.queryNodes('Ghost', 'x') YIELD node RETURN node"

    _, metadata = SchemaValidator(MOVIES).validate(query)

    assert any(
        i.get("label") == "Ghost" for i in metadata
    )  # the string is where the label is


# ----------------------------------------------------------------------------------------------------
# Names between back-quotes (labels and relationship types with a space or a symbol in them)

ODD = Snapshot.from_schema(
    nodes={"Person": ["name"], "My Label": ["x"]},
    relationships=[("Person", "Odd Type", "My Label"), ("Person", "KNOWS", "Person")],
    graph="g",
)


@pytest.mark.parametrize(
    ("query", "code", "name"),
    [
        (
            "MATCH (n:`Fake Label`) RETURN n",
            "cypher_guardrail.Warning.UnknownLabel",
            "Fake Label",
        ),
        (
            "MATCH (n:`Ghost`) RETURN n",
            "cypher_guardrail.Warning.UnknownLabel",
            "Ghost",
        ),
        (
            "MATCH (:Person)-[:`No Such`]->(:Person) RETURN 1",
            "cypher_guardrail.Warning.UnknownRelationshipType",
            "No Such",
        ),
        (
            "MATCH (a:Person)-[r:`Fake Type`]->(b) RETURN a",
            "cypher_guardrail.Warning.UnknownRelationshipType",
            "Fake Type",
        ),
        (
            "MATCH (n:Person:`Zed Label`) RETURN n",
            "cypher_guardrail.Warning.UnknownLabel",
            "Zed Label",
        ),
    ],
)
def test_an_unknown_name_between_back_quotes_is_reported(query, code, name):
    score, metadata = SchemaValidator(ODD).validate(query)

    assert score < 1
    assert [
        (i["code"], i.get("label") or i.get("relationship_type"))
        for i in metadata
        if i["code"] == code
    ] == [(code, name)]


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n:`My Label`) RETURN n",
        "MATCH (n:`Person`) RETURN n",  # back-quotes around a name that needs none
        "MATCH (:Person)-[:`Odd Type`]->(:`My Label`) RETURN 1",
        "MATCH (a:`Person`)-[:`KNOWS`]->(b:Person) RETURN a",
        "MATCH (n:`My Label` {x: 'y'}) RETURN n",
    ],
)
def test_a_name_between_back_quotes_that_the_graph_has_is_fine(query):
    assert SchemaValidator(ODD).validate(query) == (1.0, [])


def test_the_names_between_back_quotes_are_in_what_extract_gives():
    nodes, rels, _ = SchemaValidator(ODD).extract(
        "MATCH (a:`My Label`)<-[:`Odd Type`]-(b:Person) RETURN a"
    )

    assert "(:`My Label`)" in nodes and "(:Person)" in nodes
    assert "()-[:`Odd Type`]-()" in rels


def test_a_path_with_a_name_between_back_quotes_is_not_judged_only_the_names_in_it():
    # the path is not one the validator reads, so it is not called a wrong path; its labels are checked
    query = "MATCH (a:Person)-[:`Odd Type`]->(b:`My Label`) RETURN a"

    assert SchemaValidator(ODD).validate(query) == (1.0, [])
    wrong_way = "MATCH (b:`My Label`)-[:`Odd Type`]->(a:Person) RETURN a"
    assert SchemaValidator(ODD).validate(wrong_way) == (
        1.0,
        [],
    )  # left unchecked: as it was


@pytest.fixture
def odd_names(falkordb_client):
    graph = falkordb_client.select_graph("odd_names")
    graph.query(
        "CREATE (:Person {name: 'a'})-[:`Odd Type`]->(:`My Label` {x: 1}), (:Person {name: 'b'})-[:KNOWS]->(:Person)"
    )
    yield falkordb_client
    graph.delete()


QUOTED_QUERIES = [
    "MATCH (n:`My Label`) RETURN n",
    "MATCH (n:`Fake Label`) RETURN n",
    "MATCH (:Person)-[:`Odd Type`]->(:`My Label`) RETURN 1",
    "MATCH (:Person)-[:`Fake Type`]->(:`My Label`) RETURN 1",
    "MATCH (a:Person)-[r:`Odd Type`]->(b) RETURN a",
    "MATCH (n:Person:`Zed Label`) RETURN n",
    "MATCH (a:Person)-[:KNOWS]->(b:Person) RETURN a",
]


def test_a_name_between_back_quotes_is_judged_the_same_online_offline_and_hybrid(
    odd_names,
):
    snapshot = Snapshot.capture(odd_names, "odd_names")
    assert "My Label" in snapshot.labels and "Odd Type" in snapshot.relationship_types

    results = {}
    for query in QUOTED_QUERIES:
        online = SchemaValidator(odd_names).validate(query, "odd_names")
        offline = SchemaValidator(snapshot).validate(query)
        hybrid = SchemaValidator(odd_names, snapshot=snapshot).validate(
            query, "odd_names"
        )
        assert offline == online == hybrid, query
        results[query] = online

    assert results[QUOTED_QUERIES[0]] == (1.0, [])
    assert results[QUOTED_QUERIES[2]] == (1.0, [])
    assert results[QUOTED_QUERIES[6]] == (1.0, [])
    for unknown in (1, 3, 5):
        assert results[QUOTED_QUERIES[unknown]][0] < 1
        assert results[QUOTED_QUERIES[unknown]][1]


def test_a_name_between_back_quotes_that_needs_none_is_written_without_them():
    nodes, rels, _ = SchemaValidator(ODD).extract(
        "MATCH (n:`Person`)-[:`KNOWS`]->(m) RETURN n"
    )

    assert nodes == ["(:Person)"]
    assert rels == ["()-[:KNOWS]-()"]


def test_an_unknown_type_between_back_quotes_is_reported_next_to_a_path_that_is_read():
    # one path the validator reads (so the paths of the query are not none), and one with a quoted type
    query = (
        "MATCH (a:Person)-[:KNOWS]->(b:Person) "
        "MATCH (c:Person)-[:`Fake Type`]->(d) RETURN a"
    )

    score, metadata = SchemaValidator(ODD).validate(query)

    assert score < 1
    assert [i["relationship_type"] for i in metadata if "relationship_type" in i] == [
        "Fake Type"
    ]


# ----------------------------------------------------------------------------------------------------
# Each branch of a UNION has its own variables, and a variable that is used twice has all its labels


def test_a_label_in_the_first_branch_of_a_union_is_checked_as_much_as_the_last():
    query = "MATCH (n:Foo) RETURN n UNION MATCH (n:Movie) RETURN n"

    score, metadata = SchemaValidator(MOVIES).validate(query)

    assert score == 0.5
    assert [i["label"] for i in metadata] == ["Foo"]


def test_every_branch_of_a_union_all_is_checked():
    query = (
        "MATCH (a:Person) RETURN a UNION ALL MATCH (b:Ghost) RETURN b "
        "UNION ALL MATCH (c:Movie) RETURN c UNION ALL MATCH (d:Nope) RETURN d"
    )

    _, metadata = SchemaValidator(MOVIES).validate(query)

    assert sorted(i["label"] for i in metadata) == ["Ghost", "Nope"]


def test_the_variables_of_the_branches_are_inferred_each_on_its_own():
    # `a` is a Person in the first branch (it acts) and a Movie in the second (it is acted in): one label each
    query = (
        "MATCH (a)-[:ACTED_IN]->(m) RETURN a UNION MATCH (p)-[:ACTED_IN]->(a) RETURN a"
    )

    assert SchemaValidator(MOVIES).validate(query) == (1.0, [])
    _, _, paths = SchemaValidator(MOVIES).extract(query)
    assert paths == ["(:Person)-[:ACTED_IN]->(:Movie)"]


def test_a_variable_that_is_used_twice_with_two_labels_has_both_checked():
    query = "MATCH (n:Foo) WITH n MATCH (n:Movie) RETURN n"

    score, metadata = SchemaValidator(MOVIES).validate(query)

    assert score == 0.5
    assert [i["label"] for i in metadata] == ["Foo"]


def test_a_relationship_variable_used_twice_has_all_its_types_checked():
    query = "MATCH ()-[r:NOPE]->() WITH r MATCH ()-[r:ACTED_IN]->() RETURN r"

    _, metadata = SchemaValidator(MOVIES).validate(query)

    assert [i["relationship_type"] for i in metadata] == ["NOPE"]


def test_a_union_inside_a_string_or_a_comment_does_not_cut_the_query():
    query = "MATCH (n:Person) WHERE n.name = 'a UNION MATCH (x:Foo)' RETURN n // UNION (y:Bar)"

    assert SchemaValidator(MOVIES).validate(query) == (1.0, [])


def test_a_query_without_a_union_is_judged_as_before():
    query = "MATCH (a:Person)-[:ACTED_IN]->(m:Movie) RETURN a"

    assert SchemaValidator(MOVIES).validate(query) == (1.0, [])


def test_the_same_wrong_path_written_in_two_branches_is_reported_for_each_way_it_is_written():
    query = (
        "MATCH (a:Movie)-[:ACTED_IN]->(p:Person) RETURN a UNION "
        "MATCH (b:Movie)-[:ACTED_IN]->(c:Person) RETURN b"
    )

    _, metadata = SchemaValidator(MOVIES).validate(query)

    written = [i["original_path"] for i in metadata if "original_path" in i]
    assert written == [
        "(a:Movie)-[:ACTED_IN]->(p:Person)",
        "(b:Movie)-[:ACTED_IN]->(c:Person)",
    ]


# ----------------------------------------------------------------------------------------------------
# A name that a WITH dropped, or that a CALL { } does not import, is a new variable when it is declared again


def test_a_variable_that_a_with_dropped_does_not_keep_its_old_label():
    # `a` is a new, unlabelled variable after the WITH: the label of the path is inferred, not Movie
    query = "MATCH (a:Movie) WITH count(*) AS c MATCH (a)-[:ACTED_IN]->(m:Movie) RETURN a, c"

    _, _, paths = SchemaValidator(MOVIES).extract(query)

    assert paths == ["(:Person)-[:ACTED_IN]->(:Movie)"]
    assert SchemaValidator(MOVIES).validate(query) == (1.0, [])


def test_a_variable_that_a_with_carries_keeps_its_label():
    query = "MATCH (a:Movie) WITH a MATCH (a)-[:ACTED_IN]->(m:Movie) RETURN a"

    _, _, paths = SchemaValidator(MOVIES).extract(query)

    assert paths == ["(:Movie)-[:ACTED_IN]->(:Movie)"]
    assert SchemaValidator(MOVIES).validate(query)[0] < 1.0


def test_a_name_that_a_subquery_does_not_import_is_not_the_one_outside():
    query = (
        "MATCH (a:Movie) CALL { MATCH (a)-[:ACTED_IN]->(m:Movie) "
        "RETURN count(*) AS c } RETURN a, c"
    )

    _, _, paths = SchemaValidator(MOVIES).extract(query)

    assert paths == ["(:Person)-[:ACTED_IN]->(:Movie)"]


# ----------------------------------------------------------------------------------------------------
# A label or a type that the graph still knows but that nothing has any more (its nodes were deleted) is reported:
# the score is below 1, and a score below 1 always has an issue


@pytest.fixture
def emptied(falkordb_client):
    graph = falkordb_client.select_graph("emptied")
    graph.query("CREATE (:Gone)-[:LOST]->(:Gone), (:Keep)-[:HELD]->(:Keep)")
    graph.query("MATCH (n:Gone) DETACH DELETE n")  # the label and the type stay known
    yield graph
    graph.delete()


@pytest.mark.parametrize(
    ("query", "path"),
    [
        ("MATCH (m:Gone) RETURN m", "(:Gone)"),
        ("MATCH ()-[r:LOST]->() RETURN r", "()-[:LOST]->()"),
    ],
)
def test_a_name_with_nothing_behind_it_is_explained(
    falkordb_client, emptied, query, path
):
    online = SchemaValidator(falkordb_client).validate(query, "emptied")
    offline = SchemaValidator(Snapshot.capture(falkordb_client, "emptied")).validate(
        query
    )

    for score, issues in (online, offline):
        assert score < 1
        assert [i["code"] for i in issues] == ["cypher_guardrail.Warning.UnknownPath"]
    assert online == offline


# ----------------------------------------------------------------------------------------------------
# A `CYPHER a=1 ...` header gives the query its parameters: the validators read the query as if it were not there

HEADED_QUERIES = [
    "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN t",
    "MATCH (g:Ghost)-[:HAS_TARGET]->(t:Target) RETURN t",  # an unknown label
    "MATCH (t:Target)-[:HAS_TARGET]->(g:Goal) RETURN t",  # the wrong direction
    "MATCH (g:Goal)-[:HAS_TARGET]->(t) WHERE t.text = $a RETURN t",  # a label to infer
    "MATCH (g:Goal) WHERE g.zzz = $a RETURN g",  # a property that no label has
]


@pytest.mark.parametrize("header", ["CYPHER a=1 ", "cypher a=1 b='x' "])
@pytest.mark.parametrize("query", HEADED_QUERIES)
def test_a_cypher_header_changes_nothing_for_the_schema_and_properties_validators(
    falkordb_client, db_name, header, query
):
    for validator in (
        SchemaValidator(falkordb_client),
        PropertiesValidator(falkordb_client),
    ):
        assert validator.extract(header + query, db_name) == validator.extract(
            query, db_name
        )
        assert validator.validate(header + query, db_name) == validator.validate(
            query, db_name
        )
