"""``Snapshot``: a summary of one graph that can be saved, loaded, written by hand and captured from a database.

The first part needs no database (and runs on every Python version); the second captures the seeded graphs.
"""

import dataclasses
import json
import os
import stat

import pytest

from cypher_guardrail import Snapshot
from cypher_guardrail import snapshot as snapshot_module

# ----------------------------------------------------------------------------------------- the data model


def sample(**changes):
    fields = {
        "graph": "kg",
        "captured_at": "2026-10-19T12:00:00+00:00",
        "labels": ["Goal", "Target"],
        "relationship_types": ["HAS_TARGET"],
        "property_keys": ["code", "title"],
        "indexes": [["Goal", "NODE", {"title": ["FULLTEXT"]}]],
        "node_label_sets": [["Goal"], ["Target"]],
        "edges": [[["Goal"], "HAS_TARGET", ["Target"]]],
        "node_properties": {"Goal": ["code", "title"], "Target": ["code"]},
        "relationship_properties": {"HAS_TARGET": []},
    }
    fields.update(changes)
    return Snapshot.from_dict({"format_version": 1, **fields})


def test_the_order_and_the_duplicates_in_what_is_given_do_not_matter():
    shuffled = sample(
        labels=["Target", "Goal", "Goal"],
        property_keys=["title", "code", "code"],
        node_label_sets=[["Target"], ["Goal"], ["Goal"]],
        node_properties={"Target": ["code"], "Goal": ["title", "code", "code"]},
    )

    assert shuffled == sample()
    assert hash(shuffled) == hash(sample())
    assert shuffled.labels == ("Goal", "Target")
    assert shuffled.node_properties_of("Goal") == ("code", "title")


def test_the_labels_of_a_node_are_a_set():
    shared = {
        "labels": ["Area", "GeoArea", "Goal"],
        "relationship_types": ["R"],
        "node_properties": {},
        "relationship_properties": {},
    }
    one = sample(
        node_label_sets=[["GeoArea", "Area"]],
        edges=[[["GeoArea", "Area"], "R", ["Goal"]]],
        **shared,
    )
    other = sample(
        node_label_sets=[["Area", "GeoArea"]],
        edges=[[["Area", "GeoArea"], "R", ["Goal"]]],
        **shared,
    )

    assert one == other
    assert one.node_label_sets == (("Area", "GeoArea"),)
    assert one.edges == ((("Area", "GeoArea"), "R", ("Goal",)),)


def test_a_snapshot_cannot_be_changed():
    snapshot = sample()

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(snapshot, "labels", ())  # noqa: B010 - frozen: a plain assignment would not even type-check
    assert isinstance(snapshot.labels, tuple)
    assert isinstance(snapshot.edges[0][0], tuple)


def test_what_a_snapshot_says_about_properties_and_indexes():
    snapshot = sample(
        relationship_types=["HAS_TARGET", "OTHER"],
        property_keys=["code", "title", "weight"],
        relationship_properties={"HAS_TARGET": ["weight"], "OTHER": []},
    )

    assert snapshot.node_properties_of("Goal") == ("code", "title")
    assert snapshot.node_properties_of("Nope") == ()
    assert snapshot.relationship_properties_of("HAS_TARGET") == ("weight",)
    assert snapshot.relationship_properties_of("OTHER") == ()
    assert snapshot.relationship_properties_of("Nope") == ()
    assert snapshot.indexes == (("Goal", "NODE", (("title", ("FULLTEXT",)),)),)


def test_to_dict_is_plain_json_and_from_dict_undoes_it():
    snapshot = sample()
    data = snapshot.to_dict()

    assert json.loads(json.dumps(data)) == data
    assert data["format_version"] == 1
    assert data["edges"] == [[["Goal"], "HAS_TARGET", ["Target"]]]
    assert data["indexes"] == [["Goal", "NODE", {"title": ["FULLTEXT"]}]]
    assert data["node_properties"] == {"Goal": ["code", "title"], "Target": ["code"]}
    assert Snapshot.from_dict(data) == snapshot
    assert Snapshot.from_json(snapshot.to_json()) == snapshot


def test_a_snapshot_is_not_sampled_unless_it_says_so():
    snapshot = sample()

    assert snapshot.sample is None
    assert "sample" not in snapshot.to_dict()  # an older version still reads the file


