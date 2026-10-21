"""The offline mode: ``SnapshotResolver`` answers the lookups from a snapshot, held against the database's answers.

The first part builds the snapshot by hand and needs no database (so it runs on every Python version); the second
captures a graph and asks thousands of patterns both ways.
"""

import dataclasses
import itertools
import random

import pytest
from support import LAB

from cypher_guardrail import Snapshot
from cypher_guardrail._internal.core import queries as _queries
from cypher_guardrail._internal.core.errors import CypherError
from cypher_guardrail._internal.core.io import Lookup
from cypher_guardrail._internal.core.valuetypes import fits
from cypher_guardrail._internal.sources import online as _online
from cypher_guardrail._internal.sources.offline import (
    Resolution,
    SnapshotResolver,
    _parse,
)
from cypher_guardrail.backend import get_backend

# ----------------------------------------------------------------------------------------------------
# ``SnapshotResolver``: the lookups of the validators, answered from a ``Snapshot`` and not from the database.
#
# These tests build the snapshot by hand, so they need no database and run on every Python version
# (``test_offline_differential.py`` holds the same answers against a real FalkorDB).
#
# An answer is a ``Resolution``: its ``value`` (what the database would have answered), ``reliable`` (False when the
# snapshot can only say "maybe": a path of 2 or more hops that exists between the *kinds* of nodes but may not exist
# between real ones, or a property value it cannot compare) and ``miss`` (the snapshot says the thing is not there).


RESOLVER = SnapshotResolver(LAB)


def ask(kind, *args):
    return RESOLVER.answer(Lookup(kind, "lab", args))


def path(pattern):
    return ask("path_exists", pattern)


def exact(value):
    return Resolution(value, reliable=True, miss=not value)


MAYBE = Resolution(True, reliable=False, miss=False)


# ------------------------------------------------------------------------------------ nodes


@pytest.mark.parametrize(
    ("pattern", "answer"),
    [
        ("(:A)", exact(True)),
        ("(:Extra)", exact(True)),
        ("(:A:Extra)", exact(True)),
        ("(:Extra:A)", exact(True)),
        ("(:B:Extra)", exact(False)),
        ("(:Nope)", exact(False)),
        ("()", exact(True)),
        ("(a:A)", exact(True)),
        (" ( a : A ) ", exact(True)),
        # a property map: the keys can be checked, the values cannot
        ("(:A {p: 1})", MAYBE),
        ("(:A {p: 1, q: 'x'})", MAYBE),
        ("(:A {zzz: 1})", exact(False)),
        ("(:A {z: 1})", exact(False)),  # z exists, but not on A
        ("(:D {z: 'a:b'})", MAYBE),
        ("(:B {p: 1, q: 1})", exact(False)),  # q is on A, not on B
        ("({p: 1})", MAYBE),
        ("({nope: 1})", exact(False)),
    ],
)
def test_a_node(pattern, answer):
    assert path(pattern) == answer


# -------------------------------------------------------------------------- one-hop paths


@pytest.mark.parametrize(
    ("pattern", "exists"),
    [
        ("(:A)-[:R]->(:B)", True),
        ("(:B)-[:R]->(:A)", False),
        ("(:A)<-[:R]-(:B)", False),
        ("(:B)<-[:R]-(:A)", True),
        ("(:A)-[:R]-(:B)", True),
        ("(:B)-[:R]-(:A)", True),
        ("(:A)-[:S]->(:B)", False),
        ("(:A)-[:S|R]->(:B)", True),
        ("(:A)-[:S|:R]->(:B)", True),
        ("()-[:R]-()", True),
        ("()-[:NOPE]-()", False),
        ("(:Extra)-[:T]->(:B)", True),
        ("(:A)-[:T]->(:B)", True),  # an A that is also an Extra is an A
        ("(:A:Extra)-[:T]->(:B)", True),
        ("(:A:Extra)-[:R]->(:B)", False),
        ("(:C)-[:R]->(:C)", True),
        ("(a:A)-[r:R]->(b:B)", True),
        ("()-[:U]->(:A)", True),
        ("(:A)<-[:U]-()", True),
        ("(:A)-[:U]->()", False),
        ("(:A)-[:R]->()", True),
        ("()-[:R]->(:A)", True),
        ("(:D)-[]-()", False),
        ("(:D)-[r]-()", False),
        # the shorthand arrows
        ("(:A)-->(:B)", True),
        ("(:B)<--(:A)", True),
        ("(:B)-->(:A)", False),
        ("(:A)--(:C)", True),
        ("(:A)--(:D)", False),
        ("(a:A)-[]->(b)", True),
        # spaces
        ("( a : A ) - [ r : R ] -> ( b : B )", True),
        ("(:A)  <-  [:R]  -  (:B)", False),
    ],
)
def test_a_one_hop_path(pattern, exists):
    answer = path(pattern)

    assert answer.value is exists
    assert answer.miss is (not exists)
    assert answer.reliable is True


@pytest.mark.parametrize(
    ("pattern", "answer"),
    [
        # the same node on both ends: the kinds fit, a self loop of a real node is not known
        ("(a:C)-[:R]->(a)", MAYBE),
        ("(a)-[:R]->(a)", MAYBE),
        ("(a:A)-[:R]->(a)", exact(False)),
        # a relationship property map
        ("(:A)-[:R {w: 1}]->(:B)", MAYBE),
        ("(:A)-[:R {nope: 1}]->(:B)", exact(False)),
        ("(:A)-[:S {w: 1}]->(:B)", exact(False)),
        ("(:B)-[:S {w: 1}]->(:C)", exact(False)),  # w is on R, not on S
        ("(:A {p: 1})-[:R]->(:B {p: 2})", MAYBE),
        ("(:A {zzz: 1})-[:R]->(:B)", exact(False)),
    ],
)
def test_a_path_with_the_same_variable_twice_or_with_properties(pattern, answer):
    assert path(pattern) == answer


