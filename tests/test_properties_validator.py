"""PropertiesValidator against a real FalkorDB: the properties a label has, inference, strictness, index procedures."""

import pytest
from support import code_set

from cypher_guardrail import PropertiesValidator, SchemaValidator, Snapshot
from cypher_guardrail.backend import (
    UNKNOWN_PROPERTY,
)

# ----------------------------------------------------------------------------------------------------
# From test_validators_falkordb.py: The three validators against a real FalkorDB (FalkorDBLite), with a client and with a bound graph.


class TestPropertiesValidator:
    def test_valid_properties(self, driver, db_name):
        score, metadata = PropertiesValidator(driver).validate(
            "MATCH (g:Goal)-[:HAS_TARGET]-(t:Target) WHERE g.code = 1 RETURN t", db_name
        )
        assert score == 1.0
        assert metadata == []

    def test_property_on_the_wrong_label(self, driver, db_name):
        # `text` exists in the graph, but on Target rather than Goal
        score, metadata = PropertiesValidator(driver).validate(
            "MATCH (g:Goal) WHERE g.text = 1 RETURN g", db_name
        )
        assert score == 0.0
        assert code_set(metadata) == {"cypher_guardrail.Warning.InvalidPropertyAccess"}

    def test_unknown_property_key(self, driver, db_name):
        score, metadata = PropertiesValidator(driver).validate(
            "MATCH (g:Goal) WHERE g.nope = 1 RETURN g", db_name
        )
        assert score == 0.0
        assert UNKNOWN_PROPERTY in code_set(metadata)

    def test_partially_valid(self, driver, db_name):
        score, _ = PropertiesValidator(driver).validate(
            "MATCH (g:Goal) WHERE g.code = '1' AND g.nope = 1 RETURN g", db_name
        )
        assert score == 0.5

    def test_no_properties_gives_none(self, driver, db_name):
        score, _ = PropertiesValidator(driver).validate(
            "MATCH (g:Goal) RETURN g", db_name
        )
        assert score is None

    def test_relationship_properties(self, driver, db_name):
        validator = PropertiesValidator(driver)
        assert (
            validator.validate(
                "MATCH ()-[r:HAS_INDICATOR]->() WHERE r.weight = 1 RETURN r", db_name
            )[0]
            == 1.0
        )
        assert (
            validator.validate(
                "MATCH ()-[r:HAS_TARGET]->() WHERE r.weight = 1 RETURN r", db_name
            )[0]
            == 0.0
        )

    def test_extract_by_variable_and_label(self, driver, db_name):
        by_var, by_label = PropertiesValidator(driver).extract(
            "MATCH (g:Goal {title: 'x'})-[:HAS_TARGET]->(t) WHERE g.code = '1' AND t.text = 'y' RETURN g",
            db_name,
        )
        assert by_var == {"t": ["text"]}
        assert set(by_label["node"]) == {
            "Goal",
            "Target",
        }  # Target is inferred for the unlabeled `t`
        assert by_label["relationship"] == {}
        assert sorted(by_label["node"]["Goal"]) == ["code", "title"]
        assert by_label["node"]["Target"] == ["text"]

    def test_inference_finds_the_label_from_properties(self, driver, db_name):
        score, _ = PropertiesValidator(driver).validate(
            "MATCH (a) WHERE a.unit = 'percent' RETURN a", db_name
        )
        assert score == 1.0


# ----------------------------------------------------------------------------------------------------
# From test_index_procedures.py: FalkorDB's full-text and vector index procedures take a label (or type) and an attribute as strings.