def test_a_sampled_snapshot_keeps_its_sample_through_a_dict_and_a_file(tmp_path):
    snapshot = sample(sample=5)

    assert snapshot.sample == 5
    assert snapshot.to_dict()["sample"] == 5
    assert Snapshot.from_dict(snapshot.to_dict()) == snapshot
    assert Snapshot.from_json(snapshot.to_json()).sample == 5
    snapshot.save(tmp_path / "s.json")
    assert Snapshot.load(tmp_path / "s.json").sample == 5
    assert snapshot != sample()  # it is not the structure of the whole graph


@pytest.mark.parametrize("value", [0, -1, 1.5, "5", True])
def test_a_sample_in_a_snapshot_must_be_a_positive_whole_number(value):
    with pytest.raises(ValueError, match="sample"):
        sample(sample=value)


def test_an_unknown_format_version_is_refused():
    data = sample().to_dict()
    data["format_version"] = 3

    with pytest.raises(ValueError, match="format_version 3"):
        Snapshot.from_dict(data)
    del data["format_version"]
    with pytest.raises(ValueError, match="format_version"):
        Snapshot.from_dict(data)


def test_a_newer_format_is_reported_as_such_even_though_it_has_fields_we_do_not_know():
    data = {**sample().to_dict(), "format_version": 3, "something_new": []}

    with pytest.raises(ValueError, match="format_version 3"):
        Snapshot.from_dict(data)


def test_a_dict_with_a_missing_or_unknown_field_is_refused():
    data = sample().to_dict()

    missing = {key: value for key, value in data.items() if key != "edges"}
    with pytest.raises(ValueError, match="edges"):
        Snapshot.from_dict(missing)
    with pytest.raises(ValueError, match="surprise"):
        Snapshot.from_dict({**data, "surprise": 1})


def test_a_snapshot_whose_parts_do_not_fit_together_is_refused():
    with pytest.raises(ValueError, match="Nope"):
        sample(edges=[[["Nope"], "HAS_TARGET", ["Target"]]])
    with pytest.raises(ValueError, match="MISSING"):
        sample(edges=[[["Goal"], "MISSING", ["Target"]]])
    with pytest.raises(ValueError, match="Nope"):
        sample(node_label_sets=[["Nope"]])
    with pytest.raises(ValueError, match="Nope"):
        sample(node_properties={"Nope": ["code"]})
    with pytest.raises(ValueError, match="secret"):
        sample(node_properties={"Goal": ["secret"]})
    with pytest.raises(ValueError, match="Nope"):
        sample(relationship_properties={"Nope": []})
    with pytest.raises(ValueError, match="SIDEWAYS"):
        sample(indexes=[["Goal", "SIDEWAYS", {"title": ["FULLTEXT"]}]])


def test_save_and_load(tmp_path):
    snapshot = sample()
    path = tmp_path / "kg.snapshot.json"

    snapshot.save(path)

    assert Snapshot.load(path) == snapshot
    assert Snapshot.load(str(path)) == snapshot
    assert json.loads(path.read_text()) == snapshot.to_dict()
    assert os.listdir(tmp_path) == ["kg.snapshot.json"]  # no temporary file is left


def test_saving_replaces_a_file_atomically(tmp_path, monkeypatch):
    path = tmp_path / "kg.snapshot.json"
    sample(graph="old").save(path)

    def interrupted(*_):
        raise OSError("disk gone")

    monkeypatch.setattr(snapshot_module.os, "replace", interrupted)
    with pytest.raises(OSError, match="disk gone"):
        sample(graph="new").save(path)
    monkeypatch.undo()

    assert Snapshot.load(path).graph == "old"  # the old file is intact
    assert os.listdir(tmp_path) == ["kg.snapshot.json"]  # and the temporary one is gone


