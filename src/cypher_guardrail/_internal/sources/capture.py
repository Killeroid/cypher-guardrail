"""Capturing a :class:`~cypher_guardrail.snapshot.Snapshot` of a graph.

Written as a logic generator like everything that talks to the database (see ``cypher_guardrail._internal.core.io``), so the sync and
the async ``capture`` share it. Every query is read-only.
"""

from collections.abc import Generator
from typing import Any

from cypher_guardrail import snapshot
from cypher_guardrail._internal.core.errors import CypherError, is_missing_graph
from cypher_guardrail._internal.core.io import Concurrently, Request
from cypher_guardrail._internal.core.queries import execute
from cypher_guardrail._internal.sources import online as _online


def _quote(name: str) -> str:
    """``name`` as a back-quoted identifier, so a label or a type with a space or a symbol in it works.

    (FalkorDB has no way to write a back-quote inside a name, so no name has one.)
    """
    return f"`{name}`"


check_sample = snapshot._check_sample


def _scan(graph: str, query: str) -> Generator[Request, Any, list[dict]]:
    """The rows of a query. A graph that does not exist yet has no data: no rows."""
    try:
        return (yield from execute(graph, query))
    except CypherError as e:
        # FalkorDB creates a graph with its first write (see _online.schema_set)
        if not is_missing_graph(e):
            raise
        return []


def check_types(sample, types):
    if types and sample is not None:
        raise ValueError(
            "types=True needs the whole graph: a sample can miss a type, and a type that was missed would be "
            "reported as a mismatch. Capture with sample=None, or without types."
        )


def _first(rows, column):
    return [row[column] for row in rows]


def _types_of(rows):
    """``{property: [its types]}`` of the rows ``(k, t)`` of a scan that asked for the types."""
    found: dict[str, list[str]] = {}
    for row in rows:
        found.setdefault(row["k"], []).append(row["t"])
    return found


def scan_graph(
    graph: str, sample: int | None, with_types: bool = False
) -> Generator[Request, Any, dict]:
    """The pieces of a snapshot of ``graph``: names and indexes first, then the scans that need the names."""
    limit = "" if sample is None else f" LIMIT {sample}"
    # With the types, the scan that finds the properties asks for the type of each value as well
    node_return = "k, typeof(n[k]) AS t" if with_types else "k"
    rel_return = "k, typeof(r[k]) AS t" if with_types else "k"

    # The whole graph is read by one scan for the label sets and one for the edges. A sample is taken per label and
    # per type instead (a limit on all the nodes would see only the common labels), and those need the names first;
    # the first N nodes of the graph are added to them, which is what finds a node that has no label.
    first_scans = (
        _names(graph),
        _online.indexes(graph),
        _scan(
            graph,
            f"MATCH (n) WITH n{limit} RETURN DISTINCT labels(n) AS labels"
            if sample
            else "MATCH (n) RETURN DISTINCT labels(n) AS labels",
        ),
    ) + (
        ()
        if sample
        else (
            _scan(
                graph,
                "MATCH (a)-[r]->(b) RETURN DISTINCT labels(a) AS start, type(r) AS type, labels(b) AS end",
            ),
        )
    )
    names, index_rows, label_sets, *whole_edges = yield Concurrently(first_scans)
    labels, types, keys = names

    # The properties each label (type) has: one scan per name, all independent
    node_scans = tuple(
        _scan(
            graph,
            f"MATCH (n:{_quote(label)}) "
            + (f"WITH n{limit} " if sample else "")
            + f"UNWIND keys(n) AS k RETURN DISTINCT {node_return}",
        )
        for label in sorted(labels)
    )
    rel_scans = tuple(
        _scan(
            graph,
            f"MATCH ()-[r:{_quote(kind)}]->() "
            + (f"WITH r{limit} " if sample else "")
            + f"UNWIND keys(r) AS k RETURN DISTINCT {rel_return}",
        )
        for kind in sorted(types)
    )
    sampled_label_sets = tuple(
        _scan(
            graph,
            f"MATCH (n:{_quote(label)}) WITH n{limit} RETURN DISTINCT labels(n) AS labels",
        )
        for label in sorted(labels)
        if sample
    )
    sampled_edges = tuple(
        _scan(
            graph,
            f"MATCH (a)-[r:{_quote(kind)}]->(b) WITH a, r, b{limit} "
            "RETURN DISTINCT labels(a) AS start, type(r) AS type, labels(b) AS end",
        )
        for kind in sorted(types)
        if sample
    )
    scans = yield Concurrently(
        node_scans + rel_scans + sampled_label_sets + sampled_edges
    )
    node_rows = scans[: len(node_scans)]
    rel_rows = scans[len(node_scans) : len(node_scans) + len(rel_scans)]
    more = scans[len(node_scans) + len(rel_scans) :]
    more_label_sets = more[: len(sampled_label_sets)]
    edges = (
        whole_edges[0]
        if whole_edges
        else [row for rows in more[len(sampled_label_sets) :] for row in rows]
    )
    label_sets = [row for rows in (label_sets, *more_label_sets) for row in rows]

    found = {
        "labels": labels,
        "relationship_types": types,
        "property_keys": keys,
        "indexes": index_rows,
        "label_sets": list(
            dict.fromkeys(tuple(row) for row in _first(label_sets, "labels"))
        ),
        "edges": [(r["start"], r["type"], r["end"]) for r in edges],
        "node_properties": {
            label: _first(rows, "k")
            for label, rows in zip(sorted(labels), node_rows, strict=True)
        },
        "relationship_properties": {
            kind: _first(rows, "k")
            for kind, rows in zip(sorted(types), rel_rows, strict=True)
        },
    }
    if with_types:
        found["node_value_types"] = {
            label: _types_of(rows)
            for label, rows in zip(sorted(labels), node_rows, strict=True)
        }
        found["relationship_value_types"] = {
            kind: _types_of(rows)
            for kind, rows in zip(sorted(types), rel_rows, strict=True)
        }
    return found