# ------------------------------------------------------------------ variable length paths


@pytest.mark.parametrize(
    ("pattern", "answer"),
    [
        # a path of one hop is exact, whatever the range says
        ("(:A)-[:R*]->(:B)", exact(True)),
        ("(:A)-[:R*1]->(:B)", exact(True)),
        ("(:A)-[:R*1..1]->(:B)", exact(True)),
        ("(:A)-[:R*..1]->(:B)", exact(True)),
        ("(:A)-[:R*0..1]->(:B)", exact(True)),
        ("(:B)-[:R*]->(:A)", exact(False)),
        # no hops: the node itself
        ("(:D)-[:R*0..]->(:D)", exact(True)),
        ("(:D)-[*0..]->(:D)", exact(True)),
        ("(:D)-[:R*0]->(:D)", exact(True)),
        (
            "(:A)-[:R*0..]->(:A:Extra)",
            exact(True),
        ),  # the same node is an A and an Extra
        ("(:B)-[:R*0..]->(:A)", exact(False)),
        ("(:D)-[:R*1..]->(:D)", exact(False)),
        ("(:D)-[:R*]->(:D)", exact(False)),
        # two or more hops: no chain of kinds is a certain "no"
        ("(:Src)-[:R*2]->(:Dst)", exact(False)),
        ("(:A)-[:R*2]->(:B)", exact(False)),  # A-R->B, and B has no R
        ("(:B)-[:S*3]->(:B)", exact(False)),
        # a chain of kinds is a "maybe": it may not exist between real nodes
        ("(:Src)-[:R|S*2]->(:Dst)", MAYBE),
        ("(:Src)-[:R|S*]->(:Dst)", MAYBE),
        ("(:Src)-[:R|S*2..3]->(:Dst)", MAYBE),
        ("(:Src)-[:R|S*..2]->(:Dst)", MAYBE),
        ("(:Src)-[:R|S*2..]->(:Dst)", MAYBE),
        ("(:C)-[:R*2]->(:A)", MAYBE),  # C-R->C-R->A
        ("(:C)<-[:R*2]-(:C)", MAYBE),
        ("(:C)-[:R*3..5]->(:A)", MAYBE),
        (
            "(:A)-[:R*2]-(:A)",
            MAYBE,
        ),  # undirected: A-R->B... needs R back: A<-R-C then C-R->A
        ("(:A)-[*2..3]->(:C)", MAYBE),  # A-R->B-S->C
        ("(:Src)-[:R|S*3]->(:Dst)", exact(False)),
        ("(:Dst)-[:R|S*]->(:Src)", exact(False)),
        ("(:Dst)<-[:R|S*]-(:Src)", MAYBE),
        # one hop is found first: still exact
        ("(:C)-[:R*1..2]->(:C)", exact(True)),
        ("(:C)-[:R*..2]->(:A)", exact(True)),
    ],
)
def test_a_variable_length_path(pattern, answer):
    assert path(pattern) == answer


def test_a_path_that_can_go_on_for_ever_is_answered():
    # C-R->C is a loop of kinds: the search must stop
    assert path("(:C)-[:R*]->(:C)") == exact(True)
    assert path("(:C)-[:R*50..]->(:A)") == MAYBE
    # C-R->C...C-R->A-R->B: a chain of kinds that long exists too
    assert path("(:C)-[:R*50..]->(:B)") == MAYBE
    assert path("(:C)-[:S*50..]->(:B)") == exact(False)


# ---------------------------------------------------------------- not understood, not allowed


def test_a_label_with_alternatives_is_a_syntax_error_as_it_is_for_the_database():
    with pytest.raises(CypherError) as error:
        path("(:A|B)")
    assert error.value.code == "FalkorDB.ClientError.Statement.SyntaxError"
    with pytest.raises(CypherError):
        path("(:A|B)-[:R]->(:B)")


# ------------------------------------------------------------------------------ the names


def test_the_names_the_graph_does_not_know():
    assert ask("missing_names", "labels", ("A", "Nope", "Zed")) == Resolution(
        ("Nope", "Zed"), reliable=True, miss=True
    )
    assert ask("missing_names", "labels", ("A", "B")) == Resolution(
        (), reliable=True, miss=False
    )
    assert ask("missing_names", "relationship_types", ("R", "X")).value == ("X",)
    assert ask("missing_names", "property_keys", ("p", "nope")).value == ("nope",)
    assert ask("missing_names", "labels", ()).value == ()


def test_every_name_the_graph_knows_sorted():
    labels = ask("listed_names", "labels")
    assert labels == Resolution(tuple(sorted(labels.value)), reliable=True, miss=False)
    assert {"A", "B"} <= set(labels.value)
    assert ask("listed_names", "relationship_types").value == tuple(
        sorted(ask("listed_names", "relationship_types").value)
    )


# ---------------------------------------------------------------------------- properties


@pytest.mark.parametrize(
    ("label", "kind", "prop", "exists"),
    [
        ("A", "node", "p", True),
        ("A", "node", "q", True),
        ("B", "node", "q", False),
        ("D", "node", "z", True),
        ("A", "node", "nope", False),
        ("Nope", "node", "p", False),
        ("R", "relationship", "w", True),
        ("S", "relationship", "w", False),
        ("Nope", "relationship", "w", False),
    ],
)
def test_a_property_of_a_label(label, kind, prop, exists):
    answer = ask("property_exists", label, kind, prop)

    assert answer == Resolution(exists, reliable=True, miss=not exists)


