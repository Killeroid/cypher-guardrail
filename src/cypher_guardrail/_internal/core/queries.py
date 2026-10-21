"""What cypher-guardrail asks FalkorDB, written as generators that yield requests (see ``core/io.py``).

Every function takes the resolved graph name and ``yield from`` is how one calls another. They are run by
``run_sync`` (``FalkorDBBackend``) or ``run_async``, so the logic here exists once for both APIs. Inside a
snapshot the driver keeps the answer of each request, so the lookups below can be repeated freely: they
cost one round-trip each per snapshot.

See the ``backend`` module docstring for the FalkorDB peculiarities these functions compensate for.
"""

import functools
from collections.abc import Generator, Iterable
from typing import Any

import pandas as pd
import regex as re

from cypher_guardrail._internal.core import builders as _issues
from cypher_guardrail._internal.core.errors import (
    NOT_READ_ONLY,
    CypherError,
    GraphNotFoundError,
    is_missing_graph,
    missing_graph_key,
)
from cypher_guardrail._internal.core.io import (
    Concurrently,
    Explain,
    Lookup,
    Recall,
    Request,
    RoQuery,
)
from cypher_guardrail.issues import (
    ParameterNotProvidedIssue,
    UnknownLabelIssue,
    UnknownRelationshipTypeIssue,
    WriteQueryErrorIssue,
    WriteQueryWarningIssue,
)
from cypher_guardrail.patterns import strip_literals_and_comments, unquote_name

_PARAMETER = re.compile(r"\$(\w+)")
# The start of a `CYPHER a=1 b='x' ...` prefix, and one `name=` in it
_CYPHER_PREFIX = re.compile(r"\s*CYPHER\b", re.IGNORECASE)
_PARAMETER_NAME = re.compile(r"\s*(\w+)\s*=\s*")
# (var:Label1:Label2 ...) -> ':Label1:Label2'
_NODE_LABELS = re.compile(r"\(\s*(?:[A-Za-z_]\w*)?((?:\s*:\s*(?:\w+|`[^`]*`))+)")
# [var:TYPE1|TYPE2*1..3 {props}] -> 'TYPE1|TYPE2'
_REL_TYPES = re.compile(
    r"\[\s*(?:[A-Za-z_]\w*)?\s*:\s*((?:[\w|:\s]|`[^`]*`)+?)\s*(?:\*[^\]{]*)?(?:\{[^}]*\})?\s*\]"
)
# a name in a list of them: a word, or what is between back-quotes (without them)
_NAME = re.compile(r"`([^`]*)`|\w+")


def _names(group: str) -> list[str]:
    return [
        m.group(1) if m.group(1) is not None else m.group()
        for m in _NAME.finditer(group)
    ]


def _parameter_header(query: str) -> tuple[set[str], int | None]:
    """The names that a ``CYPHER a=1 b='x' ...`` prefix gives the query, and where the prefix ends (None if there is none)."""
    prefix = _CYPHER_PREFIX.match(query)
    if not prefix:
        return set(), None
    names = set()
    position = prefix.end()
    while name := _PARAMETER_NAME.match(query, position):
        names.add(name.group(1))
        position = _end_of_value(query, name.end())
    return names, position


def provided_parameters(query: str) -> set[str]:
    """The names of the parameters that a ``CYPHER a=1 b='x' ...`` prefix gives the query."""
    return _parameter_header(query)[0]


# What FalkorDB says when it plans a query and needs the value of a parameter (it does on a label that has an index,
# and in LIMIT and SKIP) and has none: the validator does not have the values, only the names
_NEEDS_A_VALUE = ("Missing parameters", "operates only on non-negative integers")


def _placeholder(name: str, text: str) -> str:
    """``{}`` for a parameter whose property is read (``$p.title``), ``0`` for any other."""
    return "{}" if re.search(r"\$" + re.escape(name) + r"\s*\.", text) else "0"