class TestPropertiesValidatorIndexProcedures:
    def test_properties_of_the_yielded_node_are_checked(self, falkordb_client, db_name):
        validator = PropertiesValidator(falkordb_client)
        valid = (
            "CALL db.idx.fulltext.queryNodes('Goal', 'x') YIELD node RETURN node.title"
        )
        invalid = "CALL db.idx.fulltext.queryNodes('Goal', 'x') YIELD node RETURN node.nonsense"
        assert validator.extract(valid, db_name) == (
            {},
            {"node": {"Goal": ["title"]}, "relationship": {}},
        )
        assert validator.validate(valid, db_name) == (1.0, [])
        score, metadata = validator.validate(invalid, db_name)
        assert score == 0.0
        assert "cypher_guardrail.Warning.InvalidPropertyAccess" in {
            m["code"] for m in metadata
        }

    def test_the_alias_is_followed(self, falkordb_client, db_name):
        query = "CALL db.idx.fulltext.queryNodes('Goal', 'x') YIELD score, node AS g RETURN g.title, g.code"
        assert PropertiesValidator(falkordb_client).extract(query, db_name) == (
            {},
            {"node": {"Goal": ["title", "code"]}, "relationship": {}},
        )

    def test_properties_of_a_yielded_relationship_are_checked(
        self, falkordb_client, db_name
    ):
        query = "CALL db.idx.fulltext.queryRelationships('HAS_INDICATOR', 'x') YIELD relationship RETURN relationship.weight, relationship.nonsense"
        score, _ = PropertiesValidator(falkordb_client).validate(query, db_name)
        assert score == 0.5

    def test_the_vector_attribute_is_checked(self, falkordb_client, documents):
        validator = PropertiesValidator(falkordb_client)
        valid = "CALL db.idx.vector.queryNodes('Doc', 'embedding', 1, vecf32([1.0, 2.0])) YIELD node RETURN node.title"
        invalid = "CALL db.idx.vector.queryNodes('Doc', 'nope', 1, vecf32([1.0, 2.0])) YIELD node RETURN node.title"
        assert validator.extract(valid, documents)[1] == {
            "node": {"Doc": ["embedding", "title"]},
            "relationship": {},
        }
        assert validator.validate(valid, documents) == (1.0, [])
        score, _ = validator.validate(invalid, documents)
        assert score == 0.5

    def test_the_vector_attribute_of_a_relationship_is_checked(
        self, falkordb_client, documents
    ):
        validator = PropertiesValidator(falkordb_client)
        valid = "CALL db.idx.vector.queryRelationships('CITES', 'emb', 1, vecf32([1.0, 2.0])) YIELD relationship RETURN relationship.note"
        invalid = valid.replace("'emb'", "'nope'")
        assert validator.validate(valid, documents) == (1.0, [])
        assert validator.validate(invalid, documents)[0] == 0.5


class TestNamespacedFunctionsAreNotPropertyAccesses:
    """`vec.euclideanDistance(...)`, `algo.BFS(...)`, `db.idx....` and `1.0` are not `variable.property`."""

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (g:Goal) RETURN g.code, vec.euclideanDistance(vecf32([1.0, 2.0]), vecf32([1.0, 3.0]))",
            "MATCH (g:Goal) CALL algo.BFS(g, 3, 'HAS_TARGET') YIELD nodes RETURN g.code, nodes",
            "MATCH (g:Goal) RETURN g.code, 1.5 + 2.0",
        ],
    )
    def test_only_real_accesses_are_extracted(self, falkordb_client, db_name, query):
        variables, labels = PropertiesValidator(falkordb_client).extract(query, db_name)
        assert variables == {}
        assert labels == {"node": {"Goal": ["code"]}, "relationship": {}}


# ----------------------------------------------------------------------------------------------------
# From test_regressions.py: Regression tests for bugs found in the code review.