@pytest.mark.parametrize(
    ("label", "prop"),
    [("Two Words", "p"), ("A", "two words"), ("A", "2020"), ("A", "a-b"), ("1A", "p")],
)
def test_a_name_that_is_not_an_identifier_is_a_syntax_error_as_it_is_for_the_database(
    label, prop
):
    # FalkorDB cannot parse (x:Two Words) or x.2020, so the property check raises there as it does here
    with pytest.raises(CypherError) as error:
        ask("property_exists", label, "node", prop)
    assert error.value.code == "FalkorDB.ClientError.Statement.SyntaxError"


# ------------------------------------------------------------------------------- indexes


@pytest.mark.parametrize(
    ("args", "exists"),
    [
        (("fulltext", "relationships", "R", "w"), True),
        (("fulltext", "relationships", "R", None), True),
        (("vector", "relationships", "R", "w"), False),
        (("fulltext", "nodes", "R", "w"), False),
        (("vector", "nodes", "B", "q"), True),
        (("vector", "nodes", "B", "p"), False),
        (("fulltext", "nodes", "B", None), True),
        (("vector", "nodes", "B", None), True),
        (("fulltext", "nodes", "A", "p"), False),  # A has a range index on p
        (("fulltext", "nodes", "Nope", None), False),
    ],
)
def test_an_index(args, exists):
    assert ask("index_exists", *args) == Resolution(
        exists, reliable=True, miss=not exists
    )


# ----------------------------------------------------------------------------- multilabels


def test_the_labels_that_occur_together_on_a_node():
    answer = ask("multilabels")

    assert [sorted(labels) for labels in answer.value] == [["A", "Extra"]]
    assert answer.reliable is True


# --------------------------------------------------------------------- label combinations


def combinations(pattern):
    answer = ask("label_combinations", pattern)
    frame, node_vars, rel_vars = answer.value
    rows = None if frame is None else {tuple(row) for row in frame.to_numpy().tolist()}
    columns = None if frame is None else list(frame.columns)
    return rows, columns, node_vars, rel_vars, answer


def test_every_combination_of_a_path_with_variables():
    rows, columns, node_vars, rel_vars, answer = combinations("(a)-[r]->(b)")

    assert (node_vars, rel_vars, columns) == (["a", "b"], ["r"], ["a", "b", "r"])
    assert rows == {
        ("A", "B", "R"),
        ("C", "A", "R"),
        ("C", "C", "R"),
        ("B", "C", "S"),
        ("A", "B", "T"),
        (
            "Extra",
            "B",
            "T",
        ),  # a node that is an A and an Extra has both labels, one row each
        ("Src", "Mid", "R"),
        ("Mid", "Dst", "S"),
        # the start node of ()-U->A has no label: UNWIND of nothing gives no row
    }
    assert answer.reliable is True
    assert answer.miss is False


def test_a_variable_with_a_label_is_not_a_column():
    rows, columns, *_ = combinations("(a)-[r]->(:B)")
    assert columns == ["a", "r"]
    assert rows == {("A", "R"), ("A", "T"), ("Extra", "T")}

    rows, columns, *_ = combinations("(:A)-[:R]->(b)")
    assert columns == ["b"]
    assert rows == {("B",)}


def test_the_direction_decides_which_node_is_which():
    assert combinations("(a)<-[:R]-(b)")[0] == {
        ("B", "A"),
        ("A", "C"),
        ("C", "C"),
        ("Mid", "Src"),
    }
    assert combinations("(a)-[:S]-(b)")[0] == {
        ("B", "C"),
        ("C", "B"),
        ("Mid", "Dst"),
        ("Dst", "Mid"),
    }


def test_a_single_node_gives_every_label_a_node_has():
    rows, columns, node_vars, rel_vars, _ = combinations("(a)")

    assert (columns, node_vars, rel_vars) == (["a"], ["a"], [])
    assert rows == {
        (name,) for name in ("A", "B", "C", "D", "Dst", "Extra", "Mid", "Src")
    }


def test_a_relationship_with_a_variable_only():
    rows, columns, *_ = combinations("()-[r]->()")

    assert columns == ["r"]
    assert rows == {("R",), ("S",), ("T",), ("U",)}


def test_the_types_that_connect_two_labels():
    assert combinations("(:A)-[r]->(:B)")[0] == {("R",), ("T",)}
    assert combinations("(:B)-[r]->(:A)")[0] == set()


def test_no_combination_is_an_empty_table_with_the_variables_as_columns():
    rows, columns, *_, answer = combinations("(a)-[:NOPE]->(b)")

    assert rows == set()
    assert columns == ["a", "b"]
    assert answer.value[0].empty
    assert answer.miss is True


def test_a_path_with_no_variable_to_infer_has_no_table():
    # the database is asked an empty query and rejects it: the answer is "no table"
    answer = ask("label_combinations", "(:A)-[:R]->(:B)")

    assert answer.value == (None, [], [])


def test_the_same_variable_twice_is_one_column():
    rows, columns, *_ = combinations("(a)-[:R]->(a)")

    assert columns == ["a"]
    assert rows == {("C",)}


def test_the_combinations_of_a_variable_length_path_are_a_maybe_when_they_need_2_hops():
    rows, columns, _, _, answer = combinations("(a)-[:R|S*2]->(b)")

    assert columns == ["a", "b"]
    assert ("Src", "Dst") in rows
    assert answer.reliable is False

    *_, answer = combinations("(a)-[:R*1]->(b)")
    assert answer.reliable is True


def test_a_label_with_alternatives_in_an_inferred_path_is_a_syntax_error_for_the_database():
    # the database rejects the inference query, which the validators take as "no table"
    answer = ask("label_combinations", "(a:A|B)-[r]->(c)")

    assert answer.value[0] is None


# ------------------------------------------------------------------------------ the driver


