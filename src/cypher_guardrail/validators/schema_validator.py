import functools
import itertools
import operator
from collections.abc import Generator
from typing import Any, Generic, TypeVar

import regex as re

from cypher_guardrail import patterns
from cypher_guardrail._internal.core import builders as _issues
from cypher_guardrail._internal.core import functions
from cypher_guardrail._internal.core import performance as _performance
from cypher_guardrail._internal.core import queries as _queries
from cypher_guardrail._internal.core.errors import CypherError
from cypher_guardrail._internal.core.io import Concurrently, Request
from cypher_guardrail._internal.core.scopes import restore_variables, separate_variables
from cypher_guardrail._internal.sources import hybrid as _modes
from cypher_guardrail.backend import (
    async_snapshotted,
    get_async_backend,
    get_backend,
    snapshotted,
)
from cypher_guardrail.drivers import AsyncDriver, Driver, Verify
from cypher_guardrail.issues import UNKNOWN_LABEL, UNKNOWN_REL_TYPE, Issue
from cypher_guardrail.snapshot import SnapshotSource
from cypher_guardrail.utils import paths_after_inference as infer_paths
from cypher_guardrail.utils import replace_with_labels

# `(a)<-[r]-(b)` and `(a)-[r]->(b)`, matched on a path whose property maps were masked.
# The relationship is optional so that `<--` and `-->` work too.
_LEFT_POINTING = re.compile(
    r"^(?P<left>\([^()]*\))\s*+<-\s*+(?P<rel>\[[^\[\]]*\])?\s*+-\s*+(?P<right>\([^()]*\))$"
)
_RIGHT_POINTING = re.compile(
    r"^(?P<left>\([^()]*\))\s*+-\s*+(?P<rel>\[[^\[\]]*\])?\s*+->\s*+(?P<right>\([^()]*\))$"
)


def _restored_paths(paths, renames):
    """The paths with the names of the variables they had in the query (two that become the same are one)."""
    return list(dict.fromkeys(restore_variables(paths, renames)))


def _restored_path_map(path_map, renames):
    """The same for a map from each path to the paths as written (what is written has no new names)."""
    restored = restore_variables(path_map, renames)
    return {path: list(dict.fromkeys(written)) for path, written in restored.items()}


def _reverse_direction(path):
    """Flip the arrow of a one-hop path: ``(a)<-[r]-(b)`` becomes ``(a)-[r]->(b)`` and vice versa.

    Property maps are masked first, so a ``-``, ``->`` or ``*`` inside a value cannot be mistaken
    for part of the arrow.

    Returns:
        str | None: The flipped path, or None if ``path`` is not a single directed hop.
    """
    masked, properties = patterns.mask_properties(path.strip())
    if match := _LEFT_POINTING.match(masked):
        flipped = f"{match['left']}-{match['rel'] or ''}->{match['right']}"
    elif match := _RIGHT_POINTING.match(masked):
        flipped = f"{match['left']}<-{match['rel'] or ''}-{match['right']}"
    else:
        return None
    return patterns.unmask_properties(flipped, properties)


def _zero_or_more_hops(path):
    """Replace every ``*X..Y`` in ``path`` with ``*``; None if the path has no variable length.

    A ``*`` inside a property value (``{note:'a*b'}``) is not a variable length and is left alone.
    """
    masked, properties = patterns.mask_properties(path)
    if "*" not in masked:
        return None
    return patterns.unmask_properties(
        patterns.VARIABLE_LENGTH.sub("*", masked), properties
    )


def _score(relationships, nodes, paths, indexed):
    """The score: the average of the shares of the relationships, nodes and paths that exist (a kind that the query
    does not use is None and is left out; a query with none of them scores 1: a query such as MATCH (n)).

    ``indexed`` is the share of the index query procedures that have an index, if the query calls any: they weigh as
    much as everything else in the query together.
    """
    # (the order of the terms, and adding them one by one rather than with ``sum``, which compensates the rounding,
    # are part of the value of the score)
    terms = [share for share in (relationships, nodes, paths) if share is not None]
    weight = 1 / len(terms) if terms else 0
    score = functools.reduce(operator.add, (term * weight for term in terms), 0.0)
    if not terms:
        score = 1
    if indexed is not None:
        score = (score + indexed) / 2
    return score


