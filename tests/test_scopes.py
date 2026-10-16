"""Variables have scopes: a name that is dropped by a WITH, or is not imported into a CALL subquery, is another variable.

``separate_variables`` gives the variables that are re-declared in another scope a name of their own, so that the code that
pools variables by name (the validators') does not take two for one; ``restore_variables`` gives the names back where
text leaves the validators. A query with neither WITH nor CALL is not touched.
"""

import pytest

from cypher_guardrail._internal.core.scopes import restore_variables, separate_variables


def separated(query):
    text, renames = separate_variables(query)
    return text, renames


# ---------------------------------------------------------------------- what is not touched


@pytest.mark.parametrize(
    "query",
    [
        "",
        "MATCH (n:Person) RETURN n",
        "MATCH (n:A) MATCH (n:B) RETURN n",  # the same scope: a variable with two labels, as it was
        "MATCH (a)-[r:R]->(b) WHERE a.x = 1 RETURN b",
        "MATCH (n) WHERE n.name STARTS WITH 'a' RETURN n",  # a WITH that is not a clause
        "MATCH (n) WHERE n.name ENDS WITH 'a' AND n.x STARTS WITH 'b' RETURN n",
        "RETURN 'WITH n' AS s, 1 AS x // WITH m",  # a string and a comment
        "MATCH (n:Person) WITH n MATCH (n:Movie) RETURN n",  # n is carried: it is the same variable
        "MATCH (n:Person) WITH n, count(*) AS c MATCH (n)-[:R]->(m) RETURN n, m, c",
        "MATCH (n:Person) WITH * MATCH (n:Movie) RETURN n",  # * carries everything
        "MATCH (n) WITH n AS n MATCH (n) RETURN n",
    ],
)
def test_a_query_whose_variables_are_all_in_one_scope_is_not_touched(query):
    assert separated(query) == (query, {})


# ------------------------------------------------------------------------------- WITH


def test_a_name_that_a_with_drops_is_another_variable_after_it():
    query = "MATCH (n:Person) WITH n.name AS name MATCH (n:Movie) RETURN n.title, name"

    text, renames = separated(query)

    assert text == (
        "MATCH (n:Person) WITH n.name AS name MATCH (n__v1:Movie) RETURN n__v1.title, name"
    )
    assert renames == {"n__v1": "n"}


def test_a_name_the_with_carries_stays_and_one_it_drops_changes():
    query = (
        "MATCH (a:Person)-[r:ACTED_IN]->(m:Movie) WITH a, count(m) AS c WHERE c > 3 "
        "MATCH (a)-[:DIRECTED]->(m:Movie) RETURN a.name, m.title"
    )

    text, renames = separated(query)

    assert text == (
        "MATCH (a:Person)-[r:ACTED_IN]->(m:Movie) WITH a, count(m) AS c WHERE c > 3 "
        "MATCH (a)-[:DIRECTED]->(m__v1:Movie) RETURN a.name, m__v1.title"
    )
    assert renames == {"m__v1": "m"}


def test_each_scope_after_a_with_has_its_own_variable():
    query = "MATCH (n:A) WITH count(*) AS c MATCH (n:B) WITH c, count(*) AS d MATCH (n:C) RETURN c, d, n"

    text, renames = separated(query)

    assert text == (
        "MATCH (n:A) WITH count(*) AS c MATCH (n__v1:B) WITH c, count(*) AS d MATCH (n__v2:C) RETURN c, d, n__v2"
    )
    assert renames == {"n__v1": "n", "n__v2": "n"}


def test_a_name_that_is_carried_by_one_with_and_dropped_by_the_next():
    query = "MATCH (n:A) WITH n MATCH (n)-[:R]->(m) WITH m MATCH (n:B) RETURN n, m"

    text, renames = separated(query)

    assert (
        text
        == "MATCH (n:A) WITH n MATCH (n)-[:R]->(m) WITH m MATCH (n__v1:B) RETURN n__v1, m"
    )
    assert renames == {"n__v1": "n"}


@pytest.mark.parametrize(
    "with_clause",
    [
        "WITH DISTINCT n.name AS name",
        "WITH n.name AS name ORDER BY name LIMIT 5",
        "with n.name as name",
    ],
)
def test_the_forms_of_a_with_clause(with_clause):
    query = f"MATCH (n:Person) {with_clause} MATCH (n:Movie) RETURN n"

    text, renames = separated(query)

    assert renames == {"n__v1": "n"}
    assert text.endswith("MATCH (n__v1:Movie) RETURN n__v1")