def test_the_resolver_works_as_a_resolver_of_the_drivers():
    from cypher_guardrail._internal.core import io as _io

    def logic():
        exists = yield Lookup("path_exists", "lab", ("(:A)-[:R]->(:B)",))
        missing = yield Lookup("missing_names", "lab", ("labels", ("A", "Nope")))
        return exists, missing

    class NoTransport:
        def run(self, request):
            raise AssertionError(
                f"the snapshot answers; the database is not asked: {request}"
            )

    assert _io.run_sync(logic(), NoTransport(), RESOLVER.resolve) == (True, ("Nope",))


def test_an_unknown_lookup_is_an_error():
    with pytest.raises(ValueError, match="nope"):
        RESOLVER.answer(Lookup("nope", "lab", ()))


def test_the_graph_name_of_a_lookup_is_not_the_resolvers_business():
    # whether a snapshot may answer for another graph is decided by whoever uses the resolver
    assert RESOLVER.answer(Lookup("path_exists", "other", ("(:A)",))) == exact(True)


# ------------------------------------------------------------------------ the types of a property

# LAB with the types of the values: A has p of both Integer and String, q a String; B has p an Integer; R has w a Float
TYPED = SnapshotResolver(
    dataclasses.replace(
        LAB,
        property_types=(
            ("A", "NODE", (("p", ("Integer", "String")), ("q", ("String",)))),
            ("B", "NODE", (("p", ("Integer",)),)),
            ("R", "RELATIONSHIP", (("w", ("Float",)),)),
        ),
    )
)


def types(label, kind, prop, literal_type):
    return TYPED.answer(
        Lookup("property_types", "lab", (label, kind, prop, literal_type))
    )


def test_the_types_a_snapshot_has_for_a_property_are_the_answer():
    assert types("A", "node", "p", "String") == Resolution(("Integer", "String"))
    assert types("A", "node", "q", "String") == Resolution(("String",))
    assert types("R", "relationship", "w", "Float") == Resolution(("Float",))


def test_a_type_that_was_not_seen_is_a_miss():
    answer = types("A", "node", "q", "Integer")

    assert answer == Resolution(("String",), reliable=True, miss=True)
    assert types("B", "node", "p", "Boolean").miss is True


def test_an_integer_and_a_float_are_the_same_kind_of_value():
    assert types("B", "node", "p", "Float").miss is False  # 2 = 2.0
    assert types("R", "relationship", "w", "Integer").miss is False


@pytest.mark.parametrize(
    ("label", "kind", "prop"),
    [
        ("A", "node", "nope"),  # a property the label does not have
        ("Nope", "node", "p"),  # a label that is not there
        ("A", "relationship", "p"),  # a label asked as a relationship type
        ("R", "node", "w"),  # a type asked as a label
        ("D", "node", "p"),  # a label of the graph that has no types recorded
    ],
)
def test_nothing_known_about_a_property_is_a_miss_with_no_types(label, kind, prop):
    assert types(label, kind, prop, "String") == Resolution((), miss=True)


@pytest.mark.parametrize("name", ["Two Words", "x.2020", "1abc", ""])
def test_a_name_the_database_cannot_parse_has_no_types_and_is_no_error(name):
    # online it is the same: the database rejects the query, and no type is known
    assert types(name, "node", "p", "String") == Resolution((), miss=True)
    assert types("A", "node", name, "String") == Resolution((), miss=True)


def test_a_snapshot_without_the_types_gives_no_answer_and_asks_the_database():
    answer = ask("property_types", "A", "node", "p", "String")

    assert answer == Resolution(None, reliable=False, miss=False)


def test_the_types_of_a_snapshot_with_no_properties_at_all_are_known_to_be_none():
    empty = SnapshotResolver(Snapshot(property_types=()))

    answer = empty.answer(Lookup("property_types", "", ("A", "node", "p", "String")))

    assert answer == Resolution((), miss=True)


def test_the_snapshot_resolver_answers_the_types_with_the_plain_value():
    lookup = Lookup("property_types", "lab", ("A", "node", "p", "String"))
    generator = TYPED.resolve(lookup)

    with pytest.raises(StopIteration) as stop:
        generator.send(None)

    assert stop.value.value == ("Integer", "String")


# ----------------------------------------------------------------------------------------------------
# The snapshot gives the answers the database gives (``SnapshotResolver`` against FalkorDB, on the same graph).
#
# A graph is built with the shapes that matter: nodes that carry several labels, a node with none, a node that
# loops to itself, paths of kinds that exist between the *kinds* of nodes but not between real ones. A ``Snapshot`` of it
# is captured, and thousands of patterns are asked both ways. The rule the snapshot is held to:
#
# * an answer it calls reliable is the database's answer;
# * an answer it calls a "maybe" is a ``True``, and the database may say ``False``;
# * it never says ``False`` when the database says ``True``.


GRAPH = "offline_lab"


SEED = """
CREATE (a1:A {p: 1, q: 'x'}), (b1:B {p: 2}), (c1:C), (c2:C), (ae:A:Extra {p: 3}),
       (d1:D {z: 1}), (u0), (src:Src), (mid1:Mid {n: 1}), (mid2:Mid {n: 2}), (dst:Dst)
CREATE (a1)-[:R {w: 1}]->(b1), (b1)-[:S]->(c1), (c1)-[:R]->(a1), (c2)-[:R]->(c2),
       (ae)-[:T]->(b1), (u0)-[:U]->(a1), (src)-[:R]->(mid1), (mid2)-[:S]->(dst)
"""