def _found_share(results, issues):
    """The share of the checks of one kind (node, relationship or path) that found the pattern.

    ``results`` are ``(exists, issues found)`` pairs; the issues are added to ``issues``.
    """
    for _, found in results:
        issues.update({(issue["code"], issue["description"]): issue for issue in found})
    return sum(1 for exists, _ in results if exists) / len(results)


def _without_variables(pattern):
    """Remove the bare variables left after inference: ``(a)-[r:HAS]->(b)`` becomes ``()-[r:HAS]->()``."""
    return re.sub(
        r"\(\s*\w+\s*\)|\[\s*\w+\s*\]",
        lambda m: "()" if m.group(0).startswith("(") else "[]",
        pattern,
    )


def _without_prefix_before_colon(txt):
    """Remove the variable before the colon in nodes and relationships: ``(a:Goal)`` becomes ``(:Goal)``."""
    return re.sub(r"\b\w+:(\w*)", r":\1", txt)


_EMPTY_ELEMENT = re.compile(
    r"""
    (\<\s*-|-|-\s*>)       # group 1: left arrow or dash
    \s*
    ([\(\[])\s*   # group 2: opening bracket ( or [
    ([\)\]])      # group 3: closing bracket ) or ]
    \s*
    (-\s*>|-|<\s*-)        # group 4: right arrow or dash
    """,
    re.VERBOSE,
)


def _with_placeholders(query):
    """Give each empty relationship ``-[]-`` in the query a variable: ``-[var_1]-``, ``-[var_2]-``, ...

    The variable lets the type of the relationship be inferred. An empty node ``()`` between two
    dashes is treated the same way.
    """
    count = itertools.count(1)
    return _EMPTY_ELEMENT.sub(
        lambda m: f"{m[1]}{m[2]}var_{next(count)}{m[3]}{m[4]}", query
    )


def _label_by_variable(matches):
    """``{variable: label}`` of the ``variable:Label`` matches (an element with no variable gets a name of its own)."""
    labelled = (match[0].split(":", 1) for match in matches)
    return {
        key.strip() or f"_empty_{i}": value.strip()
        for i, (key, value) in enumerate(labelled)
    }


def _quoted_rel_paths(query):
    """The relationship types between back-quotes (`My Type`), as paths."""
    _, quoted_types = patterns.quoted_names(query)
    return {"()-[:" + patterns.quote_name(kind) + "]-()" for kind in quoted_types}


def _labelled_elements(query, index_calls):
    """``(elements, node_paths, rel_paths)`` of the labels and types that the query writes.

    ``elements`` maps each variable to its label or type, which gives a variable without one the label that the
    query gives it elsewhere. The labels and types of an index query (``CALL db.idx.fulltext.queryNodes('Label',
    ...)``) are in no pattern: they are added. Those between back-quotes are not in the patterns, which read plain
    names: they are checked to exist, as nodes and relationships, and a path with one is left unchecked.
    """
    nodes_matches = patterns.LABELED_NODE.findall(query)
    rels_matches = patterns.LABELED_REL.findall(query)
    nodes_dict = _label_by_variable(nodes_matches)
    rels_dict = _label_by_variable(rels_matches)
    for index, call in enumerate(index_calls):
        (nodes_dict if call.on == "nodes" else rels_dict)[f"_index_{index}"] = (
            call.label
        )
    quoted_labels, _ = patterns.quoted_names(query)

    # all of the labels the query writes, also for a variable that is used twice (`nodes_dict` keeps one label for each variable)
    node_labels = {match[0].split(":", 1)[1].strip() for match in nodes_matches} | {
        call.label for call in index_calls if call.on == "nodes"
    }
    rel_labels = {match[0].split(":", 1)[1].strip() for match in rels_matches} | {
        call.label for call in index_calls if call.on != "nodes"
    }
    node_paths = sorted(
        {"(:" + label + ")" for label in node_labels}
        | {"(:" + patterns.quote_name(label) + ")" for label in quoted_labels}
    )
    rel_paths = sorted(
        # delete everything after * if exists
        {"()-[:" + re.sub(r"\*.*", "", label) + "]-()" for label in rel_labels}
        | _quoted_rel_paths(query)
    )
    return {**nodes_dict, **rels_dict}, node_paths, rel_paths


