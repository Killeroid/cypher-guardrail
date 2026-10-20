from collections.abc import Generator
from functools import reduce
from typing import Any

import pandas as pd
import regex as re

from cypher_guardrail._internal.core import queries as _queries
from cypher_guardrail._internal.core.io import Concurrently, Lookup, Request
from cypher_guardrail._internal.sources.online import (
    identify_variables_and_construct_query,  # noqa: F401
)
from cypher_guardrail.backend import get_async_backend, get_backend
from cypher_guardrail.issues import WRITE_QUERY_ERROR


def get_paths_after_inference(driver, all_paths_with_vars, database_name=None):
    """Infer the labels and types of the variables in each path, one DataFrame per group of paths.

    Each path gets a DataFrame whose columns are its variables and whose rows are the label (or type)
    combinations that exist in the graph. Paths that share a variable are inner-joined, so that the
    shared variable keeps only the labels that fit all of them; the other DataFrames are returned as they are.

    Args:
        - driver: A ``falkordb.FalkorDB`` client (or FalkorDBLite), a ``falkordb.Graph``, or a cypher-guardrail backend.
        - all_paths_with_vars (list): A list of paths with variables to be inferred.
        - database_name (str | None) :Database to validate the query against. None (default) uses the database configured on the server side.

    Returns:
        - df_list (list): The DataFrames, with the variables as columns and the valid label combinations as rows.
          A path the database rejects gives a DataFrame with its variables and no rows.
    """

    backend = get_backend(driver)
    graph = backend.graph_name(database_name)
    return backend.run(paths_after_inference(graph, all_paths_with_vars))


async def get_paths_after_inference_async(
    driver, all_paths_with_vars, database_name=None
):
    """The async version of :func:`get_paths_after_inference`: ``driver`` is a ``falkordb.asyncio`` client or graph
    (or an ``AsyncFalkorDBBackend``), and the DataFrames are awaited."""
    backend = get_async_backend(driver)
    graph = backend.graph_name(database_name)
    return await backend.run(paths_after_inference(graph, all_paths_with_vars))


def is_write(driver, query, database_name=None):
    """Whether ``query`` can modify the graph: it creates, changes or deletes data, or an index, or calls a procedure
    that writes. The database decides (it refuses such a query in a read-only request), and the query is not run:
    it is only explained. This is the check that ``SyntaxValidator`` makes (``read_only``), on its own. (No data is
    written, but FalkorDB registers the names that a ``MERGE`` mentions when it explains it: a property key it did not
    have is then in the graph's list of keys.)

    Args:
        - driver: A ``falkordb.FalkorDB`` client (or FalkorDBLite), a ``falkordb.Graph``, or a cypher-guardrail backend.
        - query (str): The Cypher query.
        - database_name (str | None): The graph to ask. None (default) uses the one the driver was given.

    Returns:
        - bool: True if the query can write, False if it only reads.

    Raises:
        - GraphNotFoundError: If the graph does not exist (a ``CypherError`` too).
        - CypherError: If the database rejects the query (a syntax error, ...). A query that cannot be read is not one
          known to be safe, so it is not answered with False.
    """
    backend = get_backend(driver)
    graph = backend.graph_name(database_name)
    return backend.run(_is_write(graph, query))


async def is_write_async(driver, query, database_name=None):
    """The async version of :func:`is_write`: ``driver`` is a ``falkordb.asyncio`` client or graph (or an
    ``AsyncFalkorDBBackend``)."""
    backend = get_async_backend(driver)
    graph = backend.graph_name(database_name)
    return await backend.run(_is_write(graph, query))


def _is_write(graph, query) -> Generator[Request, Any, bool]:
    issues = yield from _queries.explain(graph, query, read_only=True)
    return any(issue["code"] == WRITE_QUERY_ERROR for issue in issues)


def _label_combinations(
    graph: str, path: str
) -> Generator[Request, Any, tuple[pd.DataFrame | None, list, list]]:
    """The label and type combinations of one path (see ``_online.label_combinations``)."""
    return (yield Lookup("label_combinations", graph, (path,)))


def paths_after_inference(
    graph: str, all_paths_with_vars: list
) -> Generator[Request, Any, list[pd.DataFrame]]:
    """The logic of :func:`get_paths_after_inference` on the resolved graph name, as a generator (see ``cypher_guardrail._internal.core.io``)."""
    # The paths do not depend on each other, so their queries are asked together (they are joined below, in order)
    combinations = yield Concurrently(
        tuple(_label_combinations(graph, path) for path in all_paths_with_vars)
    )
    df_list = []  # the final DataFrames
    for df_var_labels, node_vars, rel_vars in combinations:
        if df_var_labels is None:
            # No label combination is known for this path: keep its variables, with no rows
            df_list.append(
                pd.DataFrame(columns=list(dict.fromkeys(node_vars + rel_vars)))
            )
            continue

        df_var_labels_columns = set(df_var_labels.columns)

        entered = False  # whether df_var_labels was merged into a DataFrame of df_list

        # Join df_var_labels with every DataFrame of df_list that shares a variable with it
        matched_indexes = {}  # index in df_list of each DataFrame joined with df_var_labels -> the columns they share
        merged_dfs = []  # the results of those joins

        for i, df in enumerate(df_list):
            joinable_columns = sorted(df_var_labels_columns & set(df.columns))
            if joinable_columns:
                inner_join = df_var_labels.merge(df, on=joinable_columns, how="inner")
                merged_dfs.append(inner_join)
                matched_indexes[i] = joinable_columns

        # Combine the joins into one DataFrame
        if merged_dfs:
            merged_df = reduce(
                lambda left, right: left.merge(right, how="inner"), merged_dfs
            )

            # Replace the joined DataFrames (those in matched_indexes) with the result
            for index in sorted(matched_indexes.keys(), reverse=True):
                del df_list[index]

            # Add the joined result
            df_list.append(merged_df)
            entered = True

        if not entered:  # it shares no variable with any DataFrame of df_list
            # so it is kept as it is
            df_list.append(df_var_labels)

    return df_list


def replace_with_labels(path, replacements):
    """Give the bare variables of a path the labels (or types) found for them.

    ``(a)-[r]->(b)`` with ``{"a": "Goal", "r": "HAS_TARGET", "b": ""}`` becomes
    ``(a:Goal)-[r:HAS_TARGET]->(b)``: a variable with an empty value, or not in ``replacements``,
    is left bare.

    Args:
        path (str): The path (or query) in which to replace.
        replacements (dict): Variable -> label or relationship type.

    Returns:
        str: ``path`` with ``(var)`` replaced by ``(var:Label)`` and ``[var]`` by ``[var:TYPE]``.
    """
    # A bare variable inside parentheses (a node) or square brackets (a relationship): group 1 or 2
    pattern = r"\(\s*(\w+)\s*\)|\[\s*(\w+)\s*\]"

    def replacer(match):
        node = match.group(1)  # Captures word inside () nodes
        rel = match.group(2)  # Captures word inside [] rels

        if node and node in replacements:
            value = replacements[node]
            # An empty value means that no label was found (SchemaValidator sets it after inference)
            return f"({node}:{value})" if value else f"({node})"

        if rel and rel in replacements:
            value = replacements[rel]
            return f"[{rel}:{value}]" if value else f"[{rel}]"

        return match.group(0)  # not a variable we know: unchanged

    return re.sub(pattern, replacer, path)