@pytest.fixture(scope="module")
def lab(falkordb_client):
    graph = falkordb_client.select_graph(GRAPH)
    graph.query(SEED)
    graph.query("CREATE FULLTEXT INDEX FOR (a:A) ON (a.q)")
    graph.query(
        "CREATE VECTOR INDEX FOR (b:B) ON (b.p) OPTIONS {dimension: 2, similarityFunction: 'euclidean'}"
    )
    graph.query("CREATE FULLTEXT INDEX FOR ()-[r:R]-() ON (r.w)")
    # (no graph.delete(): nothing else uses this graph, the database goes with the session, and dropping a graph
    # that has full-text and vector indexes takes four seconds)
    return falkordb_client


@pytest.fixture(scope="module")
def backend(lab):
    return get_backend(lab)


@pytest.fixture(scope="module")
def resolver(lab):
    return SnapshotResolver(Snapshot.capture(lab, GRAPH))


def online_path(backend, pattern):
    try:
        return backend.run(_queries.path_exists(GRAPH, pattern))[0]
    except CypherError:
        return None


def offline_path(resolver, pattern):
    return resolver.answer(Lookup("path_exists", GRAPH, (pattern,)))


def compare(patterns, backend, resolver):
    """The patterns where the snapshot breaks its rule, how many the database rejects, how many maybes were wrong."""
    broken, rejected, wrong_maybes, exact_trues = {}, [], 0, 0
    for pattern in patterns:
        online = online_path(backend, pattern)
        if online is None:
            rejected.append(pattern)
            continue
        offline = offline_path(resolver, pattern)
        if (
            (offline.reliable and offline.value != online)
            or (not offline.reliable and not offline.value)
            or (online and not offline.value)
        ):
            broken[pattern] = (online, offline)
        elif not offline.reliable and not online:
            wrong_maybes += 1
        elif offline.value and offline.reliable:
            exact_trues += 1
    return broken, rejected, wrong_maybes, exact_trues


LABELS = ["", ":A", ":B", ":C", ":Extra", ":A:Extra", ":Src", ":Nope"]


TYPES = ["", ":R", ":S", ":T", ":U", ":R|S", ":NOPE"]


LENGTHS = ["", "*", "*2", "*1..2", "*0..", "*..2", "*2..3", "*0", "*3.."]


# The labels the chains go through (Src-R->Mid-S->Dst, C-R->C-R->A), so that the long lists stay quick
CHAIN_LABELS = ["", ":A", ":C", ":Src", ":Dst"]


ARROWS = [("-", "->"), ("<-", "-"), ("-", "-")]


def one_hop_paths(lengths, labels=LABELS):
    for left, right, kind, length, (head, tail) in itertools.product(
        labels, labels, TYPES, lengths, ARROWS
    ):
        yield f"({left}){head}[{kind}{length}]{tail}({right})"


def test_the_nodes(lab, backend, resolver):
    patterns = [f"({labels})" for labels in LABELS]
    patterns += [f"(n{labels})" for labels in LABELS]
    patterns += [f"({labels} {{p: 1}})" for labels in LABELS]
    patterns += [f"({labels} {{zzz: 1}})" for labels in LABELS]
    patterns += [f"({labels} {{z: 1}})" for labels in LABELS]
    patterns += ["(:D)", "(:Mid {n: 1})", "(:Mid {n: 3})", "(:Dst {n: 1})"]

    broken, _, wrong_maybes, exact_trues = compare(patterns, backend, resolver)

    assert broken == {}
    assert exact_trues > 5
    assert (
        wrong_maybes > 0
    )  # {p: 1} on a kind whose nodes have p, but not with that value


def test_the_one_hop_paths(lab, backend, resolver):
    patterns = list(one_hop_paths([""]))

    broken, rejected, _, exact_trues = compare(patterns, backend, resolver)

    assert broken == {}
    assert len(rejected) == 0, rejected[:5]
    assert exact_trues > 100


# A third of the 4,200 patterns there are, the same ones every time (all of them took 13 seconds, a fifth of the
# suite): the unit tests above hold what each kind of pattern means, and this holds the snapshot to the database's
# answers on a spread of them. (Seeded, not every third: the loops have lengths of 3 and 8, and every third
# pattern would always have the same direction.)
SAMPLE = 1400


def test_the_paths_of_variable_length(lab, backend, resolver):
    patterns = random.Random(7).sample(
        list(one_hop_paths(LENGTHS[1:], CHAIN_LABELS)), SAMPLE
    )

    broken, rejected, wrong_maybes, exact_trues = compare(patterns, backend, resolver)

    assert broken == {}
    # what the database cannot parse is no business of the snapshot, but it should be almost nothing
    assert len(rejected) < len(patterns) * 0.05, rejected[:10]
    assert exact_trues > 200
    # the chains of kinds that no real nodes make: the reason a "maybe" exists
    assert wrong_maybes > 0


def test_a_path_through_a_kind_whose_nodes_do_not_connect_is_a_maybe_and_it_is_wrong(
    lab, backend, resolver
):
    # Src-R->Mid and Mid-S->Dst: two different Mid nodes
    for pattern in ("(:Src)-[:R|S*2]->(:Dst)", "(:C)-[:R*2]->(:A)"):
        assert online_path(backend, pattern) is False
        answer = offline_path(resolver, pattern)
        assert (answer.value, answer.reliable) == (True, False)


def test_the_paths_with_variables_and_properties(lab, backend, resolver):
    patterns = []
    for left, kind, right, arrow in itertools.product(
        ["(a)", "(a:A)", "(a:C)", "(a {p: 1})", "(a:A {p: 99})", "(a:A {nope: 1})"],
        ["[r]", "[r:R]", "[r:R {w: 1}]", "[:R {w: 2}]", "[:S {w: 1}]", "[r*2]"],
        ["(b)", "(a)", "(b:B)", "(b:B {p: 2})", "(c:C)"],
        ARROWS,
    ):
        head, tail = arrow
        patterns.append(f"{left}{head}{kind}{tail}{right}")

    broken, _, wrong_maybes, exact_trues = compare(patterns, backend, resolver)

    assert broken == {}
    assert wrong_maybes > 0
    assert exact_trues > 20