def _clean_paths(matches, written, elements):
    """``(paths, to_infer, original_path_map)`` of the paths of the query.

    A path loses its spaces and its ``{properties}``, a variable gets the label that the query gives it elsewhere,
    and the variable of an element that has a label goes. ``to_infer`` are the paths that still have a variable
    with no label (to be inferred from the graph). ``original_path_map`` maps each path to the paths as written in the
    query that give it (several can give the same one); ``written[i]`` is what ``matches[i]`` was written as.
    """
    paths, to_infer, original_path_map = set(), set(), {}
    for index, match in enumerate(matches):
        path = re.sub(r"\s+", "", match)
        without_properties = re.sub(r"\{[^}]*\}", "", path)
        # replace i with i:Indicator
        labelled = replace_with_labels(without_properties, elements)
        edited = _without_prefix_before_colon(labelled)
        # a variable with no label in a node or a relationship
        if re.search(r"\((\w+)\)|\[(\w+)\]", labelled):
            to_infer.add(edited)
        paths.add(edited)
        original_path_map.setdefault(edited, []).append(written[index])
    return paths, to_infer, original_path_map


def _first_labels(frames):
    """``{variable: label or type}`` from the tables that the inference gave ("" when none was found)."""
    inferred = {}
    for frame in frames:
        if frame.empty:
            inferred.update(dict.fromkeys(frame.columns, ""))
        else:
            # Every row is a label combination that exists in the graph, so any of them
            # will do. Take the first alphabetically so the result is reproducible.
            first = frame.sort_values(list(frame.columns)).iloc[0]
            inferred.update({column: first[column] for column in frame.columns})
    return inferred


def _infer(paths, to_infer, inferred, original_path_map):
    """``paths`` and ``original_path_map`` with the paths of ``to_infer`` replaced by themselves with their labels."""
    after = {
        path: _without_prefix_before_colon(replace_with_labels(path, inferred))
        for path in to_infer
    }
    return (
        (paths - to_infer) | set(after.values()),
        {after.get(path, path): written for path, written in original_path_map.items()},
    )


_D = TypeVar(
    "_D"
)  # the kind of driver: a client or graph (or a snapshot), sync or async