def test_a_with_where_and_the_expressions_in_its_items_are_in_the_old_scope_and_the_new_one():
    query = (
        "MATCH (n:Person) WITH n.name AS name, n.age AS age WHERE age > 3 "
        "MATCH (n:Movie) WHERE n.year = 1 RETURN name, n.title"
    )

    text, _ = separated(query)

    assert "n.name AS name, n.age AS age" in text  # the old n
    assert (
        "MATCH (n__v1:Movie) WHERE n__v1.year = 1 RETURN name, n__v1.title" in text
    )  # the new one


def test_an_alias_is_not_a_variable_that_pools_with_a_node_of_that_name():
    query = "MATCH (m:Movie) WITH m.title AS t MATCH (p:Person) RETURN t, p.name"

    assert separated(query) == (query, {})


# ----------------------------------------------------- what is not a variable (and so is not renamed)


@pytest.mark.parametrize(
    "query",
    [
        # a property, a label, a type and a map key that are called like the variable
        "MATCH (n:Person) WITH count(*) AS c MATCH (m:n {n: 1})-[:n|n]->(x) WHERE m.n = 1 RETURN m.n, x.n",
        # a function and a namespaced procedure
        "MATCH (n:Person) WITH count(*) AS c CALL db.labels() YIELD label RETURN label, c",
        "MATCH (n:Person) WITH size(n.names) AS c MATCH (x) WHERE id(x) = c RETURN x",
        # keywords
        "MATCH (n:Person) WITH n.name AS name MATCH (x:Movie) WHERE x.title CONTAINS name AND NOT x.a IS NULL RETURN x ORDER BY x.title DESC SKIP 1 LIMIT 2",
        # a parameter
        "MATCH (n:Person) WITH n.name AS name MATCH (x) WHERE x.name = $n RETURN x",
    ],
)
def test_only_variables_are_renamed(query):
    text, renames = separate_variables(query)

    assert renames == {}
    assert text == query


def test_a_word_that_is_a_label_property_type_or_key_is_left_when_a_variable_of_that_name_is_renamed():
    query = (
        "MATCH (n:Person) WITH n.name AS name "
        "MATCH (n:n {n: 1})-[:n|n]->(x) WHERE n.n = 1 RETURN n.n, x.n"
    )

    text, renames = separated(query)

    assert renames == {"n__v1": "n"}
    assert text == (
        "MATCH (n:Person) WITH n.name AS name "
        "MATCH (n__v1:n {n: 1})-[:n|n]->(x) WHERE n__v1.n = 1 RETURN n__v1.n, x.n"
    )


def test_a_map_value_is_a_variable_and_its_key_is_not():
    query = "MATCH (n:Person) WITH n.name AS name MATCH (n:Movie {title: name}) RETURN n {n: 1, title: n.title}"

    text, _ = separated(query)

    assert text.endswith(
        "MATCH (n__v1:Movie {title: name}) RETURN n__v1 {n: 1, title: n__v1.title}"
    )


def test_strings_comments_and_quoted_names_are_left_as_they_are():
    query = (
        "MATCH (n:Person) WITH n.name AS name "
        "MATCH (n:`My n`) WHERE n.x = 'n' // n\n RETURN n.`n`, 'n'"
    )

    text, renames = separated(query)

    assert renames == {"n__v1": "n"}
    assert text == (
        "MATCH (n:Person) WITH n.name AS name "
        "MATCH (n__v1:`My n`) WHERE n__v1.x = 'n' // n\n RETURN n__v1.`n`, 'n'"
    )


def test_a_new_name_is_not_one_the_query_has():
    query = "MATCH (n:Person) WITH n.name AS name MATCH (n:Movie) RETURN n, n__v1"

    text, renames = separated(query)

    assert renames == {"n__v2": "n"}
    assert "n__v1" in text and text.count("n__v2") == 2


# ------------------------------------------------------------------------------- CALL


def test_a_name_that_a_subquery_does_not_import_is_another_variable_in_it():
    query = (
        "MATCH (n:Person) CALL { MATCH (n:Movie) RETURN n.title AS t } RETURN n.name, t"
    )

    text, renames = separated(query)

    assert text == (
        "MATCH (n:Person) CALL { MATCH (n__v1:Movie) RETURN n__v1.title AS t } RETURN n.name, t"
    )
    assert renames == {"n__v1": "n"}


def test_a_name_that_a_subquery_imports_is_the_same_variable():
    query = "MATCH (n:Person) CALL { WITH n MATCH (n)-[:R]->(m) RETURN m.title AS t } RETURN n.name, t"

    assert separated(query) == (query, {})


