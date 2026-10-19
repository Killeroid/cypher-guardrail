"""Read the schema of a row of Neo4j's text2cypher dataset (``neo4j/text2cypher-2025v1``) as a ``Snapshot``.

A row has a question, a schema as text and a gold query. The schema comes in a few forms. Two are read here: the one with
the sections ``Node properties``, ``Relationship properties`` and ``The relationships``, and the JSON that
``apoc.meta.schema`` gives. The third (``Graph schema: Relevant node labels ...``) lists some node labels and no
relationships, so a gold query cannot be held to it: it is not read.

``scripts/false_positives.py`` runs the gold queries against these snapshots.
"""

import json
import re
from typing import Any

from cypher_guardrail import Snapshot

_NODE_OR_TYPE = re.compile(r"^- \*\*(.+?)\*\*")
_PROPERTY = re.compile(r"^\s*-\s*`([^`:]+)")
_EDGE = re.compile(r"^\(:(.+?)\)-\[:(.+?)\]->\(:(.+?)\)")


def _from_sections(text: str):
    nodes: dict[str, list[str]] = {}
    relationship_properties: dict[str, list[str]] = {}
    edges: list[tuple[str, str, str]] = []
    section = current = None
    for line in text.splitlines():
        if line.startswith("Node properties"):
            section = "nodes"
        elif line.startswith("Relationship properties"):
            section = "relationship properties"
        elif line.startswith("The relationships"):
            section = "edges"
        elif section == "edges":
            if edge := _EDGE.match(line.strip()):
                edges.append((edge[1], edge[2], edge[3]))
        elif section in ("nodes", "relationship properties"):
            target = nodes if section == "nodes" else relationship_properties
            if heading := _NODE_OR_TYPE.match(line):
                current = heading[1]
                target.setdefault(current, [])
            elif (prop := _PROPERTY.match(line)) and current:
                target[current].append(prop[1])
    return nodes, edges, relationship_properties


def _from_json(text: str):
    nodes: dict[str, list[str]] = {}
    relationship_properties: dict[str, list[str]] = {}
    edges: list[tuple[str, str, str]] = []
    data: dict[str, Any] = json.loads(text)
    for name, entry in data.items():
        if entry.get("type") == "node":
            nodes[name] = list(entry.get("properties", {}))
            for relationship, detail in entry.get("relationships", {}).items():
                if detail.get("direction") == "out":
                    edges += [
                        (name, relationship, end) for end in detail.get("labels", [])
                    ]
        elif entry.get("type") == "relationship":
            relationship_properties[name] = list(entry.get("properties", {}))
    return nodes, edges, relationship_properties


def snapshot_of(schema: str) -> Snapshot | None:
    """The snapshot that ``schema`` describes, or None when it is of a form that is not read (or has no node)."""
    try:
        if "Relationship properties" in schema and "The relationships" in schema:
            nodes, edges, relationship_properties = _from_sections(schema)
        elif schema.lstrip().startswith("{"):
            nodes, edges, relationship_properties = _from_json(schema)
        else:
            return None
    except (ValueError, AttributeError):
        return None
    if not nodes:
        return None
    # an edge to a label that the schema does not describe cannot be written in a snapshot
    edges = [edge for edge in edges if edge[0] in nodes and edge[2] in nodes]
    return Snapshot.from_schema(
        nodes=nodes,
        relationships=edges,
        relationship_properties=relationship_properties,
    )
