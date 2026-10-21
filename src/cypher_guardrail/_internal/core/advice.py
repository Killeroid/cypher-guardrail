"""What a person can do about a query that is valid and may be slow: the logic of ``Advisor``, as generators.

It starts from what the plan of FalkorDB says (a product, a scan of every node, a scan of a label that has an index) and
what the text says (a path with no upper limit), and looks in the graph for what the person can write instead: the
labels that have the property, the relationship that connects two labels. A suggestion is only made when the graph says
what it is, and has an ``example`` only when the replacement is one thing: see :mod:`cypher_guardrail.suggestions`.
"""

from collections.abc import Generator
from typing import Any, cast

import regex as re

from cypher_guardrail import patterns
from cypher_guardrail._internal.core import performance
from cypher_guardrail._internal.core import queries as _queries
from cypher_guardrail._internal.core.errors import CypherError, GraphNotFoundError
from cypher_guardrail._internal.core.io import Concurrently, Lookup, Request
from cypher_guardrail._internal.core.plans import Op, parse
from cypher_guardrail.patterns import unquote_name
from cypher_guardrail.suggestions import (
    ADD_LABEL,
    ADD_LIMIT,
    BOUND_PATH,
    CONNECT_PATTERNS,
    MISSING_INDEX,
    USE_INDEX,
    Suggestion,
)

_BOUND = 5  # the upper limit that an example gives to a path
_LIMIT = 100  # the number of rows that an example gives to a query
_HOPS = re.compile(r"\*\s*(\d*)\s*(\.\.)?\s*(\d*)")
_PLAIN_NAME = re.compile(r"\w+")
# a function of a property that is compared: `toLower(m.title) = 'a'`
_WRAPPED = (
    r"(?<![\w.])(\w+)\(\s*{variable}\s*\.\s*(\w+)\s*\)(?=\s*(?:=|<>|<=|>=|<|>|\bIN\b))"
)


def _name(label: str) -> str:
    """A label or a type as the query writes it."""
    return label if _PLAIN_NAME.fullmatch(label) else f"`{label}`"


def _once(query: str, old: str, new: str) -> str | None:
    """``query`` with ``old`` replaced, when ``old`` is written exactly once (what is twice may not mean the same)."""
    return query.replace(old, new) if query.count(old) == 1 else None


def _suggestion(
    code: str,
    description: str,
    fragment: str,
    example: str | None,
    changes_results: bool = True,
    **fields: list[str] | str,
) -> Suggestion:
    suggestion: dict[str, object] = {
        "code": code,
        "description": description,
        "fragment": fragment,
        "changes_results": changes_results,
        **fields,
    }
    if example is not None:
        suggestion["example"] = example
    return cast("Suggestion", suggestion)


# ------------------------------------------------------------------------------------------------ unbounded paths
def _bounded(path: str) -> str:
    """``path`` with an upper limit on its hops (a bare ``*`` is 1 to the limit)."""
    return re.sub(
        r"\[[^\[\]]*\*[^\[\]]*\]",
        lambda rel: _HOPS.sub(lambda hops: _limited(*hops.groups()), rel.group()),
        path,
    )


def _limited(low: str, dots: str | None, high: str) -> str:
    """The hops ``*low..high`` of a relationship with a limit: the one it has, or the example's."""
    if high or (not dots and low):
        return f"*{low}{dots or ''}{high}"
    return f"*{low or 1}..{max(_BOUND, int(low or 0))}"


def bound_paths(query: str) -> list[Suggestion]:
    """A limit for each path that has none: the text decides, with no database."""
    found = []
    for path in performance.written_unbounded_paths(query):
        bounded = _bounded(path)
        found.append(
            _suggestion(
                BOUND_PATH,
                f"The path {path} has no upper limit on its length, so it follows relationships as far as they go. "
                f"Give it one, as in {bounded}: choose the longest chain you expect.",
                path,
                _once(query, path, bounded),
            )
        )
    return found


# ------------------------------------------------------------------------------------------------------ labels
def _properties_read(text: str, variable: str) -> list[str]:
    """The properties of ``variable`` that the query reads (``n.born``) or filters by a map (``(n {born: 1})``)."""
    name = re.escape(variable)
    read = re.findall(rf"(?<![\w.]){name}\s*\.\s*(\w+)", text)
    for body in re.findall(rf"\(\s*{name}\s*\{{([^{{}}]*)\}}", text):
        read += re.findall(r"(\w+)\s*:", body)
    return list(dict.fromkeys(read))


def _has_property(graph: str, label: str, prop: str) -> Generator[Request, Any, bool]:
    return (yield Lookup("property_exists", graph, (label, "node", prop)))