class TestDeterminism:
    """The same query must always give the same answer, run after run and process after process."""

    def test_ambiguous_inference_is_stable(self, falkordb_client, ambiguous_name):
        # (x)-[:R]->(y) matches both (:A)->(:B) and (:C)->(:D); the pick used to be random
        results = {
            tuple(
                SchemaValidator(falkordb_client).extract(
                    "MATCH (x)-[:R]->(y) RETURN x", ambiguous_name
                )[2]
            )
            for _ in range(30)
        }
        assert len(results) == 1

    def test_ambiguous_inference_picks_the_first_combination_alphabetically(
        self, falkordb_client, ambiguous_name
    ):
        _, _, paths = SchemaValidator(falkordb_client).extract(
            "MATCH (x)-[:R]->(y) RETURN x", ambiguous_name
        )
        assert paths == ["(:A)-[:R]->(:B)"]

    def test_extract_returns_sorted_lists(self, falkordb_client, db_name):
        nodes, rels, paths = SchemaValidator(falkordb_client).extract(
            "MATCH (t:Target)<-[:HAS_TARGET]-(g:Goal)-[:HAS_TARGET]->(u:Target) "
            "MATCH (t)-[:HAS_INDICATOR]->(i:Indicator) RETURN g",
            db_name,
        )
        assert nodes == sorted(nodes)
        assert rels == sorted(rels)
        assert paths == sorted(paths)

    def test_metadata_keeps_the_order_in_which_problems_were_found(
        self, falkordb_client, db_name
    ):
        # Metadata used to be built through a set, so its order changed with the hash seed
        _, metadata = SchemaValidator(falkordb_client).validate(
            "MATCH (a:Ghost)-[:NOPE]->(b:Target) RETURN a", db_name
        )
        assert [m["code"] for m in metadata] == [
            "cypher_guardrail.Warning.UnknownLabel",
            "cypher_guardrail.Warning.UnknownRelationshipType",
        ]

    def test_properties_are_listed_in_query_order(self, falkordb_client, db_name):
        _, by_label = PropertiesValidator(falkordb_client).extract(
            "MATCH (g:Goal {title:'x', code:'1'}) RETURN g", db_name
        )
        assert by_label == {"node": {"Goal": ["title", "code"]}, "relationship": {}}


# ----------------------------------------------------------------------------------------------------
# Inference over a whole path, on a snapshot (no database needed)

PEOPLE = Snapshot.from_schema(
    nodes={"Person": ["name"], "Movie": ["title"]},
    relationships=[("Person", "ACTED_IN", "Movie")],
    relationship_properties={"ACTED_IN": ["role"]},
    graph="g",
)


def test_every_variable_of_an_inferred_path_is_checked_not_only_the_last():
    query = "MATCH (a)-[r]->(b) WHERE a.zzz = 'x' AND b.title = 1 RETURN a"

    score, metadata = PropertiesValidator(PEOPLE).validate(query)

    assert score == 0.5  # a.zzz is wrong, b.title is right
    wrong = [
        i
        for i in metadata
        if i["code"] == "cypher_guardrail.Warning.InvalidPropertyAccess"
    ]
    assert [(i["kind"], i["name"], i["properties"]) for i in wrong] == [
        ("node", "Person", ["zzz"])
    ]


def test_the_properties_of_every_label_of_an_inferred_path_are_extracted():
    query = (
        "MATCH (a)-[r]->(b) WHERE a.name = 'x' AND b.title = 1 AND r.role = 2 RETURN a"
    )

    variables, labels = PropertiesValidator(PEOPLE).extract(query)

    assert variables == {"a": ["name"], "b": ["title"], "r": ["role"]}
    assert labels == {
        "node": {"Person": ["name"], "Movie": ["title"]},
        "relationship": {"ACTED_IN": ["role"]},
    }


def test_strict_ignores_the_properties_of_every_label_when_one_is_wrong():
    query = "MATCH (a)-[r]->(b) WHERE a.zzz = 'x' AND b.title = 1 RETURN a"

    score, metadata = PropertiesValidator(PEOPLE).validate(query, strict=True)
    _, labels = PropertiesValidator(PEOPLE).extract(query, strict=True)

    assert (score, metadata) == (None, [])  # no label has all of them: none is used
    assert labels == {"node": {}, "relationship": {}}