class _SchemaValidator(_modes.ModeInfo, Generic[_D]):
    """The logic of SchemaValidator, as generators (see ``cypher_guardrail._internal.core.io``): one copy for the sync and the async validator."""

    def __init__(
        self,
        driver: _D,
        *,
        snapshot: SnapshotSource | None = None,
        verify: Verify | None = None,
        check_unbounded_paths: bool = False,
    ) -> None:
        """
        Initialize the validator with a FalkorDB client.

        Args:
            driver: A ``falkordb.FalkorDB`` client (or FalkorDBLite), or a ``falkordb.Graph``; or a ``Snapshot``, to
                validate without a database.
                ``database_name`` is the graph name (optional when a ``falkordb.Graph`` is given).
            snapshot (Snapshot | None): A snapshot of the graph. With a client it is the hybrid mode: the snapshot answers
                the lookups it can answer for sure, and the database the rest. Given as ``driver`` itself (no client) it is
                the offline mode.
            verify (str | None): Hybrid mode: ``"misses"`` (the default) asks the database before reporting a thing the
                snapshot says is not there, ``"never"`` trusts the snapshot.
            check_unbounded_paths (bool): Also warn about a variable-length path with no upper limit
                (``-[:KNOWS*]->``, ``*2..``) with ``cypher_guardrail.Warning.UnboundedPath``, from the text of the
                query alone (no database needed; ``shortestPath`` is left out). Off by default. A warning does not
                change the score.

        Raises:
            TypeError: If the driver is not a FalkorDB client or graph.
        """
        self.driver = driver
        self.check_unbounded_paths = check_unbounded_paths
        self.backend = self._open_backend(driver, snapshot, verify)

    def _open_backend(self, driver, snapshot, verify):
        """The backend the validator runs on. The async validators override this."""
        return get_backend(driver, snapshot=snapshot, verify=verify)

    def _get_path_exists(
        self, path, database_name
    ) -> Generator[Request, Any, tuple[bool, list]]:
        """Whether the pattern matches anything in the graph, and the warnings about unknown labels and types."""
        graph = self.backend.graph_name(database_name)
        return (yield from _queries.path_exists(graph, path))

    def _check_path_exists(
        self, pattern, original_path_map, database_name=None
    ) -> Generator[Request, Any, tuple[bool, list]]:
        """
        Check if the provided pattern exists in the graph database

        Args:
            pattern (str): A node, a relationship or a one-hop path, e.g. ``(:Goal)``.
            original_path_map (dict | None): For each extracted path, the paths as written in the query. None for a node or relationship.
            database_name (str | None): The graph to validate the query against. None (default) uses the graph the validator was given.

        Returns:
            - bool: True if the pattern exists , False otherwise.
            - metadata (list[dict]): Warnings about unknown labels and relationship types
                ('cypher_guardrail.Warning.UnknownLabel', 'cypher_guardrail.Warning.UnknownRelationshipType'), or about why a path does
                not exist (wrong direction, variable length, unknown path).
                Each dictionary includes:
                - 'code' (str): The error or warning code.
                - 'description' (str): A detailed message describing the issue.
        """
        metadata = []  # the warnings found
        try:
            pattern_exists, notifications = yield from self._get_path_exists(
                pattern, database_name
            )

            # Keep the warnings about unknown labels and relationship types, without duplicates
            for notification in notifications or []:
                if notification["code"] in (
                    UNKNOWN_LABEL,
                    UNKNOWN_REL_TYPE,
                ) and not any(
                    item["code"] == notification["code"]
                    and item["description"] == notification["description"]
                    for item in metadata
                ):
                    metadata.append(notification.copy())

            # An unknown label or type already explains why the pattern does not exist
            if not pattern_exists and not metadata:
                # Every label and type is known, so the pattern simply does not exist. Only a path can be
                # explained (wrong direction, ...); a node or relationship is just reported as missing.
                original_paths = (original_path_map or {}).get(pattern, [])
                yield from self._diagnose_missing_path(
                    pattern, original_paths, database_name, metadata
                )
            return pattern_exists, metadata
        except CypherError:
            # The pattern cannot be executed (syntax or execution error), so it does not exist
            return False, metadata

    def _diagnose_missing_path(
        self, pattern, original_paths, database_name, metadata
    ) -> Generator[Request, Any, None]:
        """
        Explain why a path whose labels and relationship types are all valid does not exist, and append the
        explanations to ``metadata``: one for each original path (see ``_explain_missing_path``).

        Args:
            pattern (str): The extracted path (labels and types only) that does not exist.
            original_paths (list[str]): The paths in the query the pattern was extracted from.
            database_name (str | None): Database to check against.
            metadata (list[dict]): Receives one warning per original path.
        """
        shown = _without_variables(pattern)
        for original in original_paths:
            metadata.append(
                (
                    yield from self._explain_missing_path(
                        pattern, shown, original, database_name
                    )
                )
            )

    def _explain_missing_path(
        self, pattern, shown, original, database_name
    ) -> Generator[Request, Any, Issue]:
        """Why the path ``original`` (read as ``pattern``) does not exist. In order of preference the cause is:

        1. cypher_guardrail.Warning.VariableLengthPath: it exists once the `*X..Y` of the original path is replaced by `*`
        2. cypher_guardrail.Warning.WrongDirectionPath: it exists with the opposite direction
        3. cypher_guardrail.Warning.WrongDirectionVariableLengthPath: it exists with the opposite direction and `*`
        4. cypher_guardrail.Warning.UnknownPath: none of the above
        """
        any_hops = _zero_or_more_hops(original)
        if (
            any_hops is not None
            and (yield from self._get_path_exists(any_hops, database_name))[0]
        ):
            return _issues.variable_length_path(shown, original, any_hops)
        # a path with no arrow has no other way round
        if "->" not in pattern and "<-" not in pattern:
            return _issues.unknown_path(shown, original)
        flipped = _reverse_direction(original)
        # The path with its arrow flipped (labels and types only, as the check above was made). The path
        # without arrows is not enough: over several hops it can mix directions and exist without the
        # path existing either way
        flipped_pattern = _reverse_direction(pattern)
        if (
            flipped is not None
            and flipped_pattern is not None
            and (yield from self._get_path_exists(flipped_pattern, database_name))[0]
        ):
            # It only exists the other way round
            return _issues.wrong_direction_path(shown, original, flipped)
        flipped_any_hops = _zero_or_more_hops(flipped) if flipped else None
        if (
            flipped_any_hops is not None
            and (yield from self._get_path_exists(flipped_any_hops, database_name))[0]
        ):
            return _issues.wrong_direction_variable_length_path(
                shown, original, flipped_any_hops
            )
        return _issues.unknown_path(shown, original)

    def _extract_internal(
        self, query, database_name=None
    ) -> Generator[Request, Any, tuple[list, list, list, dict]]:
        """
        Extract the node labels, relationship types and one-hop paths of the provided query. Any properties mentioned are stripped.

        The strings and comments of the query are hidden from the patterns that read it (what is in them is not
        Cypher), and the paths as written in the query are given back with their strings.

        Args:
            query (str): The Cypher query to validate.
            database_name (str | None): The graph to validate the query against. None (default) uses the graph the validator was given.

        Returns:
            tuple: Four items:
            - the nodes, e.g. ``(:Goal)``;
            - the relationships, e.g. ``()-[:HAS_TARGET]-()``;
            - the one-hop paths, with the labels and types inferred where the query leaves them out;
            - a dict: each extracted path -> the list of paths as written in the query.
        """
        # A name that a WITH dropped, or that a CALL { } does not import, is a new variable when it is declared again:
        # it gets a name of its own while the paths are read, and gets its own back in them
        query, renames = separate_variables(query)
        # The branches of a UNION are separate queries: a variable of one is not the variable of another
        branches = patterns.split_union(query)
        nodes: set[str] = set()
        rels: set[str] = set()
        paths: set[str] = set()
        original_path_map: dict[str, list[str]] = {}
        for branch in branches:
            masked, hidden = patterns.hide_literals(branch)
            (
                branch_nodes,
                branch_rels,
                branch_paths,
                branch_original_paths,
            ) = yield from self._extract_masked(masked, branch, database_name)
            if len(branches) == 1:  # the query as it is: nothing to merge
                return (
                    branch_nodes,
                    branch_rels,
                    _restored_paths(
                        patterns.show_literals(branch_paths, hidden), renames
                    ),
                    _restored_path_map(
                        patterns.show_literals(branch_original_paths, hidden), renames
                    ),
                )
            nodes.update(branch_nodes)
            rels.update(branch_rels)
            paths.update(
                _restored_paths(patterns.show_literals(branch_paths, hidden), renames)
            )
            for path, written in _restored_path_map(
                patterns.show_literals(branch_original_paths, hidden), renames
            ).items():
                original_path_map.setdefault(path, []).extend(written)
        return sorted(nodes), sorted(rels), sorted(paths), original_path_map

    def _extract_masked(
        self, query, original, database_name=None
    ) -> Generator[Request, Any, tuple[list, list, list, dict]]:
        """The work of :meth:`_extract_internal`, on the query with its strings hidden. ``original`` is the query as
        written: an index procedure has its label in a string."""
        index_calls = patterns.index_procedure_calls(original)
        # The paths as written in the query (original paths)
        written = patterns.PATH_ORIGINAL.findall(query, overlapped=True)
        # Replace -- with empty relationship to infer the hidden relationship, and () with (unknown variables)
        query = _with_placeholders(query.replace("--", "-[]-"))
        matches = patterns.PATH_EDITED.findall(query, overlapped=True)
        elements, node_paths, rel_paths = _labelled_elements(query, index_calls)

        if not matches:
            # Without a path a relationship type can only come from an index query procedure
            index_rel_paths = sorted(
                {f"()-[:{call.label}]-()" for call in index_calls if call.on != "nodes"}
                | _quoted_rel_paths(query)
            )
            return node_paths, index_rel_paths, [], {}

        paths, to_infer, original_path_map = _clean_paths(matches, written, elements)
        if to_infer:
            graph = self.backend.graph_name(database_name)
            inferred = _first_labels((yield from infer_paths(graph, sorted(to_infer))))
            paths, original_path_map = _infer(
                paths, to_infer, inferred, original_path_map
            )
        return node_paths, rel_paths, sorted(paths), original_path_map

    @_queries.graph_checked
    def _extract(
        self, query, database_name=None
    ) -> Generator[Request, Any, tuple[list, list, list]]:
        """The logic of ``extract``, as a generator."""
        self.backend.check_database(database_name)
        nodes, rels, paths, _ = yield from self._extract_internal(query, database_name)
        return nodes, rels, [_without_variables(path) for path in paths]

    @_queries.graph_checked
    def _validate(
        self, query, database_name=None
    ) -> Generator[Request, Any, tuple[float, list[Issue]]]:
        """The logic of ``validate``, as a generator."""
        self.backend.check_database(database_name)
        # The node labels, the relationship types and the one-hop paths of the query (with inferred labels and types),
        # and the mapping of each extracted path to the paths as written in the query
        nodes, rels, paths, original_path_map = yield from self._extract_internal(
            query, database_name
        )

        # The nodes, relationships and paths are checked independently of each other, so the checks are asked
        # together. Their results are used below in the order of the query, so nothing else changes.
        results = yield Concurrently(
            tuple(
                [
                    # a node is its own path as written: if it has nothing behind it, the issue names it
                    self._check_path_exists(node, {node: [node]}, database_name)
                    for node in nodes
                ]
                + [self._check_path_exists(rel, None, database_name) for rel in rels]
                + [
                    self._check_path_exists(path, original_path_map, database_name)
                    for path in paths
                ]
            )
        )
        # The distinct issues, in the order they were found (a dict is an ordered set). A description holds everything
        # an issue is about, so two issues with the same code and description are the same issue.
        issues: dict[tuple, Issue] = {}
        node_results = results[: len(nodes)]
        rel_results = results[len(nodes) : len(nodes) + len(rels)]
        path_results = results[len(nodes) + len(rels) :]
        # the issues are added in the order of the query: the nodes, the relationships, the paths
        shares = {
            kind: _found_share(found, issues) if found else None
            for kind, found in (
                ("nodes", node_results),
                ("relationships", rel_results),
                ("paths", path_results),
            )
        }
        # The label of an index query procedure exists, but does an index back the call?
        indexed = yield from self._index_share(query, database_name, issues)
        yield from self._unknown_functions(query, database_name, issues)
        if self.check_unbounded_paths:
            for path in _performance.written_unbounded_paths(query):
                issue = _issues.unbounded_path(path)
                issues[(issue["code"], issue["description"])] = issue

        # The score: the average of the shares of the nodes, relationships and paths that exist (1 when the query has
        # none of them: a query such as MATCH (n))
        score = _score(
            shares["relationships"], shares["nodes"], shares["paths"], indexed
        )
        return score, list(issues.values())

    def _unknown_functions(
        self, query, database_name, issues
    ) -> Generator[Request, Any, None]:
        """Add to ``issues`` the functions with a dotted name (``library.function``) that the query calls and the
        instance does not have. They do not change the score."""
        names = functions.called(query)
        if not names:
            return
        graph = self.backend.graph_name(database_name)
        for name in (yield from _queries.missing_functions(graph, tuple(names))):
            issue = _issues.unknown_function(name)
            issues[(issue["code"], issue["description"])] = issue

    def _index_share(
        self, query, database_name, issues
    ) -> Generator[Request, Any, float | None]:
        """The share of the index query procedures of the query that have an index (None when it calls none).

        The ones that have not are added to ``issues``.
        """
        calls = list(
            dict.fromkeys(
                call._replace(variable=None)
                for call in patterns.index_procedure_calls(query)
            )
        )
        graph = self.backend.graph_name(database_name)
        indexed = 0
        for call in calls:
            if (
                yield from _queries.index_exists(
                    graph, call.index, call.on, call.label, call.attribute
                )
            ):
                indexed += 1
            else:
                issue = _issues.unknown_index(
                    call.index, call.on, call.label, call.attribute
                )
                issues[(issue["code"], issue["description"])] = issue
        return indexed / len(calls) if calls else None


