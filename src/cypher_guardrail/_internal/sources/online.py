"""Answering the lookups of the logic (``cypher_guardrail._internal.core.io.Lookup``) by asking the database.

Each function here is the *online* answer to one kind of lookup: it takes the graph name and the ``args`` of the
lookup and is a logic generator that yields ``RoQuery`` requests and returns the answer. ``resolve`` picks the
function of a lookup; it is what the online backends give the drivers as their resolver.

The queries are what the validators have always asked FalkorDB. See the ``backend`` module docstring for the
FalkorDB peculiarities they compensate for.
"""

from collections.abc import Generator
from typing import Any

import pandas as pd
import regex as re

from cypher_guardrail._internal.core import udfs
from cypher_guardrail._internal.core.errors import CypherError, is_missing_graph
from cypher_guardrail._internal.core.io import (
    ListUdfs,
    Lookup,
    Recall,
    Remember,
    Request,
    RoQuery,
)
from cypher_guardrail._internal.core.queries import execute, query_to_df
from cypher_guardrail.issues import SYNTAX_ERROR

# the lookup kind "missing_names" is asked about one of these
_NAME_PROCEDURES = {
    "labels": "db.labels",
    "relationship_types": "db.relationshipTypes",
    "property_keys": "db.propertyKeys",
}


def schema_set(graph: str, procedure: str) -> Generator[Request, Any, frozenset]:
    """Names returned by a schema procedure (db.labels, ...). A graph that does not exist yet has an empty schema."""
    key = ("schema_set", graph, procedure)
    known = yield Recall(key)
    if known is not None:
        return known
    try:
        result = yield RoQuery(graph, f"CALL {procedure}()")
    except CypherError as e:
        # FalkorDB creates a graph with its first write: a graph with no data has an empty schema
        if not is_missing_graph(e):
            raise
        names = frozenset()
    else:
        names = frozenset(row[0] for row in result.result_set)
    # Remembered (a failed request is not kept), so a graph with no data is asked about once, not once per lookup
    yield Remember(key, names)
    return names


def indexes(graph: str) -> Generator[Request, Any, tuple]:
    """The graph's indexes as (label or type, 'NODE' or 'RELATIONSHIP', {attribute: index types})."""
    key = ("indexes", graph)
    known = yield Recall(key)
    if known is not None:
        return known
    try:
        result = yield RoQuery(graph, "CALL db.indexes()")
    except CypherError as e:
        # No data yet, so no indexes either (see schema_set)
        if not is_missing_graph(e):
            raise
        found = ()
    else:
        columns = [column[1] for column in result.header]
        label, entity, types = (
            columns.index(name) for name in ("label", "entitytype", "types")
        )
        found = tuple(
            (row[label], row[entity], dict(row[types])) for row in result.result_set
        )
    yield Remember(key, found)
    return found


def missing_names(
    graph: str, kind: str, names: tuple[str, ...]
) -> Generator[Request, Any, tuple[str, ...]]:
    """The ``names`` that the graph does not know, in the order given. ``kind``: 'labels', 'relationship_types' or 'property_keys'."""
    known = yield from schema_set(graph, _NAME_PROCEDURES[kind])
    return tuple(name for name in names if name not in known)


def listed_names(graph: str, kind: str) -> Generator[Request, Any, tuple[str, ...]]:
    """Every name of ``kind`` ('labels', 'relationship_types' or 'property_keys') that the graph knows, sorted."""
    known = yield from schema_set(graph, _NAME_PROCEDURES[kind])
    return tuple(sorted(known))


def index_exists(
    graph: str, kind: str, on: str, label: str, attribute: str | None
) -> Generator[Request, Any, bool]:
    """Whether the graph has an index that a full-text or vector query needs (see ``_queries.index_exists``)."""
    entity = "NODE" if on == "nodes" else "RELATIONSHIP"
    graph_indexes = yield from indexes(graph)
    return any(
        index_label == label
        and index_entity == entity
        and any(
            kind.upper() in index_types
            for name, index_types in types.items()
            if attribute is None or name == attribute
        )
        for index_label, index_entity, types in graph_indexes
    )


def listed_udfs(
    graph: str, wanted: bool = True
) -> Generator[Request, Any, udfs.Libraries | None]:
    """The user-defined functions of the instance: None when they were not asked for, or when the server has no list of them.

    They are not part of ``graph``: the graph only says which connection to ask on.
    """
    if not wanted:
        return None
    try:
        reply = yield ListUdfs(graph)
    except CypherError as error:
        if udfs.is_unsupported(error):
            return None
        raise
    return udfs.parse_list(reply)


def functions_missing(
    graph: str, names: tuple[str, ...]
) -> Generator[Request, Any, tuple[str, ...]]:
    """The functions ``library.function`` that the instance does not have (see ``_queries.missing_functions``)."""
    libraries = yield from listed_udfs(graph)
    if libraries is None:  # it cannot say: nothing is reported
        return ()
    known = {f"{library}.{name}" for library, names_ in libraries for name in names_}
    return tuple(name for name in names if name not in known)


def path_exists(graph: str, path: str) -> Generator[Request, Any, bool]:
    """Whether a pattern (node, relationship or path) matches anything in the graph."""
    rows = yield from execute(graph, f"MATCH {path} RETURN 1 LIMIT 1")
    return bool(rows)