# ----------------------------------------------------------------------------------------------------
# Alternatives of relationship types, and property maps with quotes (on a snapshot)

SOCIAL = Snapshot.from_schema(
    nodes={"Person": ["name", "city", "title", "time"]},
    relationships=[
        ("Person", "ACTED_IN", "Person"),
        ("Person", "KNOWS", "Person"),
    ],
    relationship_properties={"KNOWS": ["since"], "ACTED_IN": ["role"]},
    graph="g",
)


def test_narrowing_alternatives_keeps_what_the_narrowed_type_already_had():
    # [:ACTED_IN|KNOWS {since: 1}] narrows to KNOWS (the only one with `since`); KNOWS has `zzz` asked elsewhere too
    query = (
        "MATCH ()-[r:ACTED_IN|KNOWS {since: 1}]->() "
        "MATCH ()-[s:KNOWS {zzz: 1}]->() RETURN 1"
    )

    _, labels = PropertiesValidator(SOCIAL).extract(query)
    score, metadata = PropertiesValidator(SOCIAL).validate(query)

    assert sorted(labels["relationship"]["KNOWS"]) == ["since", "zzz"]
    assert score == 0.5  # since is right, zzz is not
    wrong = [
        i
        for i in metadata
        if i["code"] == "cypher_guardrail.Warning.InvalidPropertyAccess"
    ]
    assert [(i["kind"], i["name"], i["properties"]) for i in wrong] == [
        ("relationship", "KNOWS", ["zzz"])
    ]


@pytest.mark.parametrize(
    ("map_text", "expected"),
    [
        (
            "{name: \"O'Brien\", city: 'Paris'}",
            ["name", "city"],
        ),  # a quote inside the other quote
        (
            "{title: 'it\\'s: a', name: 'x'}",
            ["title", "name"],
        ),  # an escaped quote, and a colon
        ("{time: '12:30', name: \"a: b\"}", ["time", "name"]),
        ("{name: 'a', city: \"it's\", title: 'z'}", ["name", "city", "title"]),
    ],
)
def test_a_property_map_with_quotes_gives_all_its_properties(map_text, expected):
    query = f"MATCH (p:Person {map_text}) RETURN p"

    _, labels = PropertiesValidator(SOCIAL).extract(query)

    assert labels == {"node": {"Person": expected}, "relationship": {}}


# ----------------------------------------------------------------------------------------------------
# Text inside strings and comments is not a property access (on a snapshot)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n:Person) RETURN n.name // n.zzz old",
        "MATCH (n:Person) RETURN n.name /* n.zzz and m.qqq */",
        "MATCH (n:Person) WHERE n.name = 'see n.zzz here' RETURN n.name",
        'MATCH (n:Person) WHERE n.name = "n.zzz" RETURN n.name',
        "MATCH (n:Person {name: 'n.zzz'}) RETURN n",
        "MATCH (n:Person) WHERE n.name = 'it\\'s n.zzz' RETURN n.name",
        "MATCH (n:Person) WHERE n.name = 'a}b' RETURN n.name",
    ],
)
def test_what_is_in_a_string_or_a_comment_is_not_a_property_access(query):
    score, metadata = PropertiesValidator(PEOPLE).validate(query)

    assert (score, metadata) == (1.0, [])
    _, labels = PropertiesValidator(PEOPLE).extract(query)
    assert labels == {"node": {"Person": ["name"]}, "relationship": {}}


def test_a_property_inside_a_string_next_to_a_real_one_is_not_counted():
    query = "MATCH (a:Person)-[r:ACTED_IN]->(b:Movie) WHERE a.name = 'b.zzz' AND r.role = 1 RETURN b.title"

    score, metadata = PropertiesValidator(PEOPLE).validate(query)

    assert (score, metadata) == (1.0, [])
    _, labels = PropertiesValidator(PEOPLE).extract(query)
    assert labels == {
        "node": {"Person": ["name"], "Movie": ["title"]},
        "relationship": {"ACTED_IN": ["role"]},
    }