def test_loading_a_file_that_is_not_a_snapshot_says_so(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("[1, 2]")

    with pytest.raises(ValueError, match="not a snapshot"):
        Snapshot.load(path)
    path.write_text("not json")
    with pytest.raises(ValueError, match="not a snapshot"):
        Snapshot.load(path)


# ------------------------------------------------------------------------------------ written by hand


def test_a_snapshot_written_by_hand():
    snapshot = Snapshot.from_schema(
        nodes={"Goal": ["code", "title"], "Target": ["code"]},
        relationships=[("Goal", "HAS_TARGET", "Target")],
        relationship_properties={"HAS_TARGET": ["weight"]},
        graph="kg",
    )

    assert snapshot.graph == "kg"
    assert snapshot.captured_at is None
    assert snapshot.labels == ("Goal", "Target")
    assert snapshot.relationship_types == ("HAS_TARGET",)
    assert snapshot.property_keys == ("code", "title", "weight")
    assert snapshot.node_label_sets == (("Goal",), ("Target",))
    assert snapshot.edges == ((("Goal",), "HAS_TARGET", ("Target",)),)
    assert snapshot.node_properties_of("Goal") == ("code", "title")
    assert snapshot.relationship_properties_of("HAS_TARGET") == ("weight",)
    assert snapshot.indexes == ()


def test_a_label_written_by_hand_can_be_given_with_others_on_the_same_node():
    snapshot = Snapshot.from_schema(
        nodes={"GeoArea": ["name"], "Area": ["name"], "Nuts1": []},
        relationships=[],
        node_label_sets=[["GeoArea", "Area"]],
    )

    assert snapshot.node_label_sets == (("Area", "GeoArea"), ("Nuts1",))


def test_the_ends_of_a_relationship_written_by_hand_can_have_several_labels():
    snapshot = Snapshot.from_schema(
        nodes={"GeoArea": [], "Area": [], "Nuts1": []},
        relationships=[(["GeoArea", "Area"], "HAS_NUTS1", "Nuts1")],
        node_label_sets=[["GeoArea", "Area"]],
    )

    assert snapshot.edges == ((("Area", "GeoArea"), "HAS_NUTS1", ("Nuts1",)),)


def test_relationships_without_properties_and_indexes_written_by_hand():
    snapshot = Snapshot.from_schema(
        nodes={"Doc": ["title", "embedding"]},
        relationships=[("Doc", "CITES", "Doc")],
        indexes=[("Doc", "NODE", {"title": ["FULLTEXT"], "embedding": ["VECTOR"]})],
    )

    assert snapshot.relationship_properties_of("CITES") == ()
    assert snapshot.indexes == (
        ("Doc", "NODE", (("embedding", ("VECTOR",)), ("title", ("FULLTEXT",)))),
    )


def test_a_relationship_between_labels_that_were_not_declared_is_refused():
    with pytest.raises(ValueError, match="Nope"):
        Snapshot.from_schema(
            nodes={"Goal": []}, relationships=[("Goal", "HAS_TARGET", "Nope")]
        )


def test_the_labels_of_a_node_set_that_were_not_declared_are_refused():
    with pytest.raises(ValueError, match="Nope"):
        Snapshot.from_schema(
            nodes={"Goal": []}, relationships=[], node_label_sets=[["Goal", "Nope"]]
        )


# ------------------------------------------------------------------------------ captured from a database


def captured(client, graph, **options):
    return Snapshot.capture(client, graph, **options)


def test_the_snapshot_of_the_kg_graph(falkordb_client, monkeypatch):
    monkeypatch.setattr(snapshot_module, "_now", lambda: "2026-10-19T12:00:00+00:00")

    snapshot = captured(falkordb_client, "kg")

    # FalkorDB adds a property key to ``db.propertyKeys`` when a query merely mentions it (the other tests of the
    # session do), so the keys of the data are a subset of what it lists; everything else is exact
    keys_of_the_data = ("code", "name", "text", "title", "unit", "weight")
    assert set(keys_of_the_data) <= set(snapshot.property_keys)
    snapshot = dataclasses.replace(snapshot, property_keys=keys_of_the_data)

    assert (
        snapshot
        == Snapshot.from_dict(
            {
                "format_version": 2,  # the server lists UDFs (none here), which is the format with ``udfs``
                "graph": "kg",
                "captured_at": "2026-10-19T12:00:00+00:00",
                "udfs": {},
                "labels": ["Area", "GeoArea", "Goal", "Indicator", "Nuts1", "Target"],
                "relationship_types": ["HAS_INDICATOR", "HAS_NUTS1", "HAS_TARGET"],
                "property_keys": ["code", "name", "text", "title", "unit", "weight"],
                "indexes": [],
                "node_label_sets": [
                    ["Area", "GeoArea"],
                    ["Goal"],
                    ["Indicator"],
                    ["Nuts1"],
                    ["Target"],
                ],
                "edges": [
                    [["Area", "GeoArea"], "HAS_NUTS1", ["Nuts1"]],
                    [["Goal"], "HAS_TARGET", ["Target"]],
                    [["Target"], "HAS_INDICATOR", ["Indicator"]],
                ],
                "node_properties": {
                    "Area": ["code", "name"],
                    "GeoArea": ["code", "name"],
                    "Goal": ["code", "title"],
                    "Indicator": ["code", "unit"],
                    "Nuts1": ["code", "name"],
                    "Target": ["code", "text"],
                },
                "relationship_properties": {
                    "HAS_INDICATOR": ["weight"],
                    "HAS_NUTS1": [],
                    "HAS_TARGET": [],
                },
            }
        )
    )


def test_the_snapshot_of_a_graph_with_indexes(falkordb_client, documents):
    snapshot = captured(falkordb_client, documents)

    assert snapshot.indexes == (
        (
            "CITES",
            "RELATIONSHIP",
            (("emb", ("VECTOR",)), ("note", ("FULLTEXT",))),
        ),
        ("Doc", "NODE", (("embedding", ("VECTOR",)), ("title", ("FULLTEXT",)))),
    )
    assert snapshot.node_properties_of("Doc") == ("embedding", "title")
    assert snapshot.relationship_properties_of("CITES") == ("emb", "note")
    assert snapshot.edges == ((("Doc",), "CITES", ("Doc",)),)


def test_a_graph_that_does_not_exist_gives_an_empty_snapshot(falkordb_client):
    snapshot = captured(falkordb_client, "no_such_graph_for_the_snapshot")

    assert snapshot.graph == "no_such_graph_for_the_snapshot"
    assert snapshot.labels == snapshot.relationship_types == snapshot.edges == ()
    assert snapshot.property_keys == snapshot.indexes == snapshot.node_label_sets == ()
    assert snapshot.node_properties == snapshot.relationship_properties == ()


def test_names_that_need_quoting_are_captured(falkordb_client):
    graph = falkordb_client.select_graph("quoting")
    try:
        graph.query(
            "CREATE (:`Two Words` {`odd key`: 1})-[:`ODD TYPE` {`rel key`: 2}]->(:B)"
        )

        snapshot = captured(falkordb_client, "quoting")

        assert snapshot.node_properties_of("Two Words") == ("odd key",)
        assert snapshot.relationship_properties_of("ODD TYPE") == ("rel key",)
        assert snapshot.edges == ((("Two Words",), "ODD TYPE", ("B",)),)
    finally:
        graph.delete()


def test_a_sample_limits_the_scans(falkordb_client, run_async):
    graph = falkordb_client.select_graph("sampled")
    try:
        graph.query("CREATE (:Mixed {a: 1}), (:Mixed {b: 2})")
        graph.query("CREATE (:P)-[:R {x: 1}]->(:Q), (:P)-[:R {y: 2}]->(:Q)")
        graph.query("CREATE (:P:Extra)-[:S]->(:Q)")

        everything = captured(falkordb_client, "sampled")
        one = captured(falkordb_client, "sampled", sample=1)

        assert everything.node_properties_of("Mixed") == ("a", "b")
        assert everything.relationship_properties_of("R") == ("x", "y")
        assert tuple(s for s in everything.node_label_sets if "Extra" in s) == (
            ("Extra", "P"),
        )
        assert len(everything.edges) == 2
        # Each scan looks at 1 node (or relationship), the first, per label (or type)
        assert one.sample == 1
        assert everything.sample is None
        assert one.node_properties_of("Mixed") == ("a",)
        assert one.relationship_properties_of("R") == ("x",)
        # and so every label and every type is seen: the node that has Extra, and the edge of S
        assert set(one.node_label_sets) == {("Extra", "P"), ("Mixed",), ("P",), ("Q",)}
        assert set(one.edges) == {
            (("P",), "R", ("Q",)),
            (("Extra", "P"), "S", ("Q",)),
        }
        # The names come from the schema procedures, which are not scanned
        assert one.labels == everything.labels
        assert one.relationship_types == everything.relationship_types

        async def ask(client):
            return await Snapshot.capture_async(client, "sampled", sample=1)

        assert dataclasses.replace(
            run_async(ask), captured_at=None
        ) == dataclasses.replace(one, captured_at=None)
    finally:
        graph.delete()


def test_a_sample_keeps_a_rare_label_set_next_to_a_common_label(falkordb_client):
    graph = falkordb_client.select_graph("sampled_rare")
    try:
        graph.query("UNWIND range(1, 300) AS i CREATE (:Big {x: i})")
        graph.query("CREATE (:Multi:Extra {m: 1})")

        snapshot = captured(falkordb_client, "sampled_rare", sample=5)

        assert ("Extra", "Multi") in snapshot.node_label_sets
        assert ("Big",) in snapshot.node_label_sets
    finally:
        graph.delete()


def test_a_sample_keeps_a_rare_relationship_type_next_to_a_common_one(falkordb_client):
    graph = falkordb_client.select_graph("sampled_rare_edges")
    try:
        graph.query("UNWIND range(1, 300) AS i CREATE (:A)-[:COMMON]->(:B)")
        graph.query("CREATE (:C)-[:RARE]->(:D)")

        snapshot = captured(falkordb_client, "sampled_rare_edges", sample=5)

        assert (("C",), "RARE", ("D",)) in snapshot.edges
        assert (("A",), "COMMON", ("B",)) in snapshot.edges
    finally:
        graph.delete()


@pytest.mark.parametrize("sample", [0, -1, 1.5, "10", True])
def test_a_sample_must_be_a_positive_whole_number(falkordb_client, sample):
    with pytest.raises(ValueError, match="sample"):
        captured(falkordb_client, "kg", sample=sample)


def test_capturing_does_not_change_the_graph(falkordb_client):
    graph = falkordb_client.select_graph("kg")

    def state():
        return (
            graph.ro_query("MATCH (n) RETURN count(n)").result_set,
            graph.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set,
            graph.ro_query("CALL db.indexes()").result_set,
        )

    before = state()
    captured(falkordb_client, "kg")
    assert state() == before


def test_a_graph_object_needs_no_name(falkordb_client):
    graph = falkordb_client.select_graph("kg")

    from_graph = Snapshot.capture(graph)

    assert from_graph.graph == "kg"
    assert dataclasses.replace(from_graph, captured_at=None) == dataclasses.replace(
        captured(falkordb_client, "kg"), captured_at=None
    )


def test_a_client_without_a_graph_name_is_an_error(falkordb_client):
    with pytest.raises(ValueError, match="graph name"):
        Snapshot.capture(falkordb_client)


def test_the_async_capture_gives_the_same_snapshot(
    falkordb_client, documents, run_async
):
    async def ask(client):
        return [
            await Snapshot.capture_async(client, name)
            for name in ("kg", "ambiguous", documents, "no_such_graph_for_the_snapshot")
        ]

    for snapshot in run_async(ask):
        expected = captured(falkordb_client, snapshot.graph)
        assert dataclasses.replace(snapshot, captured_at=None) == dataclasses.replace(
            expected, captured_at=None
        )


def test_the_snapshot_says_when_it_was_captured(falkordb_client):
    snapshot = captured(falkordb_client, "kg")

    assert snapshot.captured_at is not None
    assert snapshot.captured_at.endswith("+00:00")  # UTC


def test_a_captured_snapshot_survives_a_save_and_a_load(falkordb_client, tmp_path):
    snapshot = captured(falkordb_client, "kg")

    snapshot.save(tmp_path / "kg.json")

    assert Snapshot.load(tmp_path / "kg.json") == snapshot


def test_names_found_in_the_data_but_not_in_the_names_asked_first_are_added():
    # The queries do not run at one instant: something created between two of them shows up in the data only
    from cypher_guardrail._internal.sources.capture import assemble as _assemble

    found = {
        "labels": frozenset({"A"}),
        "relationship_types": frozenset(),
        "property_keys": frozenset(),
        "indexes": (),
        "label_sets": [["A", "OnlyInASet"]],
        "edges": [(["A"], "NEWTYPE", ["OnlyInAnEdge"])],
        "node_properties": {"A": ["fresh"]},
        "relationship_properties": {"NEWTYPE": ["weight"]},
    }

    snapshot = _assemble("g", found)

    assert snapshot.labels == ("A", "OnlyInASet", "OnlyInAnEdge")
    assert snapshot.relationship_types == ("NEWTYPE",)
    assert snapshot.property_keys == ("fresh", "weight")
    assert snapshot.node_properties_of("A") == ("fresh",)


# ------------------------------------------------------------------------------ the types of the properties


def typed(**changes):
    return sample(
        property_types=[
            ["Goal", "NODE", {"code": ["String"], "title": ["String", "Integer"]}],
            ["HAS_TARGET", "RELATIONSHIP", {}],
        ],
        **changes,
    )


def test_a_snapshot_has_no_types_unless_they_were_given():
    assert sample().property_types is None
    assert sample().has_property_types is False
    assert sample().property_types_of("Goal") == {}
    assert Snapshot().to_dict()["property_types"] is None


def test_what_a_snapshot_says_about_the_types_of_properties():
    snapshot = typed()

    assert snapshot.has_property_types is True
    assert snapshot.property_types_of("Goal") == {
        "code": ("String",),
        "title": ("Integer", "String"),
    }
    assert snapshot.property_types_of("Goal", "NODE") == snapshot.property_types_of(
        "Goal"
    )
    # a label with no entry, an unknown name, and a name of the other kind of thing: nothing is known
    assert snapshot.property_types_of("Target") == {}
    assert snapshot.property_types_of("Nope") == {}
    assert snapshot.property_types_of("Goal", "RELATIONSHIP") == {}
    assert snapshot.property_types_of("HAS_TARGET", "RELATIONSHIP") == {}


def test_the_types_are_sorted_and_without_duplicates_whatever_the_order_given():
    shuffled = sample(
        property_types=[
            ["HAS_TARGET", "RELATIONSHIP", {}],
            [
                "Goal",
                "NODE",
                {"title": ["String", "Integer", "String"], "code": ["String"]},
            ],
        ]
    )

    assert shuffled == typed()
    assert shuffled.property_types == (
        ("Goal", "NODE", (("code", ("String",)), ("title", ("Integer", "String")))),
        ("HAS_TARGET", "RELATIONSHIP", ()),
    )


def test_a_snapshot_with_the_types_survives_json_and_a_file(tmp_path):
    snapshot = typed()

    assert Snapshot.from_json(snapshot.to_json()) == snapshot
    assert Snapshot.from_dict(snapshot.to_dict()) == snapshot
    assert json.loads(snapshot.to_json())["property_types"] == [
        ["Goal", "NODE", {"code": ["String"], "title": ["Integer", "String"]}],
        ["HAS_TARGET", "RELATIONSHIP", {}],
    ]
    snapshot.save(tmp_path / "typed.json")
    assert Snapshot.load(tmp_path / "typed.json") == snapshot


def test_a_snapshot_written_without_the_types_field_is_still_read():
    data = sample().to_dict()
    del data["property_types"]

    assert Snapshot.from_dict(data) == sample()
    # a snapshot that has no types is not the same as one that has none to give
    assert Snapshot.from_dict({**data, "property_types": []}) != sample()
    assert Snapshot.from_dict({**data, "property_types": []}).has_property_types


@pytest.mark.parametrize(
    "property_types",
    [
        [["Nope", "NODE", {"code": ["String"]}]],  # a label that is not in the snapshot
        [["Goal", "NODE", {"nope": ["String"]}]],  # a property key that is not in it
        [["Goal", "RELATIONSHIP", {}]],  # a label given as a relationship type
        [["HAS_TARGET", "NODE", {}]],  # a type given as a label
        [["Goal", "EDGE", {}]],  # neither of the two kinds
        [[1, "NODE", {}]],
        [["Goal", "NODE", {"code": [1]}]],  # a type that is not a name
    ],
)
def test_types_that_do_not_fit_the_rest_of_the_snapshot_are_refused(property_types):
    with pytest.raises((ValueError, TypeError)):
        sample(property_types=property_types)


def test_the_types_of_a_snapshot_written_by_hand():
    snapshot = Snapshot.from_schema(
        nodes={"Goal": ["code", "year"], "Target": ["code"]},
        relationships=[("Goal", "HAS_TARGET", "Target")],
        relationship_properties={"HAS_TARGET": ["weight"]},
        node_property_types={"Goal": {"code": ["String"], "year": ["Integer"]}},
        relationship_property_types={"HAS_TARGET": {"weight": ["Float"]}},
    )

    assert snapshot.property_types_of("Goal") == {
        "code": ("String",),
        "year": ("Integer",),
    }
    assert snapshot.property_types_of("HAS_TARGET", "RELATIONSHIP") == {
        "weight": ("Float",)
    }
    # without the types given, there are none
    assert (
        Snapshot.from_schema(nodes={"Goal": ["code"]}, relationships=[]).property_types
        is None
    )
    # one of the two is enough to say the types are known
    only_nodes = Snapshot.from_schema(
        nodes={"Goal": ["code"]},
        relationships=[],
        node_property_types={"Goal": {"code": ["String"]}},
    )
    assert only_nodes.has_property_types
    assert only_nodes.property_types_of("Goal") == {"code": ("String",)}


def test_the_type_of_a_property_a_label_does_not_have_is_refused_when_written_by_hand():
    with pytest.raises(ValueError, match="property key"):
        Snapshot.from_schema(
            nodes={"Goal": ["code"]},
            relationships=[],
            node_property_types={"Goal": {"year": ["Integer"]}},
        )


# ------------------------------------------------------------------------- capturing the types


def test_capturing_the_types_of_the_kg_graph(falkordb_client):
    snapshot = Snapshot.capture(falkordb_client, "kg", types=True)

    assert snapshot.has_property_types
    assert snapshot.property_types_of("Goal") == {
        "code": ("String",),
        "title": ("String",),
    }
    assert snapshot.property_types_of("HAS_INDICATOR", "RELATIONSHIP") == {
        "weight": ("Integer",)
    }
    # every property the snapshot lists for a label has a type, and no other has
    for label, properties in snapshot.node_properties:
        assert sorted(snapshot.property_types_of(label)) == sorted(properties), label
    for kind, properties in snapshot.relationship_properties:
        assert sorted(snapshot.property_types_of(kind, "RELATIONSHIP")) == sorted(
            properties
        ), kind


def test_capturing_the_types_changes_nothing_else(falkordb_client):
    plain = Snapshot.capture(falkordb_client, "kg")
    typed_snapshot = Snapshot.capture(falkordb_client, "kg", types=True)

    assert plain.property_types is None
    assert dataclasses.replace(
        typed_snapshot, property_types=None, captured_at=plain.captured_at
    ) == dataclasses.replace(plain)


def test_a_property_of_mixed_data_has_all_its_types(falkordb_client):
    graph = falkordb_client.select_graph("mixed_types_for_the_snapshot")
    graph.query(
        "CREATE (:M {v: 1, w: 'a'}), (:M {v: 'x'}), (:M {v: 2.5, flag: true}), "
        "(:M)-[:LINK {n: 1}]->(:M), (:M)-[:LINK {n: 'two'}]->(:M)"
    )
    try:
        snapshot = Snapshot.capture(falkordb_client, graph.name, types=True)
    finally:
        graph.delete()

    assert snapshot.property_types_of("M") == {
        "v": ("Float", "Integer", "String"),
        "w": ("String",),
        "flag": ("Boolean",),
    }
    assert snapshot.property_types_of("LINK", "RELATIONSHIP") == {
        "n": ("Integer", "String")
    }


def test_the_types_of_a_graph_that_does_not_exist_are_none_known_not_unknown(
    falkordb_client,
):
    snapshot = Snapshot.capture(falkordb_client, "no_such_graph_for_types", types=True)

    assert snapshot.has_property_types
    assert snapshot.property_types == ()


@pytest.mark.parametrize("sample_size", [1, 100])
def test_the_types_need_the_whole_graph(falkordb_client, sample_size):
    with pytest.raises(ValueError, match="whole graph"):
        Snapshot.capture(falkordb_client, "kg", sample=sample_size, types=True)


def test_the_async_capture_of_the_types_gives_the_same_snapshot(
    falkordb_client, run_async
):
    async def ask(client):
        return await Snapshot.capture_async(client, "kg", types=True)

    snapshot = run_async(ask)
    expected = Snapshot.capture(falkordb_client, "kg", types=True)

    assert dataclasses.replace(snapshot, captured_at=None) == dataclasses.replace(
        expected, captured_at=None
    )
    assert snapshot.has_property_types

    async def too_much(client):
        return await Snapshot.capture_async(client, "kg", sample=5, types=True)

    with pytest.raises(ValueError, match="whole graph"):
        run_async(too_much)


def test_capturing_the_types_does_not_change_the_graph(falkordb_client):
    graph = falkordb_client.select_graph("kg")
    before = graph.ro_query("MATCH (n) RETURN count(n)").result_set
    Snapshot.capture(falkordb_client, "kg", types=True)

    assert graph.ro_query("MATCH (n) RETURN count(n)").result_set == before


# ------------------------------------------------------------------ files that are corrupt or hostile


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("indexes", [1]),
        ("labels", 5),
        ("labels", [1]),
        ("node_properties", [1]),
        ("node_properties", {"Goal": 5}),
        ("edges", [[1]]),
        ("edges", [5]),
        ("node_label_sets", 5),
        ("property_types", [1]),
        ("property_types", [["Goal", "NODE", 5]]),
        ("graph", 5),
        ("captured_at", 5),
    ],
)
def test_a_snapshot_with_a_field_of_the_wrong_kind_is_a_value_error(field, value):
    data = sample().to_dict()
    data[field] = value

    with pytest.raises(ValueError, match=r"not a (valid )?snapshot"):
        Snapshot.from_dict(data)
    with pytest.raises(ValueError, match=r"not a (valid )?snapshot"):
        Snapshot.from_json(json.dumps(data))