def test_a_node_that_loops_to_itself(lab, backend, resolver):
    # C2 has a self loop; C1 has none. The kinds say a C loops, and one does.
    assert online_path(backend, "(a:C)-[:R]->(a)") is True
    assert offline_path(resolver, "(a:C)-[:R]->(a)").value is True
    # no A loops, and the snapshot is sure
    assert online_path(backend, "(a:A)-[:R]->(a)") is False
    assert offline_path(resolver, "(a:A)-[:R]->(a)").value is False


# --------------------------------------------------------------------- label combinations


def combinations_online(backend, pattern):
    try:
        frame, *_ = backend.run(_online.label_combinations(GRAPH, pattern))
    except CypherError:
        return None
    return (
        None
        if frame is None
        else (list(frame.columns), set(map(tuple, frame.to_numpy().tolist())))
    )


def test_the_label_combinations(lab, backend, resolver):
    broken, none_online, rows_checked = {}, 0, 0
    lefts = ["(a)", "(:A)", "(:Extra)", "()", "(:C)", "(a:A)", "(:D)"]
    rels = [
        "[r]",
        "[]",
        "[:R]",
        "[:S|R]",
        "[r:T]",
        "[:NOPE]",
        "[:R*2]",
        "[r*2]",
        "[:R|S*0..]",
    ]
    rights = ["(b)", "(:B)", "(:C)", "()", "(a)"]
    patterns = [
        f"{left}{head}{rel}{tail}{right}"
        for left, rel, right, (head, tail) in itertools.product(
            lefts, rels, rights, ARROWS
        )
    ] + ["(a)", "(a:A)", "(:A)", "()", "(a:Extra)", "(a:Nope)"]
    for pattern in patterns:
        online = combinations_online(backend, pattern)
        answer = resolver.answer(Lookup("label_combinations", GRAPH, (pattern,)))
        frame, *_ = answer.value
        offline = (
            None
            if frame is None
            else (list(frame.columns), set(map(tuple, frame.to_numpy().tolist())))
        )
        if online is None:
            none_online += 1
            if offline is not None:
                broken[pattern] = (online, offline)
            continue
        if offline is None or offline[0] != online[0]:
            broken[pattern] = (online, offline)
            continue
        rows_checked += 1
        if (answer.reliable and offline[1] != online[1]) or (
            not answer.reliable and not online[1] <= offline[1]
        ):
            broken[pattern] = (online, offline)
        elif answer.reliable is False and online[1] == offline[1]:
            pass  # a maybe that turned out right is fine

    assert broken == {}
    assert rows_checked > 500
    assert (
        none_online > 0
    )  # the paths with no variable, which the database rejects as an empty query


# ------------------------------------------------------------------------ names and the rest


def test_the_names_the_graph_does_not_know_match_the_databases(lab, backend, resolver):
    names = [
        "A",
        "B",
        "Extra",
        "Nope",
        "R",
        "S",
        "T",
        "U",
        "NOPE",
        "p",
        "q",
        "w",
        "z",
        "n",
        "zzz",
        "Mid",
        "Src",
    ]
    for kind in ("labels", "relationship_types", "property_keys"):
        online = backend.run(_queries._missing_names(GRAPH, kind, names))
        offline = resolver.answer(Lookup("missing_names", GRAPH, (kind, tuple(names))))
        assert tuple(online) == offline.value, kind
        assert offline.miss is bool(offline.value)


def test_the_properties_of_a_label(lab, backend, resolver):
    for label, kind in itertools.product(
        ["A", "B", "C", "D", "Extra", "Mid", "R", "S", "T", "U", "Nope"],
        ["node", "relationship"],
    ):
        for prop in ["p", "q", "w", "z", "n", "zzz"]:
            try:
                online = backend.run(
                    _queries.property_exists(GRAPH, label, kind, prop)
                )[0]
            except CypherError:
                online = "error"
            try:
                offline = resolver.answer(
                    Lookup("property_exists", GRAPH, (label, kind, prop))
                ).value
            except CypherError:
                offline = "error"
            assert offline == online, (label, kind, prop)


def test_the_indexes(lab, backend, resolver):
    for args in itertools.product(
        ["fulltext", "vector"],
        ["nodes", "relationships"],
        ["A", "B", "R", "Nope"],
        [None, "q", "p", "w", "nope"],
    ):
        online = backend.run(_queries.index_exists(GRAPH, *args))
        assert resolver.answer(Lookup("index_exists", GRAPH, args)).value == online, (
            args
        )


def test_the_labels_that_occur_together(lab, backend, resolver):
    online = backend.run(_online.multilabels(GRAPH))
    offline = resolver.answer(Lookup("multilabels", GRAPH, ())).value

    assert sorted(sorted(s) for s in offline) == sorted(sorted(s) for s in online)
    assert [sorted(s) for s in offline] == [["A", "Extra"]]


# -------------------------------------------------------------------------- the types of a property


def test_the_types_of_the_properties(lab, backend):
    resolver = SnapshotResolver(Snapshot.capture(lab, GRAPH, types=True))
    checked, with_types = 0, 0
    for label, kind, prop, literal_type in itertools.product(
        ["A", "B", "C", "D", "Extra", "Mid", "R", "S", "Nope", ""],
        ["node", "relationship"],
        ["p", "q", "w", "z", "n", "zzz", "Two Words"],
        ["String", "Integer", "Float", "Boolean"],
    ):
        online = backend.run(
            _online.property_types(GRAPH, label, kind, prop, literal_type)
        )
        answer = resolver.answer(
            Lookup("property_types", GRAPH, (label, kind, prop, literal_type))
        )
        assert answer.value == online, (label, kind, prop, literal_type)
        assert answer.reliable
        # a miss is exactly a literal whose type the property has not been seen with
        assert answer.miss is (not fits(literal_type, online))
        checked += 1
        with_types += bool(online)
    assert checked == 560
    # the 7 properties the graph has (A.p, A.q, B.p, Extra.p, D.z, Mid.n, R.w) with each of the 4 literal types:
    # the test is about types and not about nothing
    assert with_types == 28