def property_exists(
    graph: str, label: str, kind: str, prop: str
) -> Generator[Request, Any, bool]:
    """Whether any node (``kind`` 'node') or relationship ('relationship') with ``label`` has property ``prop``."""
    pattern = f"(x:{label})" if kind == "node" else f"()-[x:{label}]-()"
    # `(x.prop) IS NOT NULL` is true only for an element that has the property
    rows = yield from execute(
        graph, f"MATCH {pattern} WHERE (x.{prop}) IS NOT NULL RETURN 1 LIMIT 1"
    )
    return bool(rows)


def property_types(
    graph: str, label: str, kind: str, prop: str, _literal_type: str
) -> Generator[Request, Any, tuple[str, ...]]:
    """The types of the values of property ``prop`` of the nodes (``kind`` 'node') or relationships ('relationship')
    with ``label``, as ``typeof`` names them, sorted. ``_literal_type`` is not needed to ask the database.

    ``()`` if the database cannot say (e.g. the graph does not exist): no type is then known, and none is reported.
    """
    pattern = f"(x:`{label}`)" if kind == "node" else f"()-[x:`{label}`]->()"
    try:
        rows = yield from execute(
            graph,
            f"MATCH {pattern} WHERE (x.`{prop}`) IS NOT NULL RETURN DISTINCT typeof(x.`{prop}`) AS t",
        )
    except CypherError as e:
        # A graph that does not exist, or a name the database cannot parse: no value is known. Another failure
        # (a timeout, a full memory) does not say that there is none.
        if e.code != SYNTAX_ERROR and not is_missing_graph(e):
            raise
        return ()
    return tuple(sorted({row["t"] for row in rows}))


def multilabels(graph: str) -> Generator[Request, Any, list]:
    """The label combinations that occur together on a node."""
    query = "MATCH (n) WHERE size(labels(n)) > 1 WITH DISTINCT labels(n) AS labelList RETURN COLLECT(labelList) AS output"
    records = yield from execute(graph, query)
    return records[0]["output"] if records else []


def identify_variables_and_construct_query(pattern):
    """Build the query that finds the labels a path's unlabeled variables can have.

    ``(a)-[r]->(b)`` becomes::

        MATCH (a)-[r]->(b)
        WITH DISTINCT labels(a) AS a_labels, labels(b) AS b_labels, type(r) AS r_type
        UNWIND a_labels AS a
        UNWIND b_labels AS b
        UNWIND [r_type] AS r
        RETURN DISTINCT a, b, r

    so every row is one combination of labels and types that exists in the graph.
    Only variables written bare, as ``(a)`` or ``[r]``, are inferred.

    Args:
        pattern (str): A path, e.g. ``(a)-[r]->(:Label)``.

    Returns:
        tuple[str, list[str], list[str]]: The query ("" if the path has no variable to infer),
        the node variables and the relationship variables, each in order of first appearance.
    """
    # A variable can appear more than once in a pattern, but it must be projected only once
    node_vars = list(dict.fromkeys(re.findall(r"\((\w+)\)", pattern)))
    rel_vars = list(dict.fromkeys(re.findall(r"\[(\w+)\]", pattern)))
    if not node_vars and not rel_vars:  # no variable to infer
        return "", node_vars, rel_vars

    # The labels of the nodes and the types of the relationships, then one row for each of their combinations
    projected = [f"labels({node}) AS {node}_labels" for node in node_vars] + [
        f"type({rel}) AS {rel}_type" for rel in rel_vars
    ]
    unwound = [f"UNWIND {node}_labels AS {node}\n" for node in node_vars] + [
        f"UNWIND [{rel}_type] AS {rel}\n" for rel in rel_vars
    ]
    query = (
        f"MATCH {pattern}\nWITH DISTINCT {', '.join(projected)}\n"
        f"{''.join(unwound)}RETURN DISTINCT {', '.join([*node_vars, *rel_vars])}"
    )
    return query, node_vars, rel_vars


def label_combinations(
    graph: str, path: str
) -> Generator[Request, Any, tuple[pd.DataFrame | None, list, list]]:
    """Ask the database for the label and type combinations of one path.

    Returns:
        The DataFrame (columns: the vars of the path, rows: the valid label combinations) or None if the
        database rejected the inference query, and the node and relationship variables of the path.
    """
    query_w_labels, node_vars, rel_vars = identify_variables_and_construct_query(path)
    try:
        return (yield from query_to_df(graph, query_w_labels)), node_vars, rel_vars
    except CypherError:
        # The inference query was rejected (e.g. FalkorDB.ClientError.Statement.SyntaxError)
        return None, node_vars, rel_vars


_RESOLVERS = {
    "missing_names": missing_names,
    "listed_names": listed_names,
    "index_exists": index_exists,
    "functions_missing": functions_missing,
    "path_exists": path_exists,
    "property_exists": property_exists,
    "property_types": property_types,
    "multilabels": multilabels,
    "label_combinations": label_combinations,
}


def resolve(lookup: Lookup) -> Generator[Request, Any, Any]:
    """The logic generator that answers ``lookup`` by asking the database."""
    try:
        resolver = _RESOLVERS[lookup.kind]
    except KeyError:
        raise ValueError(f"Unknown lookup: {lookup.kind!r}") from None
    return resolver(lookup.graph, *lookup.args)
