from collections.abc import Generator
from typing import Any

import pandas as pd
import regex as re

from cypher_guardrail import patterns
from cypher_guardrail._internal.core import builders as _issues
from cypher_guardrail._internal.core import literals
from cypher_guardrail._internal.core import queries as _queries
from cypher_guardrail._internal.core.io import Concurrently, Request
from cypher_guardrail._internal.core.scopes import restore_variables, separate_variables
from cypher_guardrail._internal.core.valuetypes import fits
from cypher_guardrail._internal.sources import hybrid as _modes
from cypher_guardrail.backend import (
    CypherError,
    async_snapshotted,
    get_async_backend,
    get_backend,
    snapshotted,
)
from cypher_guardrail.issues import Issue
from cypher_guardrail.utils import paths_after_inference, replace_with_labels


def _plain_names(found):
    """What ``_extract`` found, as ``extract`` returns it: the properties as their names (the ones that were written
    between back-quotes are written so in ``_extract``, where the database is asked about them), and the labels split
    by kind, ``{"node": {label: properties}, "relationship": {type: properties}}``."""
    var_props_map, label_props_map = found
    return (
        {
            var: [patterns.unquote_name(prop) for prop in props]
            for var, props in var_props_map.items()
        },
        {
            kind: {
                label: [patterns.unquote_name(prop) for prop in props]
                for (label_kind, label), props in label_props_map.items()
                if label_kind == kind
            }
            for kind in ("node", "relationship")
        },
    )