def with_placeholder_parameters(query: str) -> str | None:
    """``query`` with a ``CYPHER`` header that gives a placeholder to each parameter that no header gives, or None.

    ``0`` is a valid ``LIMIT`` and ``SKIP`` and an inert value anywhere else a plan looks at a value, and ``{}`` is
    one for a parameter whose property is read (``$p.title``). The placeholders only let FalkorDB plan the query:
    the parameters that nobody provided are still reported, from the text.
    """
    given, end = _parameter_header(query)
    text = strip_literals_and_comments(query)
    names = [
        name for name in dict.fromkeys(_PARAMETER.findall(text)) if name not in given
    ]
    if not names:
        return None
    pairs = " ".join(f"{name}={_placeholder(name, text)}" for name in names)
    if end is None:
        return f"CYPHER {pairs} {query}"
    return f"{query[:end]} {pairs}{query[end:]}"


def _end_of_string(text: str, position: int, quote: str) -> int:
    """Where the string that starts at ``position`` (after its opening ``quote``) ends: the index of the closing quote."""
    while position < len(text):
        if text[position] == "\\":
            position += 2  # an escaped character, which may be the quote
        elif text[position] == quote:
            return position
        else:
            position += 1
    return position


def _end_of_value(text: str, start: int) -> int:
    """Where the value of a ``CYPHER`` parameter that starts at ``start`` ends: a string, a list or a map, or a word."""
    depth = 0
    position = start
    while position < len(text):
        char = text[position]
        if char in "'\"":
            position = _end_of_string(text, position + 1, char)
            if position >= len(text):
                return position
            if not depth:
                return position + 1
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if not depth:
                return position + 1
        elif char.isspace() and not depth:
            return position
        position += 1
    return position


def index_exists(
    graph: str, kind: str, on: str, label: str, attribute: str | None = None
) -> Generator[Request, Any, bool]:
    """Check whether the graph has an index that a full-text or vector query needs.

    Args:
        kind (str): 'fulltext' or 'vector'.
        on (str): 'nodes' or 'relationships'.
        label (str): The node label, or the relationship type, of the index.
        attribute (str | None): The attribute the index must cover. None: any attribute.
    """
    return (yield Lookup("index_exists", graph, (kind, on, label, attribute)))


def missing_functions(
    graph: str, names: tuple[str, ...]
) -> Generator[Request, Any, tuple[str, ...]]:
    """Which of the functions ``library.function`` the FalkorDB instance does not have as user-defined functions.

    They belong to the instance, not to the graph: ``graph`` says which connection to ask on. An instance that cannot
    list its functions (an older FalkorDB) is taken to have them all.
    """
    return (yield Lookup("functions_missing", graph, (names,)))


def execute(graph: str, query: str) -> Generator[Request, Any, list[dict]]:
    """Run a read query and return one dict per row, keyed by column name.

    Raises:
        CypherError: If the database rejects the query.
    """
    result = yield RoQuery(graph, query)
    names = [column[1] for column in result.header]
    return [dict(zip(names, row, strict=False)) for row in result.result_set]


def parameter_names(parameters: Iterable[str] | None) -> frozenset[str]:
    """The names in ``parameters``: None, a collection of names, or a dict of the parameters a client is given."""
    if isinstance(parameters, str | bytes):
        raise TypeError(
            "parameters must be a mapping or a collection of parameter names, not a string"
        )
    return frozenset(parameters or ())


def _absent_parameters(query: str, provided: frozenset[str]) -> list[str]:
    """The parameters that the text of the query uses and that are not ``provided``, once each, in order."""
    names = dict.fromkeys(_PARAMETER.findall(strip_literals_and_comments(query)))
    return [name for name in names if name not in provided]