def _labels_with(
    graph: str, properties: list[str]
) -> Generator[Request, Any, list[str]]:
    """The labels whose nodes have every one of ``properties``."""
    labels = yield from _queries.listed_names(graph, "labels")
    pairs = [(label, prop) for label in labels for prop in properties]
    answers = (
        (yield Concurrently(tuple(_has_property(graph, *pair) for pair in pairs)))
        if pairs
        else []
    )
    have: dict[str, bool] = dict.fromkeys(labels, True)
    for (label, _), answer in zip(pairs, answers, strict=True):
        have[label] = have[label] and bool(answer)
    return [label for label in labels if have[label]]


def _node_pattern(query: str, variable: str) -> str | None:
    """The node ``(n)`` or ``(n {..})`` of ``variable`` when the query writes it once."""
    found = list(
        re.finditer(
            rf"(?<![\w.])\(\s*{re.escape(variable)}\s*(?:\{{[^{{}}]*\}})?\s*\)", query
        )
    )
    return found[0].group() if len(found) == 1 else None


def _add_label_to(node: str, variable: str, label: str) -> str:
    return re.sub(rf"^\(\s*{re.escape(variable)}", f"({variable}:{_name(label)}", node)


def _label_suggestion(
    query: str, variable: str, properties: list[str], labels: list[str]
) -> Suggestion | None:
    if not labels:
        return None
    node = _node_pattern(query, variable)
    fragment = node or f"({variable})"
    props = ", ".join(properties)
    if len(labels) > 1:
        return _suggestion(
            ADD_LABEL,
            f"The node {variable} has no label, so FalkorDB looks at every node of the graph. Nodes that have "
            f"{props} have one of the labels {', '.join(labels)}: label the node with the one you mean.",
            fragment,
            None,
            labels=labels,
        )
    labelled = _add_label_to(fragment, variable, labels[0])
    return _suggestion(
        ADD_LABEL,
        f"The node {variable} has no label, so FalkorDB looks at every node of the graph. Only {labels[0]} nodes "
        f"have {props}, so label it, as in {labelled}. Nodes with another label would no longer match.",
        fragment,
        _once(query, fragment, labelled) if node else None,
        labels=labels,
    )


def label_suggestions(
    graph: str, root: Op, query: str
) -> Generator[Request, Any, list[Suggestion]]:
    """A label for each node that is scanned among all the nodes and filtered."""
    text = patterns.strip_literals_and_comments(query)
    found = []
    for variable in performance.unlabeled_variables(root, query):
        properties = _properties_read(text, variable)
        labels = yield from _labels_with(graph, properties)
        suggestion = _label_suggestion(query, variable, properties, labels)
        if suggestion:
            found.append(suggestion)
    return found


# ----------------------------------------------------------------------------------------------- connect patterns
def _label_of(text: str, variable: str) -> str | None:
    """The first label the query gives to ``variable``."""
    match = re.search(rf"\(\s*{re.escape(variable)}\s*:\s*(\w+)", text)
    return match[1] if match else None


def _labelled_node(query: str, variable: str) -> str | None:
    match = re.search(rf"\(\s*{re.escape(variable)}\s*:[^()]*\)", query)
    return match.group() if match else None


def _connects(
    graph: str, first: str, second: str, types: tuple[str, ...]
) -> Generator[Request, Any, list[tuple[str, str]]]:
    """``(type, arrow)`` for each relationship between the two labels, written from ``first`` to ``second``:
    ``->`` when it points from ``first``, ``<-`` when it points to it."""
    ways = [(kind, arrow) for kind in types for arrow in ("->", "<-")]

    def exists(kind: str, arrow: str) -> Generator[Request, Any, bool]:
        left, right = ("", ">") if arrow == "->" else ("<", "")
        path = f"(:{_name(first)}){left}-[:{_name(kind)}]-{right}(:{_name(second)})"
        return (yield Lookup("path_exists", graph, (path,)))

    answers = (yield Concurrently(tuple(exists(*way) for way in ways))) if ways else []
    return [way for way, there in zip(ways, answers, strict=True) if there]


def _written_with(kind: str, arrow: str) -> str:
    rel = f"[:{_name(kind)}]"
    return f"-{rel}->" if arrow == "->" else f"<-{rel}-"