def test_an_index_procedure_still_gets_its_label_and_attribute_from_the_strings():
    query = "CALL db.idx.fulltext.queryNodes('Person', 'x') YIELD node RETURN node.name"

    score, metadata = PropertiesValidator(PEOPLE).validate(query)

    assert (score, metadata) == (1.0, [])


def test_a_path_written_in_a_string_does_not_make_the_validator_look_anything_up():
    plain = PropertiesValidator(PEOPLE)
    with_a_string = PropertiesValidator(PEOPLE)

    plain.validate("MATCH (a)-[r]->(b) WHERE a.name = 'x' RETURN b")
    with_a_string.validate(
        "MATCH (a)-[r]->(b) WHERE a.name = 'see (c)-[:ACTED_IN]->(d) and (e) here' RETURN b"
    )

    assert with_a_string.stats.as_dict() == plain.stats.as_dict()


# ----------------------------------------------------------------------------------------------------
# Each branch of a UNION has its own variables


def test_the_same_variable_in_two_branches_is_a_different_variable():
    query = "MATCH (n:Person) RETURN n.name UNION MATCH (n:Movie) RETURN n.title"

    assert PropertiesValidator(PEOPLE).validate(query) == (1.0, [])
    _, labels = PropertiesValidator(PEOPLE).extract(query)
    assert labels == {
        "node": {"Person": ["name"], "Movie": ["title"]},
        "relationship": {},
    }


def test_a_property_that_is_wrong_in_one_branch_is_reported_for_that_branchs_label():
    query = "MATCH (n:Person) RETURN n.name UNION MATCH (n:Movie) RETURN n.name"

    score, metadata = PropertiesValidator(PEOPLE).validate(query)

    assert score == 0.5  # Person.name is right, Movie.name is not
    assert [(i["kind"], i["name"], i["properties"]) for i in metadata] == [
        ("node", "Movie", ["name"])
    ]


def test_every_branch_of_a_union_all_is_checked_for_its_properties():
    query = (
        "MATCH (a:Person) RETURN a.zzz UNION ALL MATCH (a:Movie) RETURN a.title "
        "UNION ALL MATCH (a:Person) RETURN a.name UNION ALL MATCH (a:Movie) RETURN a.qqq"
    )

    score, metadata = PropertiesValidator(PEOPLE).validate(query)
    wrong = {i["name"]: i["properties"] for i in metadata if "properties" in i}

    assert score == 0.5
    assert wrong == {"Person": ["zzz"], "Movie": ["qqq"]}


def test_the_variables_without_a_label_of_each_branch_are_inferred_on_their_own():
    query = (
        "MATCH (a)-[r]->(b) WHERE a.name = 'x' RETURN a UNION "
        "MATCH (a)-[r]->(b) WHERE b.title = 'y' RETURN b"
    )

    assert PropertiesValidator(PEOPLE).validate(query) == (1.0, [])


def test_a_query_without_a_union_is_extracted_as_before():
    query = "MATCH (a:Person)-[r:ACTED_IN]->(b:Movie) WHERE a.name = 'x' RETURN b.title"

    variables, labels = PropertiesValidator(PEOPLE).extract(query)

    assert labels == {
        "node": {"Person": ["name"], "Movie": ["title"]},
        "relationship": {},
    }
    assert variables == {}


# ----------------------------------------------------------------------------------------------------
# A name that a WITH dropped, or that a CALL { } does not import, is a new variable when it is declared again


def test_a_variable_that_a_with_dropped_is_a_new_one_when_it_comes_back():
    query = "MATCH (n:Movie) WITH n.title AS t MATCH (n:Person) RETURN n.name, t"

    assert PropertiesValidator(PEOPLE).validate(query) == (1.0, [])
    _, labels = PropertiesValidator(PEOPLE).extract(query)
    assert labels == {
        "node": {"Movie": ["title"], "Person": ["name"]},
        "relationship": {},
    }