def explain(
    graph: str,
    query: str,
    read_only: bool | None = None,
    parameters: Iterable[str] | None = None,
) -> Generator[
    Request,
    Any,
    list[ParameterNotProvidedIssue | WriteQueryErrorIssue | WriteQueryWarningIssue],
]:
    """Check a query without executing it.

    Args:
        read_only: None does not look for writes. True or False does, and reports a query that can modify the
            graph as an error (True) or as a warning (False). FalkorDB decides what can write: it refuses such
            a query in ``GRAPH.RO_QUERY``, in which a read query is stopped after a millisecond (see ``io``).
        parameters: The names of the parameters the caller will send with the query (a dict of them will do), which
            are not reported as missing. The ones a ``CYPHER`` header gives are not either.

    Returns:
        The problems found: missing parameters, and a write when ``read_only`` is not None.

    Raises:
        TypeError: If ``parameters`` is a string.
        GraphNotFoundError: If the graph does not exist.
        CypherError: If the database rejects the query.
    """
    given = parameter_names(parameters)
    try:
        writes = yield from _explain(graph, query, read_only)
    except CypherError as e:
        if is_missing_graph(e):
            raise GraphNotFoundError(graph) from e
        # FalkorDB cannot plan the query without the value of a parameter: plan it with a placeholder for each
        # (a missing parameter is reported below, from the text, whatever the plan says)
        placeholders = (
            with_placeholder_parameters(query)
            if any(text in e.message for text in _NEEDS_A_VALUE)
            else None
        )
        if placeholders is None:
            raise
        writes = yield from _explain(graph, placeholders, read_only)
    issues: list[
        ParameterNotProvidedIssue | WriteQueryErrorIssue | WriteQueryWarningIssue
    ] = []
    # FalkorDB only reports a missing parameter when the query runs, so detect it from the text.
    # (The caller gives some of them, and so does a ``CYPHER`` header.)
    absent = _absent_parameters(query, given | provided_parameters(query))
    if absent:
        issues.append(_issues.parameters_not_provided(absent))
    if writes and read_only is not None:
        issues.append(_issues.write_query(read_only))
    return issues


def graph_checked(method):
    """Make a validation (a generator method ``(self, query, database_name=None, ...)``) end with GraphNotFoundError when
    it met a graph that does not exist.

    FalkorDB answers "empty key" to everything about such a graph, and the lookups take it for a graph with no schema
    (a capture needs that). They note it (``missing_graph_key``), and the validation says so at its end, so a query that
    asks nothing about the graph (``RETURN 1``) is not an error, and a request that is not made is not an extra one.
    """

    @functools.wraps(method)
    def wrapper(self, query, database_name=None, *args, **kwargs):
        result = yield from method(self, query, database_name, *args, **kwargs)
        graph = self.backend.graph_name(database_name)
        if (yield Recall(missing_graph_key(graph))):
            raise GraphNotFoundError(graph)
        return result

    return wrapper


def _explain(
    graph: str, query: str, read_only: bool | None
) -> Generator[Request, Any, bool]:
    """EXPLAIN ``query``, and say whether FalkorDB refused it as a write (only asked when ``read_only`` is not None)."""
    if read_only is None:
        yield Explain(graph, query)
        return False
    try:
        yield Explain(graph, query, read_only=True)
    except CypherError as e:
        if e.code == NOT_READ_ONLY:
            return True
        # Not an answer about writes (a syntax error, or something this form of the request gets
        # wrong, such as the parameters of a ``CYPHER a=1 ...`` query): the plain EXPLAIN gives the
        # database's own answer, and raises the error if there is one
        yield Explain(graph, query)
    return False


def plan(graph: str, query: str) -> Generator[Request, Any, list[str]]:
    """The plan that FalkorDB makes of a query (``GRAPH.EXPLAIN``), one operation to a line, as it was asked in this
    validation if it was (the answer is kept). A parameter that FalkorDB needs the value of is given a placeholder,
    as in :func:`explain`.

    Raises:
        CypherError: If the database rejects the query.
    """
    try:
        reply = yield Explain(graph, query)
    except CypherError as e:
        placeholders = (
            with_placeholder_parameters(query)
            if any(text in e.message for text in _NEEDS_A_VALUE)
            else None
        )
        if placeholders is None:
            raise
        reply = yield Explain(graph, placeholders)
    return (
        [line for line in reply if isinstance(line, str)]
        if isinstance(reply, list)
        else []
    )


def query_to_df(graph: str, query: str) -> Generator[Request, Any, pd.DataFrame]:
    """Run a read query and return the rows as a DataFrame (columns = returned names)."""
    result = yield RoQuery(graph, query)
    return pd.DataFrame(
        result.result_set, columns=[column[1] for column in result.header]
    )