def test_text_nested_too_deep_to_read_is_a_value_error(tmp_path):
    deep = "[" * 200_000

    with pytest.raises(ValueError, match="not a snapshot"):
        Snapshot.from_json(deep)
    (tmp_path / "deep.json").write_text(deep)
    with pytest.raises(ValueError, match="not a snapshot"):
        Snapshot.load(tmp_path / "deep.json")


# ------------------------------------------------------------- the types must be of the label's own properties


def test_a_type_for_a_property_the_label_does_not_have_is_refused():
    with pytest.raises(ValueError, match="does not have"):
        sample(
            node_properties={"Goal": ["code"], "Target": ["code", "title"]},
            property_types=[
                ["Goal", "NODE", {"title": ["String"]}]
            ],  # title is Target's
        )


def test_a_type_for_a_property_the_relationship_type_does_not_have_is_refused():
    with pytest.raises(ValueError, match="does not have"):
        sample(
            relationship_properties={"HAS_TARGET": ["weight"]},
            property_keys=["code", "title", "weight", "other"],
            property_types=[["HAS_TARGET", "RELATIONSHIP", {"other": ["Float"]}]],
        )


def test_written_by_hand_a_type_for_another_labels_property_is_refused():
    with pytest.raises(ValueError, match="does not have"):
        Snapshot.from_schema(
            nodes={"A": ["x"], "B": ["y"]},
            relationships=[],
            node_property_types={"A": {"y": ["String"]}},  # y is B's
        )