def test_a_name_that_comes_back_without_a_label_does_not_get_the_old_one():
    query = "MATCH (n:Movie) WITH n.title AS t MATCH (n) RETURN n.name, t"

    variables, labels = PropertiesValidator(PEOPLE).extract(query)

    assert variables == {"n": ["name"]}  # under the name the query writes
    assert labels == {
        "node": {"Movie": ["title"], "Person": ["name"]},
        "relationship": {},
    }


def test_a_variable_that_a_with_carries_keeps_its_label():
    query = "MATCH (n:Person) WITH n MATCH (n) RETURN n.name"

    assert PropertiesValidator(PEOPLE).validate(query) == (1.0, [])


def test_a_property_of_the_wrong_scope_is_reported_for_that_scopes_label():
    query = "MATCH (n:Person) WITH count(*) AS c MATCH (n:Movie) RETURN n.name, c"

    score, metadata = PropertiesValidator(PEOPLE).validate(query)

    assert score == 0.0
    assert [(i["kind"], i["name"], i["properties"]) for i in metadata] == [
        ("node", "Movie", ["name"])
    ]


def test_a_subquery_does_not_see_a_name_it_does_not_import():
    query = (
        "MATCH (n:Person) CALL { MATCH (n:Movie) RETURN n.title AS t } RETURN n.name, t"
    )

    assert PropertiesValidator(PEOPLE).validate(query) == (1.0, [])


def test_the_branches_of_a_union_inside_a_subquery_have_their_own_variables():
    query = (
        "CALL { MATCH (n:Person) RETURN n.name AS x "
        "UNION MATCH (n:Movie) RETURN n.title AS x } RETURN x"
    )

    assert PropertiesValidator(PEOPLE).validate(query) == (1.0, [])
    _, labels = PropertiesValidator(PEOPLE).extract(query)
    assert labels == {
        "node": {"Person": ["name"], "Movie": ["title"]},
        "relationship": {},
    }


# ----------------------------------------------------------------------------------------------------
# A property key between back-quotes (``n.`odd key```, ``{`odd key`: 1}``) is read, online and offline


@pytest.fixture
def quoted_graph(falkordb_client):
    graph = falkordb_client.select_graph("quoted_keys")
    graph.query(
        "CREATE (:Q {`odd key`: 1, ok: 'x', `a}b'c`: 2})-[:T {`rel key`: 3}]->(:Q)"
    )
    yield graph
    graph.delete()


def both(falkordb_client, **options):
    """The validator online and the one offline (from a snapshot of the same graph)."""
    snapshot = Snapshot.capture(
        falkordb_client, "quoted_keys", types=options.get("check_types", False)
    )
    return {
        "online": PropertiesValidator(falkordb_client, **options),
        "offline": PropertiesValidator(snapshot, **options),
    }


@pytest.mark.parametrize(
    ("query", "labels"),
    [
        (
            "MATCH (w:Q) WHERE w.`odd key` = 1 RETURN w.`odd key`, w.ok",
            {"node": {"Q": ["odd key", "ok"]}, "relationship": {}},
        ),
        (
            "MATCH (w:Q {`odd key`: 1, ok: 'x'}) RETURN w",
            {"node": {"Q": ["odd key", "ok"]}, "relationship": {}},
        ),
        (
            "MATCH ()-[r:T]->() RETURN r.`rel key`",
            {"node": {}, "relationship": {"T": ["rel key"]}},
        ),
        (
            "MATCH ()-[r:T {`rel key`: 3}]->() RETURN r",
            {"node": {}, "relationship": {"T": ["rel key"]}},
        ),
        (
            "MATCH (w:Q) RETURN w.`a}b'c`",
            {"node": {"Q": ["a}b'c"]}, "relationship": {}},
        ),  # what is between the quotes is no Cypher
    ],
)
def test_a_quoted_property_key_is_extracted(
    falkordb_client, quoted_graph, query, labels
):
    for mode, validator in both(falkordb_client).items():
        _, found = (
            validator.extract(query, "quoted_keys")
            if mode == "online"
            else validator.extract(query)
        )
        assert found == labels, mode


