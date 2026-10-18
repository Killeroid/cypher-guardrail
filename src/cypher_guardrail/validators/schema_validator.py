from collections.abc import Generator
from typing import Any

import regex as re

from cypher_guardrail import patterns
from cypher_guardrail._internal.core import builders as _issues
from cypher_guardrail._internal.core import queries as _queries
from cypher_guardrail._internal.core.io import Concurrently, Request
from cypher_guardrail._internal.core.scopes import restore_variables, separate_variables
from cypher_guardrail._internal.sources import hybrid as _modes
from cypher_guardrail.backend import (
    UNKNOWN_LABEL,
    UNKNOWN_REL_TYPE,
    CypherError,
    async_snapshotted,
    get_async_backend,
    get_backend,
    snapshotted,
)
from cypher_guardrail.issues import Issue
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


class _SchemaValidator(_modes.ModeInfo):
    """The logic of SchemaValidator, as generators (see ``cypher_guardrail._internal.core.io``): one copy for the sync and the async validator."""

    def __init__(self, driver, *, snapshot=None, verify=None):
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

        Raises:
            TypeError: If the driver is not a FalkorDB client or graph.
        """
        self.driver = driver
        self.backend = self._open_backend(driver, snapshot, verify)

    def _open_backend(self, driver, snapshot, verify):
        """The backend the validator runs on. The async validators override this."""
        return get_backend(driver, snapshot=snapshot, verify=verify)

    def __get_path_exists(
        self, path, database_name
    ) -> Generator[Request, Any, tuple[bool, list]]:
        """Whether the pattern matches anything in the graph, and the warnings about unknown labels and types."""
        graph = self.backend.graph_name(database_name)
        return (yield from _queries.path_exists(graph, path))

    def __check_path_exists(
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
            pattern_exists, notifications = yield from self.__get_path_exists(
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
                yield from self.__diagnose_missing_path(
                    pattern, original_paths, database_name, metadata
                )
            return pattern_exists, metadata
        except CypherError:
            # The pattern cannot be executed (syntax or execution error), so it does not exist
            return False, metadata

    def __diagnose_missing_path(
        self, pattern, original_paths, database_name, metadata
    ) -> Generator[Request, Any, None]:
        """
        Explain why a path whose labels and relationship types are all valid does not exist, and append the
        explanations to ``metadata``. In order of preference the cause is:

        1. cypher_guardrail.Warning.VariableLengthPath: it exists once the `*X..Y` of the original path is replaced by `*`
        2. cypher_guardrail.Warning.WrongDirectionPath: it exists with the opposite direction
        3. cypher_guardrail.Warning.WrongDirectionVariableLengthPath: it exists with the opposite direction and `*`
        4. cypher_guardrail.Warning.UnknownPath: none of the above

        Args:
            pattern (str): The extracted path (labels and types only) that does not exist.
            original_paths (list[str]): The paths in the query the pattern was extracted from.
            database_name (str | None): Database to check against.
            metadata (list[dict]): Receives one warning per original path.
        """
        shown = self.__replace_variables_with_empty_string(pattern)
        # The same path without its arrows, or '' if the path has no direction
        undirected = (
            pattern.replace("->", "-").replace("<-", "-")
            if "->" in pattern or "<-" in pattern
            else ""
        )

        for original in original_paths:
            unknown = _issues.unknown_path(shown, original)
            any_hops = _zero_or_more_hops(original)

            if (
                any_hops is not None
                and (yield from self.__get_path_exists(any_hops, database_name))[0]
            ):
                metadata.append(_issues.variable_length_path(shown, original, any_hops))
            elif not undirected:
                metadata.append(unknown)
            elif (yield from self.__get_path_exists(undirected, database_name))[0]:
                # It only exists the other way round
                flipped = _reverse_direction(original)
                if flipped is None:
                    metadata.append(unknown)
                else:
                    metadata.append(
                        _issues.wrong_direction_path(shown, original, flipped)
                    )
            else:
                flipped = _reverse_direction(original)
                flipped_any_hops = _zero_or_more_hops(flipped) if flipped else None
                if (
                    flipped_any_hops is not None
                    and (
                        yield from self.__get_path_exists(
                            flipped_any_hops, database_name
                        )
                    )[0]
                ):
                    metadata.append(
                        _issues.wrong_direction_variable_length_path(
                            shown, original, flipped_any_hops
                        )
                    )
                else:
                    metadata.append(unknown)

    def __extract_internal(
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
            ) = yield from self.__extract_masked(masked, branch, database_name)
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

    def __extract_masked(
        self, query, original, database_name=None
    ) -> Generator[Request, Any, tuple[list, list, list, dict]]:
        """The work of :meth:`__extract_internal`, on the query with its strings hidden. ``original`` is the query as
        written: an index procedure has its label in a string."""
        # The extracted path -> the paths as written in the query that give it
        original_path_map = {}
        # A path that needs inference -> the same path after inference
        inference_path_map = {}

        ################### Original paths ##################################
        # Extract the paths as it is in the query (original paths)
        matches_original = patterns.PATH_ORIGINAL.findall(query, overlapped=True)

        ################## Edited paths #####################################
        # Replace -- with empty relationship to infer the hidden relationship
        query = query.replace("--", "-[]-")

        # Replace () with (unknown variables)
        query = self.__replace_bracket_placeholders(query)

        # Extract paths
        matches = patterns.PATH_EDITED.findall(query, overlapped=True)

        # Extract the nodes and relationships that have a label or type (even when they also have properties)
        nodes_matches = patterns.LABELED_NODE.findall(query)
        rels_matches = patterns.LABELED_REL.findall(query)

        # Dict of key the variables in query and value the label of the rel or node
        nodes_dict = {
            key.strip() or f"_empty_{i}": value.strip()
            for i, (key, value) in enumerate(
                match[0].split(":", 1) for match in nodes_matches
            )
        }
        rels_dict = {
            key.strip() or f"_empty_{i}": value.strip()
            for i, (key, value) in enumerate(
                match[0].split(":", 1) for match in rels_matches
            )
        }
        # The label or type of an index query (CALL db.idx.fulltext.queryNodes('Label', ...)) is in no pattern
        index_calls = patterns.index_procedure_calls(original)
        for index, call in enumerate(index_calls):
            (nodes_dict if call.on == "nodes" else rels_dict)[f"_index_{index}"] = (
                call.label
            )
        elements_dict = {**nodes_dict, **rels_dict}

        # The labels and types between back-quotes (`My Label`) are not in the patterns above, which read plain names:
        # they are checked to exist, as nodes and relationships, and a path with one is left unchecked
        quoted_labels, quoted_types = patterns.quoted_names(query)

        # The nodes labels: all of those the query writes, also for a variable that is used twice (`nodes_dict` keeps one
        # label for each variable, which is what gives a variable without a label its label)
        node_label_values = [
            match[0].split(":", 1)[1].strip() for match in nodes_matches
        ] + [call.label for call in index_calls if call.on == "nodes"]
        node_labels = sorted(set(node_label_values))
        node_paths = sorted(
            {"(:" + node_label + ")" for node_label in node_labels}
            | {"(:" + patterns.quote_name(label) + ")" for label in quoted_labels}
        )

        # The rels labels
        rel_label_values = [
            match[0].split(":", 1)[1].strip() for match in rels_matches
        ] + [call.label for call in index_calls if call.on != "nodes"]
        rel_labels = sorted(set(rel_label_values))
        # Delete everything after * if exists
        rel_paths = sorted(
            {
                "()-[:" + re.sub(r"\*.*", "", rel_label) + "]-()"
                for rel_label in rel_labels
            }
            | {"()-[:" + patterns.quote_name(kind) + "]-()" for kind in quoted_types}
        )

        # If we have at least one path in the cypher query
        if matches:
            # A set containing the paths for inference
            inference_paths, paths = set(), set()

            # Stage 1: Give variables the labels the query gives them elsewhere, remove {properties} and find the paths to infer
            for index, match in enumerate(matches):
                # Step 1: Delete extra spaces
                path = re.sub(r"\s+", "", match)

                # Step 2: Delete {} properties inside nodes and rels
                match_wt_prop = re.sub(r"\{[^}]*\}", "", path)

                # Step 3: Add labels to variables if specified in another match clause
                path_with_node_rels_labels = replace_with_labels(
                    match_wt_prop, elements_dict
                )  # replace i to i:Indicator

                # Step 4: Remove prefix before colon to the edited path
                edited_path = self.__remove_prefix_before_colon(
                    path_with_node_rels_labels
                )

                # Step 5: Inference - Find variables in relationships or nodes
                if any(
                    filter(
                        None,
                        re.findall(r"\((\w+)\)|\[(\w+)\]", path_with_node_rels_labels),
                    )
                ):
                    # add to inference paths
                    inference_paths.add(edited_path)
                    # keep a mapping before and after inference
                    inference_path_map[edited_path] = ""

                # Step 6: Add the edited path to paths
                paths.add(edited_path)

                # Step 7: Map the edited path to the original paths it comes from (several can give the same one)
                original_path_map.setdefault(edited_path, []).append(
                    matches_original[index]
                )

                # Step 8: The paths that need no inference
                not_inference_paths = paths - inference_paths

            # Stage 2: Inference time!
            if inference_paths:
                # Copy not inference paths to combine them
                paths_after_inference = not_inference_paths.copy()
                # variable -> its inferred label or type ("" if none was found)
                inference_dict = {}
                graph = self.backend.graph_name(database_name)
                for df in (yield from infer_paths(graph, sorted(inference_paths))):
                    if df.empty:
                        for col in df.columns:
                            inference_dict[col] = ""
                    else:
                        # Every row is a label combination that exists in the graph, so any of them
                        # will do. Take the first alphabetically so the result is reproducible.
                        first_combination = df.sort_values(list(df.columns)).iloc[0]
                        for col in df.columns:
                            inference_dict[col] = first_combination[col]
                # Loop over the inference paths to replace with labels
                for inference_match in inference_paths:
                    inferenced_path = self.__remove_prefix_before_colon(
                        replace_with_labels(inference_match, inference_dict)
                    )
                    paths_after_inference.add(inferenced_path)
                    # update the mapping dict before and after inference of the path
                    inference_path_map[inference_match] = inferenced_path

                # Update original_path_map to map the original path to the inferenced path
                updated_original_path_map = {}
                for modified_path, original_paths in original_path_map.items():
                    # If the edited/modified path is a path that needs to be inferenced then:
                    if modified_path in inference_path_map:
                        # get the modified inferenced path as key and value the original paths
                        updated_original_path_map[inference_path_map[modified_path]] = (
                            original_paths
                        )
                    else:
                        # otherwise, add as key the modified path and value the original paths
                        updated_original_path_map[modified_path] = original_paths

                # Replace the old mapping with the updated one
                original_path_map = updated_original_path_map
                return (
                    node_paths,
                    rel_paths,
                    sorted(paths_after_inference),
                    original_path_map,
                )
            return node_paths, rel_paths, sorted(paths), original_path_map
        else:
            # Without a path a relationship type can only come from an index query procedure
            index_rel_paths = sorted(
                {f"()-[:{call.label}]-()" for call in index_calls if call.on != "nodes"}
                | {
                    "()-[:" + patterns.quote_name(kind) + "]-()"
                    for kind in quoted_types
                }
            )
            return node_paths, index_rel_paths, [], original_path_map

    def _extract(
        self, query, database_name=None
    ) -> Generator[Request, Any, tuple[list, list, list]]:
        """The logic of ``extract``, as a generator."""
        self.backend.check_database(database_name)
        nodes, rels, paths, _ = yield from self.__extract_internal(query, database_name)
        paths = [self.__replace_variables_with_empty_string(path) for path in paths]
        return nodes, rels, paths

    def _validate(
        self, query, database_name=None
    ) -> Generator[Request, Any, tuple[float, list[Issue]]]:
        """The logic of ``validate``, as a generator."""
        self.backend.check_database(database_name)
        # The node labels, the relationship types and the one-hop paths of the query (with inferred labels and types),
        # and the mapping of each extracted path to the paths as written in the query
        nodes, rels, paths, original_path_map = yield from self.__extract_internal(
            query, database_name
        )

        # The distinct issues, in the order they were found (a dict is an ordered set). A description holds everything
        # an issue is about, so two issues with the same code and description are the same issue.
        metadata = {}

        # The nodes, relationships and paths are checked independently of each other, so the checks are asked
        # together. Their results are used below in the order of the query, so nothing else changes.
        results = yield Concurrently(
            tuple(
                [
                    # a node is its own path as written: if it has nothing behind it, the issue names it
                    self.__check_path_exists(node, {node: [node]}, database_name)
                    for node in nodes
                ]
                + [self.__check_path_exists(rel, None, database_name) for rel in rels]
                + [
                    self.__check_path_exists(path, original_path_map, database_name)
                    for path in paths
                ]
            )
        )
        node_results = results[: len(nodes)]
        rel_results = results[len(nodes) : len(nodes) + len(rels)]
        path_results = results[len(nodes) + len(rels) :]

        if nodes:
            common_nodes = 0
            for node_exists, node_notifications in node_results:
                common_nodes += int(
                    node_exists
                )  # Convert boolean to integer and sum it
                if node_notifications:
                    # cypher_guardrail.Warning.UnknownLabel
                    metadata.update(
                        {(n["code"], n["description"]): n for n in node_notifications}
                    )
        if rels:
            common_rels = 0
            for rel_exists, rel_notifications in rel_results:
                common_rels += int(rel_exists)
                if rel_notifications:
                    # cypher_guardrail.Warning.UnknownRelationshipType
                    metadata.update(
                        {(n["code"], n["description"]): n for n in rel_notifications}
                    )
        if paths:
            common_paths = 0
            for path_exists, path_notifications in path_results:
                common_paths += int(path_exists)
                if path_notifications:
                    # cypher_guardrail.Warning.UnknownLabel
                    # cypher_guardrail.Warning.UnknownRelationshipType
                    metadata.update(
                        {(n["code"], n["description"]): n for n in path_notifications}
                    )

        # The label of an index query procedure exists, but does an index back the call?
        index_calls = list(
            dict.fromkeys(
                call._replace(variable=None)
                for call in patterns.index_procedure_calls(query)
            )
        )
        indexed = 0
        graph = self.backend.graph_name(database_name)
        for call in index_calls:
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
                metadata[(issue["code"], issue["description"])] = issue

        # Convert to list
        metadata = list(metadata.values())
        # The score: the average of the shares of the nodes, relationships and paths that exist
        if paths:
            if nodes:
                if rels:
                    score = (
                        1 / 3 * ((common_rels) / len(rels))
                        + 1 / 3 * ((common_nodes) / len(nodes))
                        + 1 / 3 * ((common_paths) / len(paths))
                    )

                else:
                    score = 0.5 * ((common_nodes) / len(nodes)) + 0.5 * (
                        (common_paths) / len(paths)
                    )

            else:
                if rels:
                    score = 0.5 * ((common_rels) / len(rels)) + 0.5 * (
                        (common_paths) / len(paths)
                    )
                else:
                    score = (common_paths) / len(paths)
        else:
            # No paths: the labels, and the relationship types of index query procedures
            parts = []
            if nodes:
                parts.append(common_nodes / len(nodes))
            if rels:
                parts.append(common_rels / len(rels))
            # nothing to check: a query such as MATCH (n)
            score = sum(parts) / len(parts) if parts else 1

        if index_calls:
            # The index calls weigh as much as everything else in the query together
            score = (score + indexed / len(index_calls)) / 2

        return score, metadata

    @staticmethod
    def __remove_prefix_before_colon(txt):
        """Remove the variable before the colon in nodes and relationships: ``(a:Goal)`` becomes ``(:Goal)``.

        Args:
            txt (str): A path or pattern.

        Returns:
            str: ``txt`` without the variables that precede a label or type.
        """
        return re.sub(r"\b\w+:(\w*)", r":\1", txt)

    @staticmethod
    def __replace_bracket_placeholders(query):
        """Give each empty relationship ``-[]-`` in the query a variable: ``-[var_1]-``, ``-[var_2]-``, ...

        The variable lets the type of the relationship be inferred. An empty node ``()`` between two
        dashes is treated the same way.
        """
        counter = [0]  # a list, so that the inner function can change it

        pattern = re.compile(
            r"""
            (\<\s*-|-|-\s*>)       # group 1: left arrow or dash
            \s*           # optional spaces
            ([\(\[])\s*   # group 2: opening bracket ( or [
            \s*           # optional spaces
            ([\)\]])      # group 3: closing bracket ) or ]
            \s*           # optional spaces
            (-\s*>|-|<\s*-)        # group 4: right arrow or dash
        """,
            re.VERBOSE,
        )

        def replacer(match):
            counter[0] += 1
            left = match.group(1)
            open_bracket = match.group(2)
            close_bracket = match.group(3)
            right = match.group(4)
            return f"{left}{open_bracket}var_{counter[0]}{close_bracket}{right}"

        return pattern.sub(replacer, query)

    @staticmethod
    def __replace_variables_with_empty_string(pattern):
        """Remove the bare variables left after inference: ``(a)-[r:HAS]->(b)`` becomes ``()-[r:HAS]->()``."""
        return re.sub(
            r"\(\s*\w+\s*\)|\[\s*\w+\s*\]",
            lambda m: "()" if m.group(0).startswith("(") else "[]",
            pattern,
        )


class SchemaValidator(_SchemaValidator):
    """
    A class to validate Cypher queries against the schema of a graph.

    A query is valid if the node labels, relationship types and one-hop paths it uses exist in the graph
    (with the direction written in the query), and so do the indexes behind the full-text and vector
    index query procedures it calls. Labels and types that the query leaves out are inferred from the graph.
    """

    @snapshotted
    def extract(self, query, database_name=None):
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
    def validate(self, query, database_name=None):
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
        """
        return self.backend.run(self._validate(query, database_name))


class AsyncSchemaValidator(_SchemaValidator):
    """The async version of :class:`SchemaValidator`, for a ``falkordb.asyncio`` client or graph: ``extract`` and ``validate`` are coroutines."""

    def __init__(self, driver, *, max_concurrency=8, snapshot=None, verify=None):
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

        Raises:
            TypeError: If the driver is not an async FalkorDB client or graph.
            ValueError: If ``max_concurrency`` is less than 1.
        """
        self._max_concurrency = max_concurrency
        super().__init__(driver, snapshot=snapshot, verify=verify)

    def _open_backend(self, driver, snapshot, verify):
        return get_async_backend(
            driver, self._max_concurrency, snapshot=snapshot, verify=verify
        )

    @async_snapshotted
    async def extract(self, query, database_name=None):
        """The async version of :meth:`SchemaValidator.extract`: the same arguments and result."""
        return await self.backend.run(self._extract(query, database_name))

    @async_snapshotted
    async def validate(self, query, database_name=None):
        """The async version of :meth:`SchemaValidator.validate`: the same arguments and result."""
        return await self.backend.run(self._validate(query, database_name))