def _missing_names(
    graph: str, kind: str, names: list[str]
) -> Generator[Request, Any, tuple[str, ...]]:
    """The ``names`` (labels, relationship types or property keys) that the graph does not know."""
    return (yield Lookup("missing_names", graph, (kind, tuple(names))))


def unknown_schema_notifications(
    graph: str, pattern: str
) -> Generator[Request, Any, list[UnknownLabelIssue | UnknownRelationshipTypeIssue]]:
    """Unknown label / relationship type warnings for the labels used in ``pattern``."""
    pattern = strip_literals_and_comments(pattern)
    labels = {
        label for group in _NODE_LABELS.findall(pattern) for label in _names(group)
    }
    rel_types = {t for group in _REL_TYPES.findall(pattern) for t in _names(group)}
    # Both are asked whenever the pattern has labels and types, so they are asked together
    lookups = []
    if labels:
        lookups.append(_missing_names(graph, "labels", sorted(labels)))
    if rel_types:
        lookups.append(_missing_names(graph, "relationship_types", sorted(rel_types)))
    answers = iter((yield Concurrently(tuple(lookups))))
    notifications: list[UnknownLabelIssue | UnknownRelationshipTypeIssue] = []
    if labels:
        notifications += [_issues.unknown_label(label) for label in next(answers)]
    if rel_types:
        notifications += [
            _issues.unknown_relationship_type(rel_type) for rel_type in next(answers)
        ]
    return notifications


def path_exists(graph: str, path: str) -> Generator[Request, Any, tuple[bool, list]]:
    """Check whether a pattern (node, relationship or path) matches anything in the graph.

    Returns:
        tuple[bool, list[dict]]: Whether it exists, and warnings about unknown labels/types.
    """
    exists = yield Lookup("path_exists", graph, (path,))
    notifications = yield from unknown_schema_notifications(graph, path)
    return exists, notifications


def listed_names(graph: str, kind: str) -> Generator[Request, Any, tuple[str, ...]]:
    """Every node label (``kind`` 'labels') or relationship type ('relationship_types') of the graph, sorted."""
    return (yield Lookup("listed_names", graph, (kind,)))


def name_exists(graph: str, kind: str, name: str) -> Generator[Request, Any, bool]:
    """Whether the graph has a node label (``kind`` 'node') or a relationship type (``kind`` 'relationship') called ``name``.

    A node label and a relationship type can have the same name, so the kind is asked for, not guessed.
    """
    missing = yield from _missing_names(
        graph, "labels" if kind == "node" else "relationship_types", [name]
    )
    return not missing


def property_exists(
    graph: str, label: str, kind: str, prop: str
) -> Generator[Request, Any, tuple[bool, list]]:
    """Check whether any node/relationship with ``label`` has property ``prop``.

    Args:
        kind (str): 'node' or 'relationship'.

    Returns:
        tuple[bool, list[dict]]: Whether it exists, and a warning if the property key is unknown.
    """
    # The property keys of the graph do not depend on whether this label has the property, so they are
    # asked together with it
    exists, unknown = yield Concurrently(
        (
            _property_exists(graph, label, kind, prop),
            _missing_names(graph, "property_keys", [unquote_name(prop)]),
        )
    )
    notifications = [_issues.unknown_property(name) for name in unknown]
    return exists, notifications


def _property_exists(
    graph: str, label: str, kind: str, prop: str
) -> Generator[Request, Any, bool]:
    return (yield Lookup("property_exists", graph, (label, kind, prop)))


def property_types(
    graph: str, label: str, kind: str, prop: str, literal_type: str
) -> Generator[Request, Any, tuple[str, ...] | None]:
    """The types of the values of property ``prop`` of ``label`` (``kind`` 'node' or 'relationship'): the names
    ``typeof`` gives, sorted, several when the data is mixed.

    ``literal_type`` is the type of the value the property is compared with. The answer does not depend on it: a
    snapshot uses it to tell a type it has seen from one it has not, which the database is then asked to confirm.

    Returns:
        ``()`` when no value is known (the property has none, or the database cannot say), None when a snapshot
        was captured without the types.
    """
    return (yield Lookup("property_types", graph, (label, kind, prop, literal_type)))