def test_a_quoted_property_key_that_the_label_has_is_valid(
    falkordb_client, quoted_graph
):
    query = "MATCH (w:Q) WHERE w.`odd key` = 1 RETURN w"

    online = both(falkordb_client)["online"].validate(query, "quoted_keys")
    offline = both(falkordb_client)["offline"].validate(query)

    assert online == offline == (1.0, [])


def test_a_quoted_property_key_that_the_label_lacks_is_reported_by_its_name(
    falkordb_client, quoted_graph
):
    query = "MATCH (w:Q) RETURN w.`odd key`, w.`no such key`"

    online = both(falkordb_client)["online"].validate(query, "quoted_keys")
    offline = both(falkordb_client)["offline"].validate(query)

    assert online == offline
    score, issues = online
    assert score == 0.5
    wrong = [
        i
        for i in issues
        if i["code"] == "cypher_guardrail.Warning.InvalidPropertyAccess"
    ]
    assert [(i["kind"], i["name"], i["properties"]) for i in wrong] == [
        ("node", "Q", ["no such key"])
    ]
    unknown = [i for i in issues if i["code"] == UNKNOWN_PROPERTY]
    assert [i["property"] for i in unknown] == ["no such key"]


def test_a_label_is_inferred_from_a_quoted_property_key(falkordb_client, quoted_graph):
    query = "MATCH (n) WHERE n.`odd key` = 1 RETURN n"

    for mode, validator in both(falkordb_client).items():
        variables, labels = (
            validator.extract(query, "quoted_keys")
            if mode == "online"
            else validator.extract(query)
        )
        assert variables == {"n": ["odd key"]}, mode
        assert labels == {"node": {"Q": ["odd key"]}, "relationship": {}}, mode


def test_a_comparison_with_a_quoted_property_key_is_type_checked(
    falkordb_client, quoted_graph
):
    query = "MATCH (w:Q) WHERE w.`odd key` = 'text' RETURN w"

    for mode, validator in both(falkordb_client, check_types=True).items():
        score, issues = (
            validator.validate(query, "quoted_keys")
            if mode == "online"
            else validator.validate(query)
        )
        assert score == 1.0, mode
        (issue,) = issues
        assert issue["code"] == "cypher_guardrail.Warning.PropertyTypeMismatch", mode
        assert (issue["kind"], issue["name"], issue["property"]) == (
            "node",
            "Q",
            "odd key",
        ), mode


def test_a_name_that_is_not_quoted_is_still_a_syntax_error_for_the_property_check(
    falkordb_client, quoted_graph
):
    # `w.2020` is not Cypher, whatever the graph has: it is not made valid by the quoting
    for mode, validator in both(falkordb_client).items():
        score, _ = (
            validator.validate("MATCH (w:Q) RETURN w.2020", "quoted_keys")
            if mode == "online"
            else validator.validate("MATCH (w:Q) RETURN w.2020")
        )
        assert score in (0.0, None), mode


# ----------------------------------------------------------------------------------------------------
# A node label and a relationship type are told apart: the issue says which of the two it is about


def test_a_property_issue_about_an_unknown_relationship_type_says_it_is_a_relationship():
    # LOVES is no relationship type of the graph. The schema validator says so, and the property issue repeats it:
    # `kind` lets a consumer see that, and match it with the right one of the two unknown-name issues
    query = "MATCH ()-[r:LOVES]->() RETURN r.zzz"

    _, schema_issues = SchemaValidator(PEOPLE).validate(query)
    _, property_issues = PropertiesValidator(PEOPLE).validate(query)

    assert [(i["code"], i["relationship_type"]) for i in schema_issues] == [
        ("cypher_guardrail.Warning.UnknownRelationshipType", "LOVES")
    ]
    (issue,) = property_issues
    assert (issue["kind"], issue["name"], issue["properties"]) == (
        "relationship",
        "LOVES",
        ["zzz"],
    )
    assert issue["description"] == (
        "The relationship type LOVES does not have the following properties:  zzz."
    )