# ------------------------------------------------------------------------------------ the file save writes


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_saving_keeps_the_mode_of_the_file_it_replaces(tmp_path):
    path = tmp_path / "shared.json"
    path.write_text("old")
    os.chmod(
        path, 0o640
    )  # not what a new file gets under the usual umask (0o644), so it shows the mode was kept

    sample().save(path)

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640


@pytest.mark.skipif(os.name != "posix", reason="file modes")
@pytest.mark.parametrize(
    ("umask", "mode"), [(0o022, 0o644), (0o077, 0o600), (0o002, 0o664)]
)
def test_a_new_file_gets_the_mode_an_ordinary_file_would(tmp_path, umask, mode):
    previous = os.umask(umask)
    try:
        sample().save(tmp_path / "new.json")
    finally:
        os.umask(previous)

    assert stat.S_IMODE(os.stat(tmp_path / "new.json").st_mode) == mode


@pytest.mark.skipif(os.name != "posix", reason="a directory is synced on POSIX")
def test_saving_syncs_the_file_and_then_its_directory(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(
        os, "fsync", lambda fd: synced.append(os.fstat(fd).st_mode) or real(fd)
    )

    sample().save(tmp_path / "s.json")

    kinds = ["directory" if stat.S_ISDIR(mode) else "file" for mode in synced]
    assert kinds == ["file", "directory"]