class SchemaValidator(_SchemaValidator[Driver | SnapshotSource]):
    """
    A class to validate Cypher queries against the schema of a graph.

    A query is valid if the node labels, relationship types and one-hop paths it uses exist in the graph
    (with the direction written in the query), and so do the indexes behind the full-text and vector
    index query procedures it calls. Labels and types that the query leaves out are inferred from the graph.
    """

    @snapshotted
    def extract(
        self, query: str, database_name: str | None = None
    ) -> tuple[list[str], list[str], list[str]]:
        """
        Extract the node labels, relationship types and one-hop paths of the provided query. Any properties mentioned are stripped.

        Args:
            query (str): The Cypher query to validate.
            database_name (str | None) :Database to validate the query against. None (default) uses the database configured on the server side.

        Returns:
            Three lists: the nodes, the relationships and the one-hop paths of the query, with properties and variables removed.
        """
        return self.backend.run(self._extract(query, database_name))

    @snapshotted
    def validate(
        self, query: str, database_name: str | None = None
    ) -> tuple[float, list[Issue]]:
        """
        Validate the correctness of the nodes, relationships and paths of the provided query

        Args:
            query (str): The Cypher query to validate.
            database_name (str | None): The graph to validate the query against. None (default) uses the graph the validator was given.

        Returns:
            tuple: ``(score, metadata)``.

            score (float): In [0, 1]. The average of the share of the query's nodes that exist, the share of its
            relationships that exist and the share of its paths that exist (a kind the query does not use is left
            out; a query with none of them scores 1). If the query calls a full-text or vector index procedure
            (``db.idx.fulltext.query...``, ``db.idx.vector.query...``) the score is then averaged with the share
            of those calls that have an index.

            metadata (list[dict]): The problems found, without duplicates. Each dictionary includes:

            - 'code' (str): ``cypher_guardrail.Warning.UnknownLabel``, ``cypher_guardrail.Warning.UnknownRelationshipType``,
              ``cypher_guardrail.Warning.UnknownIndex``, or ``cypher_guardrail.Warning.UnknownPath``, ``cypher_guardrail.Warning.WrongDirectionPath``,
              ``cypher_guardrail.Warning.VariableLengthPath`` or ``cypher_guardrail.Warning.WrongDirectionVariableLengthPath`` for a path that does not exist.
            - 'description' (str): A detailed message describing the issue.

        Raises:
            GraphNotFoundError: If the validation asked the database about a graph that does not exist.
        """
        return self.backend.run(self._validate(query, database_name))