def test_the_name_a_subquery_returns_is_the_variable_of_the_subquery_outside_it():
    query = "MATCH (n:Person) CALL { MATCH (n:Movie) RETURN n } RETURN n.title"

    text, renames = separated(query)

    assert (
        text
        == "MATCH (n:Person) CALL { MATCH (n__v1:Movie) RETURN n__v1 } RETURN n__v1.title"
    )
    assert renames == {"n__v1": "n"}


def test_the_branches_of_a_union_in_a_subquery_have_their_own_variables():
    query = "CALL { MATCH (n:Person) RETURN n.name AS x UNION MATCH (n:Movie) RETURN n.title AS x } RETURN x"

    text, renames = separated(query)

    assert text == (
        "CALL { MATCH (n:Person) RETURN n.name AS x UNION MATCH (n__v1:Movie) RETURN n__v1.title AS x } RETURN x"
    )
    assert renames == {"n__v1": "n"}


def test_a_with_inside_a_subquery_is_a_scope_of_the_subquery():
    query = "MATCH (n:Person) CALL { MATCH (n:Movie) WITH n.title AS t MATCH (n:Studio) RETURN t, n.name AS s } RETURN t, s"

    text, renames = separated(query)

    assert renames == {"n__v1": "n", "n__v2": "n"}
    assert text.count("n__v1") == 2 and text.count("n__v2") == 2


def test_two_subqueries_one_after_the_other_do_not_share_their_variables():
    query = (
        "CALL { MATCH (n:Person) RETURN n.name AS a } "
        "CALL { MATCH (n:Movie) RETURN n.title AS b } RETURN a, b"
    )

    text, renames = separated(query)

    assert text == (
        "CALL { MATCH (n:Person) RETURN n.name AS a } "
        "CALL { MATCH (n__v1:Movie) RETURN n__v1.title AS b } RETURN a, b"
    )
    assert renames == {"n__v1": "n"}


def test_a_brace_that_is_a_map_and_not_a_subquery_is_not_one():
    query = (
        "MATCH (n:Person {name: 'a'}) WITH n MATCH (m:Movie {title: 'b'}) RETURN n, m"
    )

    assert separated(query) == (query, {})


# --------------------------------------------------------------------------------- restore


def test_the_names_are_given_back_in_texts_lists_tuples_and_dicts():
    renames = {"n__v1": "n", "m__v2": "m"}

    assert restore_variables("(n__v1:A)-[:R]->(m__v2)", renames) == "(n:A)-[:R]->(m)"
    assert restore_variables(["n__v1", ("m__v2",)], renames) == ["n", ("m",)]
    assert restore_variables({"n__v1": ["x"], "k": "m__v2"}, renames) == {
        "n": ["x"],
        "k": "m",
    }


def test_only_whole_names_are_given_back():
    renames = {"n__v1": "n"}

    assert (
        restore_variables("n__v10 xn__v1 n__v1_ n__v1", renames)
        == "n__v10 xn__v1 n__v1_ n"
    )


def test_two_names_given_back_to_the_same_name_merge_their_dicts():
    renames = {"n__v1": "n"}

    assert restore_variables({"n": ["a"], "n__v1": ["b", "a"]}, renames) == {
        "n": ["a", "b"]
    }
    assert restore_variables({"n": {"x": None}, "n__v1": {"y": None}}, renames) == {
        "n": {"x": None, "y": None}
    }


def test_nothing_renamed_means_nothing_to_give_back():
    value = ["n", {"m": ("x",)}]

    assert restore_variables(value, {}) is value


def test_what_is_separated_and_restored_is_the_query():
    query = "MATCH (a:P)-[r]->(m:M) WITH a, count(m) AS c MATCH (a)-[:D]->(m:M) RETURN a.name, m.title, c"

    text, renames = separated(query)

    assert restore_variables(text, renames) == query


def test_whatever_it_is_given_it_does_not_raise_and_can_be_given_back():
    import random

    pieces = [
        "MATCH", "WITH", "CALL", "{", "}", "(", ")", "[", "]", ":", ",", ".", "|", "*", "n", "m", "x", "AS",
        "RETURN", "UNION", "WHERE", "'s'", "1", "$p", "`q`", "//", "\n", "n.name", "(n:A)", "STARTS", "ENDS",
        "DISTINCT", "ORDER", "BY", "LIMIT",
    ]  # fmt: skip
    rng = random.Random(11)
    for _ in range(3000):
        query = " ".join(rng.choice(pieces) for _ in range(rng.randint(0, 22)))
        text, renames = separate_variables(query)
        assert restore_variables(text, renames) == query
        assert all(old.isidentifier() for old in renames.values())