@pytest.fixture
def mixed(falkordb_client):
    graph = falkordb_client.select_graph("offline_mixed_types")
    graph.query(
        "CREATE (:M {v: 1, w: 'a'}), (:M {v: 'x'}), (:M {v: 2.5, ok: true}), "
        "(:M)-[:L {n: 1}]->(:M), (:M)-[:L {n: 'two'}]->(:M)"
    )
    yield falkordb_client
    graph.delete()


def test_mixed_data_has_every_type_it_has_online_and_offline(mixed):
    online_backend = get_backend(mixed)
    resolver = SnapshotResolver(
        Snapshot.capture(mixed, "offline_mixed_types", types=True)
    )
    for label, kind, prop in [
        ("M", "node", "v"),
        ("M", "node", "w"),
        ("M", "node", "ok"),
        ("L", "relationship", "n"),
    ]:
        args = (label, kind, prop, "String")
        online = online_backend.run(
            _online.property_types("offline_mixed_types", *args)
        )
        offline = resolver.answer(Lookup("property_types", "offline_mixed_types", args))
        assert offline.value == online, args
    assert online_backend.run(
        _online.property_types("offline_mixed_types", "M", "node", "v", "String")
    ) == ("Float", "Integer", "String")


# ----------------------------------------------------------------------------------------------------
# Parity with FalkorDB for names and patterns the parser does not take (no database needed)

UNREADABLE = [
    "(:A {name: '}'})",  # a brace in a string
    "(:A)-[:R {ids:[1]}]->(:B)",  # a bracket in a map, which ends the relationship early
    "(:A)<-->(:B)",  # both arrowheads
    "",  # not a pattern at all
    "garbage",
    "(:A)-[:R]->",  # no second node
    "(:A)-[:R]->(:B)-[:S]->(:C)",  # more than one hop
    "(:A)<-[:R]->(:B)",
]


@pytest.mark.parametrize("pattern", UNREADABLE)
def test_a_pattern_that_is_not_understood_is_a_maybe_that_exists_never_a_no(pattern):
    # the contract of this module: a "maybe" always comes with True, a "no" is always certain
    answer = path(pattern)

    assert answer == Resolution(True, reliable=False, miss=False)


@pytest.mark.parametrize(
    "pattern", ["(:Ünï)", "(a:A)-[:Ünï]->()", "(a:Ünï)", "(:A)-[r:R]->(ü:B)", "(x.ü)"]
)
def test_a_name_with_a_letter_outside_ascii_is_a_syntax_error_like_in_falkordb(pattern):
    with pytest.raises(CypherError) as error:
        path(pattern)

    assert error.value.code == "FalkorDB.ClientError.Statement.SyntaxError"


def test_a_name_with_a_letter_outside_ascii_is_fine_between_back_quotes_or_in_a_string():
    assert path("(:`Ünï`)") == Resolution(
        False, miss=True
    )  # no such label, but no error either
    assert path("(:A {p: 'Ünï'})").value is True  # a string may hold anything
    assert (
        path("(:A) // Ünï\n").reliable is False
    )  # a comment too (and the pattern is not one this reads)


@pytest.mark.parametrize(
    ("label", "kind", "prop"),
    [("Ünï", "node", "p"), ("A", "node", "ü"), ("A", "relationship", "é")],
)
def test_a_property_of_a_name_outside_ascii_is_a_syntax_error(label, kind, prop):
    with pytest.raises(CypherError):
        ask("property_exists", label, kind, prop)


def test_a_property_key_between_back_quotes_counts_as_a_key():
    quoted, plain = path("(:A {`p`: 1})"), path("(:A {p: 1})")

    assert (
        quoted == plain == Resolution(True, reliable=False, miss=False)
    )  # the kinds fit, the value may not
    # a key that A does not have is a certain no, whichever way it is written
    assert (
        path("(:A {`zzz`: 1})") == path("(:A {zzz: 1})") == Resolution(False, miss=True)
    )
    assert path("(:A)-[:R {`w`: 1}]->(:B)") == path("(:A)-[:R {w: 1}]->(:B)")
    assert path("(:A)-[:R {`nope`: 1}]->(:B)").value is False


def test_the_names_of_the_snapshot_are_built_once():
    resolver = SnapshotResolver(LAB)

    for kind in ("labels", "relationship_types", "property_keys"):
        assert resolver._names(kind) is resolver._names(kind)
    assert resolver._names("labels") == frozenset(LAB.labels)


def test_a_path_search_gives_each_match_once():
    # a ring of 12 kinds: a walk of any length from any kind reaches any other, over and over
    ring = Snapshot.from_schema(
        nodes={f"K{i}": [] for i in range(12)},
        relationships=[(f"K{i}", "NEXT", f"K{(i + 1) % 12}") for i in range(12)],
    )
    resolver = SnapshotResolver(ring)
    parsed = _parse("(a)-[*]->(b)")
    assert parsed is not None

    hops = list(resolver._hops(parsed))

    assert len(hops) == len(set(hops))
    assert len(hops) <= 12 * 12 + 12  # every pair of kinds once, and the one-hop edges