class AsyncSchemaValidator(_SchemaValidator[AsyncDriver | SnapshotSource]):
    """The async version of :class:`SchemaValidator`, for a ``falkordb.asyncio`` client or graph: ``extract`` and ``validate`` are coroutines."""

    def __init__(
        self,
        driver: AsyncDriver | SnapshotSource,
        *,
        max_concurrency: int = 8,
        snapshot: SnapshotSource | None = None,
        verify: Verify | None = None,
        check_unbounded_paths: bool = False,
    ) -> None:
        """
        Initialize the validator with an async FalkorDB client.

        Args:
            driver: A ``falkordb.asyncio.FalkorDB`` client, or one of its graphs; or a ``Snapshot``, to validate
                without a database.
                ``database_name`` is the graph name (optional when a graph is given).
            max_concurrency (int): The most requests in flight at once on this validator's connection.
            snapshot (Snapshot | None): A snapshot of the graph. With a client it is the hybrid mode: the snapshot answers
                the lookups it can answer for sure, and the database the rest. Given as ``driver`` itself (no client) it is
                the offline mode.
            verify (str | None): Hybrid mode: ``"misses"`` (the default) asks the database before reporting a thing the
                snapshot says is not there, ``"never"`` trusts the snapshot.
            check_unbounded_paths (bool): Also warn about a variable-length path with no upper limit
                (``-[:KNOWS*]->``, ``*2..``) with ``cypher_guardrail.Warning.UnboundedPath``, from the text of the
                query alone (no database needed; ``shortestPath`` is left out). Off by default. A warning does not
                change the score.

        Raises:
            TypeError: If the driver is not an async FalkorDB client or graph.
            ValueError: If ``max_concurrency`` is less than 1.
        """
        self._max_concurrency = max_concurrency
        super().__init__(
            driver,
            snapshot=snapshot,
            verify=verify,
            check_unbounded_paths=check_unbounded_paths,
        )

    def _open_backend(self, driver, snapshot, verify):
        return get_async_backend(
            driver, self._max_concurrency, snapshot=snapshot, verify=verify
        )

    @async_snapshotted
    async def extract(
        self, query: str, database_name: str | None = None
    ) -> tuple[list[str], list[str], list[str]]:
        """The async version of :meth:`SchemaValidator.extract`: the same arguments and result."""
        return await self.backend.run(self._extract(query, database_name))

    @async_snapshotted
    async def validate(
        self, query: str, database_name: str | None = None
    ) -> tuple[float, list[Issue]]:
        """The async version of :meth:`SchemaValidator.validate`: the same arguments and result."""
        return await self.backend.run(self._validate(query, database_name))