def test_a_real_property_error_is_not_taken_for_the_unknown_relationship_type_of_the_same_name():
    # `Movie` is a node label of the graph and an unknown relationship type; `m.name` is wrong on the node label
    query = "MATCH (m:Movie)-[:Movie]->(p:Person) RETURN m.name"

    _, schema_issues = SchemaValidator(PEOPLE).validate(query)
    _, property_issues = PropertiesValidator(PEOPLE).validate(query)

    assert [(i["code"].rsplit(".", 1)[1]) for i in schema_issues] == [
        "UnknownRelationshipType"
    ]
    (issue,) = property_issues
    assert (issue["kind"], issue["name"], issue["properties"]) == (
        "node",
        "Movie",
        ["name"],
    )
    assert issue["description"] == (
        "The label Movie does not have the following properties:  name."
    )


# A node label and a relationship type can have the same name: their properties are not mixed up
RATED = Snapshot.from_schema(
    nodes={"Person": ["name"], "Rated": ["note"]},
    relationships=[("Person", "Rated", "Rated")],
    relationship_properties={"Rated": ["stars"]},
    graph="g",
)
SAME_NAME = "MATCH (a:Person)-[r:Rated]->(b:Rated) RETURN {relationship}, {node}"


def test_a_node_label_and_a_relationship_type_with_the_same_name_are_kept_apart():
    query = SAME_NAME.format(relationship="r.stars", node="b.note")

    assert PropertiesValidator(RATED).extract(query) == (
        {},
        {"node": {"Rated": ["note"]}, "relationship": {"Rated": ["stars"]}},
    )
    assert PropertiesValidator(RATED).validate(query) == (1.0, [])


def test_the_properties_of_a_node_label_and_a_relationship_type_of_one_name_are_not_mixed_up():
    # each one has the property that the other one has
    query = SAME_NAME.format(relationship="r.note", node="b.stars")

    score, issues = PropertiesValidator(RATED).validate(query)

    assert score == 0.0
    assert sorted((i["kind"], i["name"], tuple(i["properties"])) for i in issues) == [
        ("node", "Rated", ("stars",)),
        ("relationship", "Rated", ("note",)),
    ]


def test_an_inferred_label_and_an_inferred_type_of_one_name_are_kept_apart():
    query = "MATCH (a)-[r]->(b) WHERE r.stars = 1 AND b.note = 'x' RETURN a"

    assert PropertiesValidator(RATED).extract(query)[1] == {
        "node": {"Rated": ["note"]},
        "relationship": {"Rated": ["stars"]},
    }
    assert PropertiesValidator(RATED).validate(query) == (1.0, [])


@pytest.fixture
def rated_graph(falkordb_client):
    graph = falkordb_client.select_graph("rated")
    graph.query(
        "CREATE (:Person {name: 'A'})-[:Rated {stars: 5}]->(:Rated {note: 'x'})"
    )
    yield graph
    graph.delete()


@pytest.mark.parametrize(
    "properties",
    [("r.stars", "b.note"), ("r.note", "b.stars")],
    ids=["right", "swapped"],
)
def test_the_database_and_a_snapshot_tell_a_label_and_a_type_of_one_name_apart(
    falkordb_client, rated_graph, properties
):
    relationship, node = properties
    query = SAME_NAME.format(relationship=relationship, node=node)
    snapshot = Snapshot.capture(falkordb_client, "rated")

    online = PropertiesValidator(falkordb_client).validate(query, "rated")
    offline = PropertiesValidator(snapshot).validate(query)

    assert online == offline
    assert online[0] == (1.0 if properties == ("r.stars", "b.note") else 0.0)