def test_an_exact_answer_ends_the_search_before_the_walks_are_tried(monkeypatch):
    resolver = SnapshotResolver(LAB)

    def walks_are_not_needed(*args, **kwargs):
        raise AssertionError("the walks were tried although a one-hop match settles it")

    monkeypatch.setattr(resolver, "_chains", walks_are_not_needed)

    answer = resolver.answer(Lookup("path_exists", "lab", ("(:A)-[*]->(:B)",)))

    assert answer == Resolution(True)  # A-R->B is one hop, and exact


def test_one_walk_is_enough_to_call_a_path_a_maybe(monkeypatch):
    # a ring of kinds has a walk from every kind to every other, so there are many to stop at the first of
    ring = Snapshot.from_schema(
        nodes={f"K{i}": [] for i in range(12)},
        relationships=[(f"K{i}", "NEXT", f"K{(i + 1) % 12}") for i in range(12)],
    )
    resolver = SnapshotResolver(ring)
    steps = []
    original = resolver._chains

    def counting(*args, **kwargs):
        for hop in original(*args, **kwargs):
            steps.append(hop)
            yield hop

    monkeypatch.setattr(resolver, "_chains", counting)

    answer = resolver.answer(Lookup("path_exists", "ring", ("(a)-[:NEXT*2..]->(b)",)))

    assert answer == Resolution(True, reliable=False)
    assert len(steps) == 1  # the first walk settles that it is a maybe
    parsed = _parse("(a)-[:NEXT*2..]->(b)")
    assert parsed is not None
    assert len(list(original(parsed))) > 12  # and there were many


# ----------------------------------------------------------------------------------------------------
# The same, against the database


@pytest.mark.parametrize(
    "pattern",
    [
        "(a:`A` {p: 1})",  # read, but a value is a maybe
        "(:A {p: '}'})",
        "(:A)-[:R {w: [1]}]->(:B)",
        "(:A) // a comment",
    ],
)
def test_a_pattern_the_snapshot_does_not_read_is_never_a_no_where_the_database_says_yes(
    lab, backend, resolver, pattern
):
    online = online_path(backend, pattern)
    offline = offline_path(resolver, pattern)

    # not read: nothing is known, so a maybe with True, whatever the database says (a "yes" is never contradicted)
    assert (offline.value, offline.reliable) == (True, False)
    assert not (online is True and offline.value is False)


@pytest.mark.parametrize("pattern", ["(:Ünï)", "(a:A)-[:Ünï]->()", "(:A)-[r:R]->(ü:B)"])
def test_a_name_outside_ascii_is_a_syntax_error_for_the_database_and_for_the_snapshot(
    lab, backend, resolver, pattern
):
    assert online_path(backend, pattern) is None  # the database rejects it
    with pytest.raises(CypherError):
        offline_path(resolver, pattern)


def test_a_back_quoted_name_outside_ascii_is_not_an_error_for_the_database_nor_for_the_snapshot(
    lab, backend, resolver
):
    assert online_path(backend, "(:`Ünï`)") is False  # no such label, but it parses
    assert offline_path(resolver, "(:`Ünï`)") == Resolution(
        False, miss=True
    )  # the same, and no error


# ----------------------------------------------------------------------------------------------------
# Names between back-quotes are names

QUOTED = SnapshotResolver(
    Snapshot.from_schema(
        nodes={"My Label": ["the x"], "Person": ["name"]},
        relationships=[("Person", "Odd Type", "My Label")],
        graph="q",
    )
)


def quoted(pattern):
    return QUOTED.answer(Lookup("path_exists", "q", (pattern,)))


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("(:`My Label`)", Resolution(True)),
        ("(:`Nope Label`)", Resolution(False, miss=True)),
        ("(a:`Person`)", Resolution(True)),  # back-quotes around a name that needs none
        ("(:Person)-[:`Odd Type`]->(:`My Label`)", Resolution(True)),
        ("(:`My Label`)<-[:`Odd Type`]-(:Person)", Resolution(True)),
        (
            "(:`My Label`)-[:`Odd Type`]->(:Person)",
            Resolution(False, miss=True),
        ),  # the other way round
        ("(:Person)-[:`Nope Type`]->(:`My Label`)", Resolution(False, miss=True)),
        ("(:Person)-[:`Odd Type`|NOPE]->()", Resolution(True)),
    ],
)
def test_a_name_between_back_quotes_is_answered_like_any_other(pattern, expected):
    assert quoted(pattern) == expected


def test_a_property_key_between_back_quotes_is_a_key_of_a_name_between_back_quotes():
    assert quoted("(:`My Label` {`the x`: 1})") == Resolution(True, reliable=False)
    assert quoted("(:`My Label` {`nope`: 1})") == Resolution(False, miss=True)


def test_a_name_between_back_quotes_is_answered_as_the_database_answers(
    lab, backend, resolver
):
    patterns = [
        "(:`A`)",
        "(:`Nope`)",
        "(a:`A`)",
        "(:`A`:`Extra`)",
        "(:`A`)-[:`R`]->(:`B`)",
        "(:`B`)<-[:`R`]-(:`A`)",
        "(:`B`)-[:`R`]->(:`A`)",
        "(:A)-[:`NOPE`]->(:B)",
        "(:`A`)-[:`R`|`S`]->(:`B`)",
        "(:`A`)-[:`R`*2]->(:`B`)",
        "(:`A`)-[:`R`*]->(:`C`)",
        "()-[:`R`]-()",
        "()-[:`T`]->(:`B`)",
    ]

    broken, rejected, _, exact_trues = compare(patterns, backend, resolver)

    assert broken == {} and rejected == []
    assert exact_trues >= 5
    for (
        pattern
    ) in patterns:  # and not one of them is merely a maybe, unless a chain of kinds
        if "*" not in pattern:
            assert offline_path(resolver, pattern).reliable, pattern