def _connect_suggestion(
    query: str, nodes: tuple[str, str], ways: list[tuple[str, str]]
) -> Suggestion:
    first, second = nodes
    types = list(dict.fromkeys(kind for kind, _ in ways))
    fragment = f"{first}, {second}"
    adjacent = re.search(rf"{re.escape(first)}\s*,\s*{re.escape(second)}", query)
    if not ways:
        return _suggestion(
            CONNECT_PATTERNS,
            f"The patterns {first} and {second} have nothing in common, so every row of one is paired with every "
            "row of the other, and no relationship of the graph connects the two labels. Ask for them in two "
            "queries, or add the relationship the graph is missing.",
            fragment,
            None,
            relationship_types=[],
        )
    example = None
    if len(ways) == 1 and adjacent:
        joined = f"{first}{_written_with(*ways[0])}{second}"
        example = _once(query, adjacent.group(), joined)
    return _suggestion(
        CONNECT_PATTERNS,
        f"The patterns {first} and {second} have nothing in common, so every row of one is paired with every row "
        f"of the other. The graph connects them with {', '.join(types)}: write the relationship between them.",
        adjacent.group() if adjacent else fragment,
        example,
        relationship_types=types,
    )


def connect_suggestions(
    graph: str, root: Op, query: str
) -> Generator[Request, Any, list[Suggestion]]:
    """A relationship for each pair of patterns that are a product, when the graph has one between their labels."""
    text = patterns.strip_literals_and_comments(query)
    pairs = []
    for groups in performance.cartesian_groups(root, query):
        firsts = [group[0] for group in groups if group]
        for index, one in enumerate(firsts):
            pairs += [(one, other) for other in firsts[index + 1 :]]
    types = yield from _queries.listed_names(graph, "relationship_types")
    found = []
    for one, other in dict.fromkeys(pairs):
        first, second = _label_of(text, one), _label_of(text, other)
        nodes = (_labelled_node(query, one), _labelled_node(query, other))
        if first is None or second is None or nodes[0] is None or nodes[1] is None:
            continue
        ways = yield from _connects(graph, first, second, types)
        found.append(_connect_suggestion(query, (nodes[0], nodes[1]), ways))
    return found


# ------------------------------------------------------------------------------------------------------ indexes
def _label_scans(root: Op) -> list[tuple[str, str]]:
    """``(variable, label)`` of each label scan that a filter works on (an index scan is not one)."""
    found = []
    for op in root.walk():
        match = re.fullmatch(r"\((\w+):([^()]+)\)", op.detail)
        if (
            op.name == "Node By Label Scan"
            and match
            and any(a.name == "Filter" for a in op.ancestors())
        ):
            found.append((match[1], match[2]))
    return found


def _has_index(graph: str, label: str, prop: str) -> Generator[Request, Any, bool]:
    return (yield Lookup("index_exists", graph, ("range", "nodes", label, prop)))


def _wrapped_properties(query: str, variable: str) -> list[tuple[str, str, str]]:
    """``(fragment, function, property)`` for each comparison of ``function(variable.property)`` in the query."""
    pattern = _WRAPPED.format(variable=re.escape(variable))
    return [
        (match.group(), match[1], match[2]) for match in re.finditer(pattern, query)
    ]


def _index_suggestion(
    query: str, label: str, variable: str, wrapped: tuple[str, str, str]
) -> Suggestion:
    fragment, function, prop = wrapped
    direct = f"{variable}.{prop}"
    return _suggestion(
        USE_INDEX,
        f"FalkorDB cannot use the index on :{label}({prop}) because the query compares {function}({direct}) and "
        f"not the property itself. Compare it directly, as in {direct}, or store the value in the form you "
        "search for.",
        fragment,
        _once(query, fragment, direct),
    )


def index_suggestions(
    graph: str, root: Op, query: str
) -> Generator[Request, Any, list[Suggestion]]:
    """A direct comparison for each property that has an index and is compared through a function."""
    candidates = [
        (variable, label, wrapped)
        for variable, label in _label_scans(root)
        for wrapped in _wrapped_properties(query, variable)
    ]
    if not candidates:
        return []
    indexed = yield Concurrently(
        tuple(_has_index(graph, label, wrapped[2]) for _, label, wrapped in candidates)
    )
    return [
        _index_suggestion(query, label, variable, wrapped)
        for (variable, label, wrapped), there in zip(candidates, indexed, strict=True)
        if there
    ]


# ------------------------------------------------------------------------------------------------ missing indexes
_COMPARED = (
    r"(?<![\w.]){variable}\s*\.\s*(\w+|`[^`]+`)"
    r"(?=\s*(?:=(?![=~])|<=|>=|<(?!>)|>|\bIN\b))"
)


