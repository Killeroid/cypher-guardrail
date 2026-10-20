"""The reader of the schemas of Neo4j's text2cypher dataset (``tests/accuracy/text2cypher.py``), on small schemas of the
same forms. The dataset is not in the repository."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "accuracy"))

import text2cypher

from cypher_guardrail import PropertiesValidator, SchemaValidator

SECTIONS = """Node properties:
- **Person**
  - `name`: STRING Example: "Keanu"
  - `born`: INTEGER Min: 1950, Max: 2000
- **Movie**
  - `title`: STRING Example: "The Matrix"
Relationship properties:
- **ACTED_IN**
  - `role: STRING` Example: "Neo"
The relationships:
(:Person)-[:ACTED_IN]->(:Movie)
(:Person)-[:DIRECTED]->(:Movie)
(:Ghost)-[:HAUNTS]->(:Movie)
"""

APOC = json.dumps(
    {
        "Person": {
            "type": "node",
            "count": 2,
            "properties": {"name": {"type": "STRING"}, "born": {"type": "INTEGER"}},
            "relationships": {
                "ACTED_IN": {"direction": "out", "labels": ["Movie"], "properties": {}},
                "KNOWS": {"direction": "in", "labels": ["Person"], "properties": {}},
            },
        },
        "Movie": {
            "type": "node",
            "count": 1,
            "properties": {"title": {"type": "STRING"}},
            "relationships": {},
        },
        "ACTED_IN": {
            "type": "relationship",
            "count": 2,
            "properties": {"role": {"type": "STRING"}},
        },
    }
)

ONLY_SOME_NODES = "Graph schema: Relevant node labels and their properties (with datatypes) are:\nTopic {label: STRING}"


@pytest.mark.parametrize("schema", [SECTIONS, APOC], ids=["sections", "apoc json"])
def test_a_schema_is_read_as_what_it_says(schema):
    snapshot = text2cypher.snapshot_of(schema)
    assert snapshot is not None
    right = "MATCH (p:Person)-[r:ACTED_IN]->(m:Movie) WHERE p.born > 1 RETURN p.name, r.role, m.title"
    reversed_ = "MATCH (m:Movie)-[:ACTED_IN]->(p:Person) RETURN p.name"
    wrong_property = "MATCH (p:Person) RETURN p.title"

    assert SchemaValidator(snapshot).validate(right) == (1.0, [])
    assert PropertiesValidator(snapshot).validate(right) == (1.0, [])
    (issue,) = SchemaValidator(snapshot).validate(reversed_)[1]
    assert issue["code"] == "cypher_guardrail.Warning.WrongDirectionPath"
    (issue,) = PropertiesValidator(snapshot).validate(wrong_property)[1]
    assert (issue["kind"], issue["name"], issue["properties"]) == (
        "node",
        "Person",
        ["title"],
    )


def test_a_relationship_property_belongs_to_the_relationship():
    snapshot = text2cypher.snapshot_of(SECTIONS)
    assert snapshot is not None

    assert (
        PropertiesValidator(snapshot).validate(
            "MATCH ()-[r:ACTED_IN]->() RETURN r.role"
        )[0]
        == 1.0
    )
    issues = PropertiesValidator(snapshot).validate("MATCH (p:Person) RETURN p.role")[1]
    assert [(i["kind"], i["name"]) for i in issues] == [("node", "Person")]


def test_an_edge_to_a_label_the_schema_does_not_describe_is_left_out():
    snapshot = text2cypher.snapshot_of(SECTIONS)

    assert snapshot is not None
    assert "Ghost" not in snapshot.labels
    assert "HAUNTS" not in snapshot.relationship_types


@pytest.mark.parametrize("schema", [ONLY_SOME_NODES, "", "{not json", "[]", "{}"])
def test_a_schema_that_is_not_read_gives_none(schema):
    assert text2cypher.snapshot_of(schema) is None