def _names(
    graph: str,
) -> Generator[Request, Any, tuple[frozenset, frozenset, frozenset]]:
    labels, types, keys = yield Concurrently(
        (
            _online.schema_set(graph, "db.labels"),
            _online.schema_set(graph, "db.relationshipTypes"),
            _online.schema_set(graph, "db.propertyKeys"),
        )
    )
    return labels, types, keys


def fingerprint_logic(graph: str) -> Generator[Request, Any, tuple]:
    """What a refresh looks at to see whether the graph has changed: its names, its indexes and how many nodes and
    relationships it has. A few cheap queries, where a capture scans the graph."""
    names, index_rows, nodes, edges = yield Concurrently(
        (
            _names(graph),
            _online.indexes(graph),
            _scan(graph, "MATCH (n) RETURN count(n) AS c"),
            _scan(graph, "MATCH ()-[r]->() RETURN count(r) AS c"),
        )
    )
    labels, types, keys = names
    return (
        tuple(sorted(labels)),
        tuple(sorted(types)),
        tuple(sorted(keys)),
        tuple(sorted(repr(row) for row in index_rows)),
        nodes[0]["c"] if nodes else 0,
        edges[0]["c"] if edges else 0,
    )


def _property_types(found: dict):
    """The ``property_types`` of a snapshot of what was found: None when the types were not asked for."""
    if "node_value_types" not in found:
        return None
    return (
        *(
            (label, "NODE", tuple(attributes.items()))
            for label, attributes in found["node_value_types"].items()
        ),
        *(
            (kind, "RELATIONSHIP", tuple(attributes.items()))
            for kind, attributes in found["relationship_value_types"].items()
        ),
    )


def assemble(graph: str, found: dict, sample: int | None = None) -> "snapshot.Snapshot":
    """The snapshot of what was found.

    The queries do not run at one instant, so a label, a type or a property created between two of them can show
    up in the data without being in the names that were asked first; the names of the data are added to them.
    """
    label_sets = [tuple(s) for s in found["label_sets"]]
    edges = [(tuple(a), kind, tuple(b)) for a, kind, b in found["edges"]]
    node_properties = found["node_properties"]
    relationship_properties = found["relationship_properties"]
    return snapshot.Snapshot(
        graph=graph,
        captured_at=snapshot._now(),
        labels=tuple(
            {
                *found["labels"],
                *(label for s in label_sets for label in s),
                *(label for a, _, b in edges for label in (*a, *b)),
            }
        ),
        relationship_types=tuple(
            {*found["relationship_types"], *(k for _, k, _ in edges)}
        ),
        property_keys=tuple(
            {
                *found["property_keys"],
                *(p for ps in node_properties.values() for p in ps),
                *(p for ps in relationship_properties.values() for p in ps),
            }
        ),
        indexes=tuple(
            (label, entity, tuple(types.items()))
            for label, entity, types in found["indexes"]
        ),
        node_label_sets=tuple(label_sets),
        edges=tuple(edges),
        node_properties=tuple(node_properties.items()),
        relationship_properties=tuple(relationship_properties.items()),
        property_types=_property_types(found),
        sample=sample,
    )