def _compared_properties(text: str, variable: str, label: str) -> list[tuple[str, str]]:
    """``(fragment, property)`` for each property of ``variable`` that the query compares with ``=``, a bound or
    ``IN``, or filters by a map of the node (``(m:Movie {year: 2})``): what a range index serves."""
    name = re.escape(variable)
    found = [
        (text[match.start() : match.end()], unquote_name(match[1]))
        for match in re.finditer(_COMPARED.format(variable=name), text)
    ]
    maps = rf"\(\s*{name}\s*:\s*{re.escape(label)}[^(){{}}]*\{{([^{{}}]*)\}}"
    for body in re.findall(maps, text):
        found += [
            (key, unquote_name(key)) for key in re.findall(r"(\w+|`[^`]+`)\s*:", body)
        ]
    return found


def missing_index_suggestions(
    graph: str, root: Op, query: str
) -> Generator[Request, Any, list[Suggestion]]:
    """The index that would serve each property that a label scan filters on, when the graph has none."""
    text = patterns.strip_literals_and_comments(query)
    candidates = list(
        dict.fromkeys(
            (variable, label, fragment, prop)
            for variable, label in _label_scans(root)
            for fragment, prop in _compared_properties(text, variable, label)
        )
    )
    if not candidates:
        return []
    indexed = yield Concurrently(
        tuple(_has_index(graph, label, prop) for _, label, _, prop in candidates)
    )
    found: dict[tuple[str, str], Suggestion] = {}
    for (variable, label, fragment, prop), there in zip(
        candidates, indexed, strict=True
    ):
        if not there:
            found.setdefault(
                (label, prop), _missing_index(variable, label, fragment, prop)
            )
    return list(found.values())


def _missing_index(variable: str, label: str, fragment: str, prop: str) -> Suggestion:
    statement = (
        f"CREATE INDEX FOR ({variable}:{_name(label)}) ON ({variable}.{_name(prop)})"
    )
    return _suggestion(
        MISSING_INDEX,
        f"FalkorDB reads every :{label} node to find the ones with {prop} that the query asks for, because there "
        f"is no index on :{label}({prop}). If :{label} is large, create one: {statement}. An index costs memory "
        "and a little time on every write, and never changes what a query returns.",
        fragment,
        None,
        False,
        statement=statement,
    )


# ------------------------------------------------------------------------------------------------------- a limit
_RETURNS_ALL_ROWS = {
    "Limit",
    "Aggregate",
    "Distinct",
    "Create",
    "Merge",
    "Update",
    "Delete",
}


def _last_return(query: str) -> str | None:
    """The last ``RETURN`` clause of the query as written, to its end."""
    body = query.rstrip().removesuffix(";").rstrip()
    match = re.search(r"(?is)\bRETURN\b(?!.*\bRETURN\b).*$", body)
    return match.group() if match else None


def limit_suggestions(root: Op, query: str) -> list[Suggestion]:
    """A limit for a query that reads rows of the graph and returns every one of them."""
    reads = any("Scan" in leaf.name for leaf in root.leaves())
    returns_all = (
        root.name == "Results"
        and not {op.name for op in root.walk()} & _RETURNS_ALL_ROWS
    )
    text = patterns.strip_literals_and_comments(query)
    clause = _last_return(query)
    if not (reads and returns_all and clause) or len(patterns.split_union(text)) > 1:
        return []
    body = query.rstrip().removesuffix(";").rstrip()
    example = f"{body}\nLIMIT {_LIMIT}"
    return [
        _suggestion(
            ADD_LIMIT,
            "The query returns every row it finds, so on a large graph the result can be very large. Add a limit "
            f"at the end, as in LIMIT {_LIMIT}: the number is an example, choose the one you need.",
            clause,
            example,
        )
    ]


# ------------------------------------------------------------------------------------------------------ the advice
def advise(
    graph: str, query: str, *, suggest_limit: bool = False
) -> Generator[Request, Any, list[Suggestion]]:
    """The suggestions for ``query``: none when the database rejects it (there is nothing to advise on yet).
    A limit is only suggested when ``suggest_limit`` is on: it is advice about any query that returns rows."""
    try:
        reply = yield from _queries.plan(graph, query)
    except GraphNotFoundError:
        raise
    except CypherError:
        return []
    root = parse(reply)
    found = bound_paths(query)
    if root is None:
        return found
    for suggestions in (
        yield Concurrently(
            (
                label_suggestions(graph, root, query),
                connect_suggestions(graph, root, query),
                index_suggestions(graph, root, query),
                missing_index_suggestions(graph, root, query),
            )
        )
    ):
        found += suggestions
    return found + (limit_suggestions(root, query) if suggest_limit else [])