class _PropertiesValidator(_modes.ModeInfo):
    """The logic of PropertiesValidator, as generators (see ``cypher_guardrail._internal.core.io``): one copy for the sync and the async validator."""

    def __init__(self, driver, *, snapshot=None, verify=None, check_types=False):
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
            check_types (bool): Also report a property that the query compares with a literal of a type that none of
                its values has (``cypher_guardrail.Warning.PropertyTypeMismatch``): see :meth:`PropertiesValidator.validate`.
                Off by default. With a snapshot it needs one captured with ``types=True``.

        Raises:
            TypeError: If the driver is not a FalkorDB client or graph.
        """
        self.driver = driver
        self.check_types = check_types
        self.backend = self._open_backend(driver, snapshot, verify)

    def _open_backend(self, driver, snapshot, verify):
        """The backend the validator runs on. The async validators override this."""
        return get_backend(driver, snapshot=snapshot, verify=verify)

    def __query_prop_exist(
        self, kind, label, property, database_name=None
    ) -> Generator[Request, Any, tuple[bool, list]]:
        """Check if this property exists for the defined label.

        Args:
            - kind (str): "node" if ``label`` is a node label, "relationship" if it is a relationship type.
            - label(str): The label (or relationship type) to check.
            - property (str): The property to check.
            - database_name (str | None): The graph to validate the query against. None (default) uses the graph the validator was given.

        Returns:
            - bool: True if the property exists, False otherwise (also if the label does not exist).
            - metadata (list[dict]): The warning about an unknown property key (cypher_guardrail.Warning.UnknownPropertyKey), if any.
              Each dictionary includes:
              - 'code' (str): The warning code.
              - 'description' (str): A detailed message describing the issue.
        """
        property_exists = False
        metadata = []
        graph = self.backend.graph_name(database_name)
        if (
            yield from _queries.name_exists(graph, kind, label)
        ):  # otherwise the label does not exist in the graph, and neither does the property
            try:
                property_exists, notifications = yield from _queries.property_exists(
                    graph, label, kind, property
                )
                # The warning about a property key that exists on no node or relationship at all
                metadata.extend(n.copy() for n in notifications)
            except CypherError:
                # The check cannot be run (a syntax or execution error), so the property does not exist
                return False, metadata
        return property_exists, metadata

    def __check_properties(
        self, checks, database_name
    ) -> Generator[Request, Any, list[tuple[bool, list]]]:
        """Check several properties together: ``checks`` is a list of ``(kind, label, property)``.

        Returns:
            What ``__query_prop_exist`` returns for each of them, in the same order.
        """
        return list(
            (
                yield Concurrently(
                    tuple(
                        self.__query_prop_exist(kind, label, property, database_name)
                        for kind, label, property in checks
                    )
                )
            )
        )

    def __get_inferred_labels_properties(
        self,
        strict,
        var_label_map,
        label_props_map,
        var_props_map,
        query,
        database_name=None,
    ) -> Generator[Request, Any, dict]:
        """
        Infers the labels of variables that have none but access properties, and adds the labels with the
        properties they are accessed with to label_props_map ((kind, label) -> properties).

        A variable can fit several label combinations that exist in the graph. The properties are attributed to
        the combination in which all of them are valid. If there is none, ``strict`` decides.

        Args:
        - strict (bool): What to do when no label combination has all the properties:
            - If True, the properties are ignored: they are not attributed to any label, so they cannot lower the score.
            - If False, they are attributed to the combination that has the most of them, so the invalid ones lower the score.
        - var_label_map (dict): Variable -> its (kind, label), for the variables that have one.
        - label_props_map (dict): (kind, label) -> the properties accessed on it so far.
        - var_props_map (dict): Variable without a label -> the properties accessed on it.
        - query (str): The query, whose paths are used to infer the labels.
        - database_name (str | None): The graph to validate the query against. None (default) uses the graph the validator was given.

        Returns:
        - dict: label_props_map, with the inferred labels and their properties added.
        """
        # Infer the unknown variables
        inference_paths = self.__prepare_query_for_infer(var_label_map, query)
        # A bare variable in square brackets, [r], is a relationship; in parentheses, (a), a node
        relationship_variables = set(
            re.findall(r"\[\s*(\w+)\s*\]", " ".join(inference_paths))
        )

        def kind_of(variable):
            return "relationship" if variable in relationship_variables else "node"

        graph = self.backend.graph_name(database_name)
        df_valid_label_pairs = yield from paths_after_inference(
            graph, inference_paths
        )  # DataFrames of the label combinations found for the variables
        for df_var_labels in df_valid_label_pairs:
            # Keep the columns of the variables that access properties
            cols_to_keep = [
                col for col in df_var_labels.columns if col in var_props_map
            ]
            if cols_to_keep:
                # Sorted, so that which of several equally valid combinations is used does not depend on the
                # order the rows came in (the scan order of the database, or the order of a snapshot)
                df_filtered = (
                    df_var_labels[cols_to_keep]
                    .drop_duplicates()
                    .sort_values(cols_to_keep)
                    .reset_index(drop=True)
                )
            else:
                df_filtered = pd.DataFrame()

            # One row: there is only one possible label for each variable
            if len(df_filtered.index) == 1:
                valid_label = True  # whether all the labels have all the properties accessed on them
                label_props = {}  # inferred label -> the properties accessed on its variable
                # Every property is checked whatever the others give, so the checks are asked together
                results = iter(
                    (
                        yield from self.__check_properties(
                            [
                                (
                                    kind_of(variable),
                                    df_filtered[variable].iloc[0],
                                    accessed_prop,
                                )
                                for variable in df_filtered.columns
                                for accessed_prop in var_props_map[variable]
                            ],
                            database_name,
                        )
                    )
                )
                for variable in df_filtered.columns:
                    label = (
                        kind_of(variable),
                        df_filtered[variable].iloc[0],
                    )  # the label of the variable, with its kind
                    label_props = self.__extend_dict_with_list_elements(
                        label_props, label, var_props_map[variable]
                    )
                    for _accessed_prop in var_props_map[variable]:
                        property_exists, _ = next(results)
                        if not property_exists:
                            valid_label = False
                # Attribute the properties to the inferred labels (every variable has its own). With strict,
                # properties that are not all valid are ignored
                if valid_label or not strict:
                    for label in label_props:
                        label_props_map = self.__extend_dict_with_list_elements(
                            label_props_map, label, label_props[label]
                        )
            elif len(df_filtered.index) > 1:  # several possible label combinations
                # Look for a combination in which all the accessed properties are valid. If there is none,
                # (without strict) use the one with the most valid properties.
                label_pair_correct_props = {}  # row of df_filtered -> how many of its properties are valid
                for index, row in df_filtered.iterrows():
                    total_correct_props = 0
                    # The variables of a row depend on each other: every one of their labels needs its properties
                    valid_label = True  # whether all the labels have all the properties accessed on them
                    label_props = {}  # inferred label -> the properties accessed on its variable
                    results = iter(
                        (
                            yield from self.__check_properties(
                                [
                                    (kind_of(variable), row[variable], accessed_prop)
                                    for variable in df_filtered.columns
                                    for accessed_prop in var_props_map[variable]
                                ],
                                database_name,
                            )
                        )
                    )
                    for variable in df_filtered.columns:
                        label = (
                            kind_of(variable),
                            row[variable],
                        )  # the label this row gives the variable, with its kind
                        label_props = self.__extend_dict_with_list_elements(
                            label_props, label, var_props_map[variable]
                        )
                        for _accessed_prop in var_props_map[variable]:
                            property_exists, _ = next(results)
                            if not property_exists:
                                valid_label = False
                            else:
                                total_correct_props += 1
                    label_pair_correct_props[index] = total_correct_props
                    if valid_label:
                        # Attribute the properties to the labels of this row
                        for label in label_props:
                            label_props_map = self.__extend_dict_with_list_elements(
                                label_props_map, label, label_props[label]
                            )
                        break  # no need to look at the other rows
                # With strict, properties that are not all valid for any row are ignored
                if not valid_label and not strict:
                    # Use the row with the most valid properties
                    max_index = max(
                        label_pair_correct_props,
                        key=lambda index: label_pair_correct_props[index],
                    )
                    label_props = {}

                    for variable in df_filtered.columns:
                        label = (
                            kind_of(variable),
                            df_filtered.loc[max_index, variable],
                        )
                        label_props = self.__extend_dict_with_list_elements(
                            label_props, label, var_props_map[variable]
                        )

                    for label in label_props:
                        label_props_map = self.__extend_dict_with_list_elements(
                            label_props_map, label, label_props[label]
                        )

        return label_props_map

    def __infer_disjoint_labels(
        self, var_label_map, label_props_map, query, database_name
    ) -> Generator[Request, Any, tuple[dict, dict, str]]:
        """
        Detects alternatives (e.g. ``Label1|Label2``) that access properties, and narrows them to the
        alternatives that have the most of those properties.

        FalkorDB has no node label alternatives (``(n:A|B)`` is a syntax error), so in practice these are
        relationship types, ``[r:TYPE1|TYPE2]``.

        If every alternative has all the properties, the alternatives are kept. Otherwise they are replaced by
        the alternative (or alternatives) with the most valid properties, in var_label_map, label_props_map and the query.

        Args:
        - var_label_map (dict): Variable -> its (kind, label).
        - label_props_map (dict): (kind, label) -> the properties accessed on it.
        - query (str): The query, in which the alternatives are replaced.
        - database_name (str | None): The graph to validate the query against. None (default) uses the graph the validator was given.

        Returns:
        - var_label_map(dict): The updated version of var_label_map
        - label_props_map: The updated version of label_props_map
        - query(str): The updated version of the query
        """
        for examined_key in label_props_map.copy():
            kind, examined_label = examined_key
            if "|" in examined_label:  # alternatives: Label1|Label2|...
                disjoint_labels = examined_label.split("|")
                properties_accessed = label_props_map[examined_key]

                label_pair_correct_props = {}  # alternative -> how many of the properties it has
                valid_label = True  # whether every alternative has all the properties
                results = iter(
                    (
                        yield from self.__check_properties(
                            [
                                (kind, label, accessed_prop)
                                for label in disjoint_labels
                                for accessed_prop in properties_accessed
                            ],
                            database_name,
                        )
                    )
                )
                for label in disjoint_labels:
                    total_correct_props = 0
                    for _accessed_prop in properties_accessed:
                        property_exists, _ = next(results)
                        if not property_exists:
                            valid_label = False
                        else:
                            total_correct_props += 1
                    label_pair_correct_props[label] = total_correct_props
                if not valid_label:  # some alternative lacks a property: keep the alternatives with the most
                    max_correct_props = max(label_pair_correct_props.values())

                    # The alternatives that have that many
                    selected_labels = [
                        i
                        for i, v in label_pair_correct_props.items()
                        if v == max_correct_props
                    ]

                    if len(selected_labels) == 1:
                        updated_label = selected_labels[0]
                    else:  # several tie: they stay alternatives
                        updated_label = "|".join(selected_labels)

                    # 1: Update var_label_map
                    for var, labelled in var_label_map.items():
                        if labelled == examined_key:
                            var_label_map[var] = (kind, updated_label)

                    # 2: Update label_props_map (what the narrowed label already has is kept)
                    if examined_key in label_props_map:
                        label_props_map = self.__extend_dict_with_list_elements(
                            label_props_map,
                            (kind, updated_label),
                            label_props_map.pop(examined_key),
                        )

                    # 3: Update the query
                    query = query.replace(examined_label, updated_label)

        return var_label_map, label_props_map, query

    def __get_var_labels_props(
        self, query, database_name
    ) -> Generator[Request, Any, tuple[dict, dict, dict]]:
        """
        Extract the variables, labels and properties of the query, as far as its text tells.

        Args:
            query (str): The query to be validated.
            database_name (str | None): The graph to validate the query against.

        Returns:
            tuple: Three dicts:
            - var_label_map: variable -> its (kind, label), for the variables that have one;
            - label_props_map: (kind, label) -> the properties accessed on it;
            - var_props_map: variable without a label -> the properties accessed on it (its label is inferred later).
        """
        # What is in a string or a comment is not Cypher: the patterns below must not see it. (An index procedure has
        # its label in a string, so it reads the query as written.)
        original = query
        # A property key between back-quotes is read as a plain name, and written as it was where the database is asked
        query, written_keys = patterns.hide_quoted_keys(query)
        query, _ = patterns.hide_literals(query)
        # Remove *x..y in relationships, if any
        query = patterns.VARIABLE_LENGTH.sub("", query)

        # Labels and variables written in the patterns of the query, and the properties in their {maps}
        var_label_map, label_props_map = (
            PropertiesValidator.__inline_var_label_properties_mapping(query)
        )
        # In CALL db.idx.fulltext.queryNodes('Label', ...) YIELD node, `node` is a `Label`;
        # a vector query also accesses its attribute
        for call in patterns.index_procedure_calls(original):
            index_label = ("node" if call.on == "nodes" else "relationship", call.label)
            if call.variable:
                var_label_map[call.variable] = index_label
            if call.attribute:
                label_props_map = PropertiesValidator.__extend_dict_with_list_elements(
                    label_props_map, index_label, [call.attribute]
                )
        # The properties accessed as variable.property (in WHERE, RETURN, ...)
        label_props_map, var_props_map = (
            PropertiesValidator.__outside_var_properties_mapping(
                var_label_map, label_props_map, query
            )
        )
        label_props_map = restore_variables(label_props_map, written_keys)
        var_props_map = restore_variables(var_props_map, written_keys)

        # Alternatives (Label1|Label2) that access properties are narrowed to the ones that have them
        var_label_map, label_props_map, query = yield from self.__infer_disjoint_labels(
            var_label_map, label_props_map, query, database_name
        )

        return var_label_map, label_props_map, var_props_map

    @_queries.graph_checked
    def _extract(
        self, query, database_name=None, strict=False
    ) -> Generator[Request, Any, tuple[dict, dict]]:
        """The logic of ``extract``, as a generator."""
        self.backend.check_database(database_name)
        # A name that a WITH dropped, or that a CALL { } does not import, is a new variable when it is declared again:
        # it gets a name of its own while the query is read, and gets its own back in the result
        query, renames = separate_variables(query)
        # The branches of a UNION are separate queries: a variable of one is not the variable of another
        var_props_map: dict = {}
        label_props_map: dict = {}
        for branch in patterns.split_union(query):
            branch_vars, branch_labels = yield from self.__extract_branch(
                branch, database_name, strict
            )
            for var, props in branch_vars.items():
                self.__extend_dict_with_list_elements(var_props_map, var, props)
            for label, props in branch_labels.items():
                self.__extend_dict_with_list_elements(label_props_map, label, props)
        return restore_variables(var_props_map, renames), label_props_map

    def __extract_branch(
        self, query, database_name, strict
    ) -> Generator[Request, Any, tuple[dict, dict]]:
        """The properties of one query (or of one branch of a UNION)."""
        # The labels and properties the text of the query gives
        (
            var_label_map,
            label_props_map,
            var_props_map,
        ) = yield from self.__get_var_labels_props(query, database_name)

        # Infer the labels of the variables that have none but access properties
        if var_props_map:
            label_props_map = yield from self.__get_inferred_labels_properties(
                strict,
                var_label_map,
                label_props_map,
                var_props_map,
                patterns.hide_literals(query)[0],
                database_name,
            )

        return var_props_map, label_props_map

    @_queries.graph_checked
    def _validate(
        self, query, database_name=None, strict=False
    ) -> Generator[Request, Any, tuple[float | None, list[Issue]]]:
        """The logic of ``validate``, as a generator."""
        metadata: list[Issue] = []  # the problems found
        _, label_props_map = yield from self._extract(query, database_name, strict)
        if label_props_map:
            correct_props = 0  # the properties that exist on their label
            incorrect_label_props_map = {}  # (kind, label) -> its properties that do not exist

            # The properties are checked independently of each other, so the checks are asked together.
            # Their results are used below in the order of the query, so nothing else changes.
            checked = []  # (kind, label, property) of every property to check
            for (kind, label), properties in label_props_map.items():
                # For alternatives (Label1|Label2|...) check only the first: after narrowing, they all
                # have the same number of valid properties
                if "|" in label:
                    label = label.split("|")[0]
                checked.extend((kind, label, property) for property in properties)
            results = yield from self.__check_properties(checked, database_name)

            for (kind, label, property), (property_exists, notifications) in zip(
                checked, results, strict=True
            ):
                if property_exists:
                    correct_props += 1
                else:
                    incorrect_label_props_map = self.__extend_dict_with_list_elements(
                        incorrect_label_props_map, (kind, label), [property]
                    )

                if notifications:  # the unknown property key warning
                    metadata.extend(notifications)

            total_props = sum(
                len(properties) for properties in label_props_map.values()
            )

            precision = correct_props / total_props
            if precision < 1:
                for (kind, label), props in incorrect_label_props_map.items():
                    metadata.append(
                        _issues.invalid_property_access(
                            kind, label, [patterns.unquote_name(prop) for prop in props]
                        )
                    )
            # Only a warning: it does not change the score
            metadata.extend((yield from self.__type_issues(query, database_name)))
            return precision, metadata

        return None, metadata  # no property was accessed

    def __type_issues(
        self, query, database_name
    ) -> Generator[Request, Any, list[Issue]]:
        """The properties the query compares with a literal of a type that none of their values has.

        Only the comparisons whose label the query gives are checked: the ``{map}`` of a pattern with a label, and
        ``variable.property`` where the variable has a label in the query. A variable whose label is inferred is
        left out, so that a guess is never reported. A property with several types (mixed data) is fine if the literal
        has any of them; a property with no value, and a snapshot without types, say nothing.
        """
        if not self.check_types:
            return []
        graph = self.backend.graph_name(database_name)
        questions = []  # (label, "node" or "relationship", the comparison)
        query, _ = separate_variables(
            query
        )  # so that the variables are the ones of their scope
        # The branches of a UNION are separate queries: a variable of one is not the variable of another
        for branch in patterns.split_union(query):
            found = literals.comparisons(branch)
            if not found:
                continue
            var_label_map, _, _ = yield from self.__get_var_labels_props(
                branch, database_name
            )
            for comparison in found:
                label, entity = comparison.label, comparison.entity
                if (
                    label is None or entity is None
                ):  # a `variable.property`: the variable's label is in the query
                    labelled = var_label_map.get(comparison.variable)
                    if (
                        labelled is None or "|" in labelled[1]
                    ):  # no label, or alternatives
                        continue
                    entity, label = labelled
                    if not (
                        yield from _queries.name_exists(graph, entity, label)
                    ):  # not a label of the graph: the schema validator reports it
                        continue
                questions.append((label, entity, comparison))
        # What the values of each property are does not depend on the literal, so the questions are asked together
        keys = list(
            dict.fromkeys(
                (label, entity, comparison.property, comparison.value_type)
                for label, entity, comparison in questions
            )
        )
        answers = dict(
            zip(
                keys,
                (
                    yield Concurrently(
                        tuple(_queries.property_types(graph, *key) for key in keys)
                    )
                ),
                strict=True,
            )
        )
        mismatches: dict[tuple, list[str]] = {}  # each once, in the order of the query
        for label, entity, comparison in questions:
            observed = answers[
                (label, entity, comparison.property, comparison.value_type)
            ]
            if observed and not fits(comparison.value_type, observed):
                mismatches.setdefault(
                    (
                        entity,
                        label,
                        comparison.property,
                        comparison.value,
                        comparison.value_type,
                    ),
                    list(observed),
                )
        return [
            _issues.property_type_mismatch(
                entity, label, prop, types, value, value_type
            )
            for (entity, label, prop, value, value_type), types in mismatches.items()
        ]

    @staticmethod
    def __extend_dict_with_list_elements(mydict, key, list_value):
        """
        Add the key with a copy of the list to ``mydict``, or, if the key exists, append the
        elements of the list that its list does not have yet.
        Args:
        - mydict (dict): The dictionary to modify.
        - key (hashable): The key to check or add.
        - list_value (list): The list of elements to add or extend.

        Returns:
        - mydict: The modified dictionary.
        """
        if key not in mydict:
            mydict[key] = list(list_value)  # a copy: the list is extended later on
        else:
            mydict[key].extend(prop for prop in list_value if prop not in mydict[key])
        return mydict

    @staticmethod
    def __inline_var_label_properties_mapping(query):
        """
        Extract the labels written in the patterns of the query: which variable has which label, and
        which properties are mentioned in the ``{map}`` of each pattern.

        Each pattern found is a pair like ``('var:Label', '{ property_name : property_value}')``.

        Args:
            query (str): The Cypher query to validate.
        Returns:
            Dict (var_label_map) that maps any variables with their (kind, label)
            Dict (label_props_map) that maps the (kind, label) of a node or a relationship with a list of the
            properties mentioned for it in the patterns
        """

        label_props_map = {}  # (kind, label) -> the properties mentioned in its patterns
        # variable -> (kind, label): a later mention of the variable without its label is then known
        var_label_map = {}

        # Find patterns of nodes or relationships that define their label
        # To match a node: optional_variable:label{optional properties} (label can be one or more labels (e.g., :Label1 or :Label1|Label2))
        node_pattern = r"\(\s*(\w*\s*:\s*\w+(?:\s*\|\s*\w+)*)\s*(\{[^}]*\})?\s*\)"
        # To match a relationship: optional_variable:type{optional properties}  (type can be one or more types (e.g., :Type1 or :Type1|Type2))
        rel_pattern = r"\[\s*(\w*\s*:\s*\w+(?:\s*\|\s*\w+)*)\s*(\{[^}]*\})?\s*\]"
        # Match nodes and relationships separately
        node_matches = re.findall(node_pattern, query)
        rel_matches = re.findall(rel_pattern, query)
        matches = [("node", match) for match in node_matches] + [
            ("relationship", match) for match in rel_matches
        ]

        # The properties mentioned inside the node or relationship: {name: value, ...}
        label_pattern = r":(.*)"  # the label, after the colon
        var_pattern = r"(.*):"  # the variable, before the colon
        property_pattern = (
            r"\b(\w+)\s*:"  # a property name: the word before a colon in the {map}
        )

        for kind, match in matches:
            # Extract the label
            label = re.search(label_pattern, match[0]).group(1).strip()

            # Extract the var if any
            var = re.search(var_pattern, match[0]).group(1).strip()
            if var:
                var_label_map[var] = (kind, label)

            # Extract the properties
            properties = []
            if match[1]:  # the pattern also has a {map} of properties
                # Blank the string values first: a ':' inside one (e.g. a time '12:30') is not a property name
                # (this knows escapes and the other kind of quote inside a string: "O'Brien")
                cleared_match = patterns.strip_literals_and_comments(match[1])
                # Each property once, in the order it appears in the query
                properties = list(
                    dict.fromkeys(re.findall(property_pattern, cleared_match))
                )
                if properties:
                    label_props_map = (
                        PropertiesValidator.__extend_dict_with_list_elements(
                            label_props_map, (kind, label), properties
                        )
                    )
        return var_label_map, label_props_map

    @staticmethod
    def __outside_var_properties_mapping(var_label_map, label_props_map, query):
        """
        Find the ``variable.property`` accesses of the query (in WHERE, RETURN, ...). A property accessed on a
        variable with a known label (in var_label_map) is added to that label in label_props_map; one accessed
        on a variable without a label goes to var_props_map, and its label is inferred later.

        Args:
            Dict (var_label_map) that maps any variables with their (kind, label)
            Dict (label_props_map), that maps (kind, label) with properties they access
            query (str): The Cypher query to validate.
        Returns:
            Dict (label_props_map), that maps (kind, label) with properties they access, updated.
            Dict (var_props_map), that maps variables without a label with the properties accessed on them.
        """
        var_props_map = {}  # variable without a label -> the properties accessed on it

        # A variable.property that is not inside a string ('...' or "...") and so not a value.
        # Not `db.idx.fulltext.queryNodes(...)`, `vec.euclideanDistance(...)` or `1.5`: those are not variable.property
        var_property_pattern = (
            r'(?<!["\'\w.])(?!\d)(\w+)\s*\.\s*(\w+)\b(?!["\']|\s*[.(])'
        )

        var_props = re.findall(
            var_property_pattern, query
        )  # (variable, property) pairs
        for var_property in var_props:
            variable = var_property[0]
            property = var_property[1]
            if variable in var_label_map:  # the label is known
                label = var_label_map[variable]
                label_props_map = PropertiesValidator.__extend_dict_with_list_elements(
                    label_props_map, label, [property]
                )
            else:  # no label: it will be inferred
                var_props_map = PropertiesValidator.__extend_dict_with_list_elements(
                    var_props_map, variable, [property]
                )

        return label_props_map, var_props_map

    @staticmethod
    def __extract_1hop_paths(query):
        """Extract the one-hop paths and the single nodes of a Cypher query, as a list.
        A path of several hops is broken into one-hop paths.

        Args:
            query(str): The query to be validated
        Returns:
            List of one hop paths (including single nodes, if any)
        """

        # A node: (a:Label), (:Label), (a), (a:Label {prop:1}), (:Label {prop:1}), (), or with alternatives (:Label1|Label2)
        node_pattern = r"\(\s*(?:\s*\w+\s*:\s*\w+(?:\s*\|\s*\w+)*\s*|\s*\w*\s*|:\s*\w+(?:\s*\|\s*\w+)*\s*|)\s*(?:\{[^}]*\})?\s*\)"
        # A relationship, written like a node but in square brackets
        rel_pattern = r"\[\s*(?:\s*\w+\s*:\s*\w+(?:\s*\|\s*\w+)*\s*|\s*\w*\s*|:\s*\w+(?:\s*\|\s*\w+)*\s*|)\s*(?:\{[^}]*\})?\s*\]"
        # A one-hop path: node, relationship, node
        path_pattern = rf"({node_pattern}\s*<?-\s*{rel_pattern}\s*->?\s*{node_pattern})"
        # The one-hop paths, then the single nodes
        one_hop_paths = []

        # Find the one-hop paths (overlapping: a path's last node starts the next path)
        matches = re.findall(path_pattern, query, overlapped=True)

        nodes_in_paths = set()  # the nodes that appear in a path
        for path_match in matches:
            one_hop_paths.append(path_match)
            nodes = re.findall(node_pattern, path_match)
            nodes_in_paths.update(nodes)

        # The nodes that appear in no path are single nodes
        all_node_matches = re.findall(node_pattern, query, overlapped=True)
        single_node_matches = [
            node for node in all_node_matches if node not in nodes_in_paths
        ]

        one_hop_paths.extend(single_node_matches)

        return one_hop_paths

    @staticmethod
    def __prepare_query_for_infer(var_label_map, query):
        """
        Find the one-hop paths of the query that have variables whose label is unknown:
        1. Replace the variables whose label is known with their label (so they are not inferred)
        2. Replace -- with -[]-
        3. Remove *x..y in relationships, if any
        4. Break the query into one-hop paths and single nodes
        5. Remove the {properties} from each of them
        6. Keep those that have a variable to infer

        Args:
            Dict (var_label_map): that maps any variables with their (kind, label)
            query(str): The query to be validated
        Returns:
            List of 1 hop paths that have unknown variables to infer
        """
        # Give the variables that have a known label their label
        query = replace_with_labels(
            query, {var: label for var, (_, label) in var_label_map.items()}
        )

        # Replace -- with empty relationship
        query = query.replace("--", "-[]-")

        # Remove *x..y in relationships, if any
        query = patterns.VARIABLE_LENGTH.sub("", query)

        # Extract 1 hop paths
        one_hop_paths = PropertiesValidator.__extract_1hop_paths(query)

        # Remove the {properties}
        prop_pattern = r"\{[^}]*\}"
        no_prop_paths = [re.sub(prop_pattern, "", path) for path in one_hop_paths]

        # A bare variable, (a) or [r], is one to infer
        pattern = r"\(\w+\)|\[\w+\]"
        return [path for path in no_prop_paths if re.search(pattern, path)]


class PropertiesValidator(_PropertiesValidator):
    """
    A class to validate the correctness of property access in Cypher queries against the schema of a graph.

    A property access is valid if some node (or relationship) with the label (or type) has the property.
    The label of a variable is taken from the query, or inferred from the graph when the query leaves it out.
    """

    @snapshotted
    def extract(self, query, database_name=None, strict=False):
        """
        Extract the properties accessed by each label of the provided query

        Args:
            query (str): The Cypher query to validate.
            database_name (str | None): The graph to validate the query against. None (default) uses the graph the validator was given.
            strict (bool): How to treat the properties accessed on a variable without a label, whose label is inferred.
                If True, they are used only when some label has all of them; otherwise they are ignored.
                If False, they are attributed to the label that has the most of them, so an invalid one is reported.

        Returns:
            tuple: Two dicts:

            - var_props_map: variable without a label in the query -> the properties accessed on it;
            - label_props_map: ``{"node": {label: properties}, "relationship": {type: properties}}``, the node labels
              and the relationship types (written in the query, or inferred) with the properties accessed on each.
              A node label and a relationship type with the same name are kept apart.
        """
        return _plain_names(
            self.backend.run(self._extract(query, database_name, strict))
        )

    @snapshotted
    def validate(self, query, database_name=None, strict=False):
        """
        Validate the correctness of the properties accessed by labels in the given query against the schema of the graph.

        Args:
            query (str): The Cypher query to validate.
            database_name (str | None): The graph to validate the query against. None (default) uses the graph the validator was given.
            strict (bool): How to treat the properties accessed on a variable without a label: see :meth:`extract`.

        Returns:
            tuple: ``(score, metadata)``.

            score (float | None): The precision, in [0, 1]: the properties that exist on their label divided by
            all the properties accessed. None if the query accesses no property.

            metadata (list[dict]): The problems found. Each dictionary includes:

            - 'code' (str): ``cypher_guardrail.Warning.InvalidPropertyAccess`` for a property that its node label or
              relationship type does not have (``kind`` says which of the two ``name`` is),
              ``cypher_guardrail.Warning.UnknownPropertyKey`` for a property that exists nowhere in the graph, and, with
              ``check_types=True``, ``cypher_guardrail.Warning.PropertyTypeMismatch`` for a property that the query compares
              with a literal of a type that none of its values has (FalkorDB finds nothing then, and says nothing).
              That warning does not change the score. It covers the variables that have a label in the query and the
              ``{map}`` of a pattern, and only comparisons with literals (not parameters or expressions).
            - 'description' (str): A detailed message describing the issue.

        Raises:
            GraphNotFoundError: If the validation asked the database about a graph that does not exist.
        """
        return self.backend.run(self._validate(query, database_name, strict))


class AsyncPropertiesValidator(_PropertiesValidator):
    """The async version of :class:`PropertiesValidator`, for a ``falkordb.asyncio`` client or graph: ``extract`` and ``validate`` are coroutines."""

    def __init__(
        self,
        driver,
        *,
        max_concurrency=8,
        snapshot=None,
        verify=None,
        check_types=False,
    ):
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
            check_types (bool): Also report a property compared with a literal of a type that none of its values
                has: see :class:`PropertiesValidator`. Off by default.

        Raises:
            TypeError: If the driver is not an async FalkorDB client or graph.
            ValueError: If ``max_concurrency`` is less than 1.
        """
        self._max_concurrency = max_concurrency
        super().__init__(
            driver, snapshot=snapshot, verify=verify, check_types=check_types
        )

    def _open_backend(self, driver, snapshot, verify):
        return get_async_backend(
            driver, self._max_concurrency, snapshot=snapshot, verify=verify
        )

    @async_snapshotted
    async def extract(self, query, database_name=None, strict=False):
        """The async version of :meth:`PropertiesValidator.extract`: the same arguments and result."""
        return _plain_names(
            await self.backend.run(self._extract(query, database_name, strict))
        )

    @async_snapshotted
    async def validate(self, query, database_name=None, strict=False):
        """The async version of :meth:`PropertiesValidator.validate`: the same arguments and result."""
        return await self.backend.run(self._validate(query, database_name, strict))
