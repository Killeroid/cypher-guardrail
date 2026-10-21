"""Queries that are valid and may be expensive: what the plan of FalkorDB says of them.

Each check is a heuristic, which is why each can be switched off (``check_performance``). They read the plan
(``GRAPH.EXPLAIN``) and so are exact about what the database will do, and they are warnings: the query is still valid.
"""

from collections.abc import Iterable

import regex as re

from cypher_guardrail import patterns
from cypher_guardrail._internal.core import builders as _issues
from cypher_guardrail._internal.core import plans
from cypher_guardrail.issues import Issue

CARTESIAN_PRODUCT = "cartesian_product"
UNLABELED_SCAN = "unlabeled_scan"
UNBOUNDED_PATH = "unbounded_path"
CHECKS = (CARTESIAN_PRODUCT, UNLABELED_SCAN, UNBOUNDED_PATH)


def selected(option: bool | Iterable[str] | None) -> frozenset[str]:
    """The checks that ``check_performance`` asks for: True is all of them, a collection of names is those.

    Raises:
        TypeError: If it is a string (one name is a collection of one: ``{"unbounded_path"}``) or not iterable.
        ValueError: If a name is not one of ``CHECKS``.
    """
    if option is None or option is False:
        return frozenset()
    if option is True:
        return frozenset(CHECKS)
    if isinstance(option, str) or not isinstance(option, Iterable):
        raise TypeError(
            f"check_performance is True, False or a collection of names, not {option!r}"
        )
    names = frozenset(option)
    if unknown := sorted(names - set(CHECKS)):
        raise ValueError(
            f"Unknown performance checks {unknown}: the checks are {', '.join(CHECKS)}"
        )
    return names


def warnings(query: str, plan: object, checks: frozenset[str]) -> list[Issue]:
    """The warnings of the checks that are on, for ``query`` and its plan (the reply of ``GRAPH.EXPLAIN``)."""
    root = plans.parse(plan)
    if root is None or not checks:
        return []
    found: list[Issue] = []
    if CARTESIAN_PRODUCT in checks:
        found += _cartesian_products(root, query)
    if UNLABELED_SCAN in checks:
        found += _unlabeled_scans(root, query)
    if UNBOUNDED_PATH in checks:
        found += _unbounded_paths(root, query)
    return found


def _cartesian_products(root: plans.Op, query: str) -> list[Issue]:
    return [
        _issues.cartesian_product(groups) for groups in cartesian_groups(root, query)
    ]


def cartesian_groups(root: plans.Op, query: str) -> list[list[list[str]]]:
    """The products of patterns that have nothing in common, each as the named variables of its patterns, in the
    order the query writes them. One that is made of index scans only is left alone: it is the lookup of a few rows
    each (``MATCH (a:P {id: 1}), (b:P {id: 2})``)."""
    found: dict[tuple[tuple[str, ...], ...], list[list[str]]] = {}
    for op in root.walk():
        if op.name != "Cartesian Product":
            continue
        if all(
            leaf.name == "Node By Index Scan"
            for branch in op.children
            for leaf in branch.leaves()
        ):
            continue
        groups = sorted(
            (
                sorted(
                    {v for inner in branch.walk() for v in inner.variables},
                    key=lambda name: _first_written(query, name),
                )
                for branch in op.children
            ),
            key=lambda group: _first_written(query, group[0]) if group else 0,
        )
        found.setdefault(tuple(map(tuple, groups)), groups)
    return list(found.values())


def _first_written(query: str, variable: str) -> int:
    """Where the query first writes ``variable`` (the plan lists the variables in the order it likes)."""
    match = re.search(rf"(?<![\w.]){re.escape(variable)}(?!\w)", query)
    return match.start() if match else len(query)


def _reads_property(text: str, variable: str) -> bool:
    """Whether the query reads a property of ``variable`` (``n.name``) or filters it by a map (``(n {name: 'x'})``)."""
    name = re.escape(variable)
    return bool(
        re.search(rf"(?<![\w.]){name}\s*\.\s*\w", text)
        or re.search(rf"\(\s*{name}\s*\{{", text)
    )


def _unlabeled_scans(root: plans.Op, query: str) -> list[Issue]:
    return [_issues.unlabeled_node_scan(v) for v in unlabeled_variables(root, query)]


def unlabeled_variables(root: plans.Op, query: str) -> list[str]:
    """The nodes with no label that are looked for among all the nodes of the graph and filtered on a property."""
    text = patterns.strip_literals_and_comments(query)
    found: list[str] = []
    for op in root.walk():
        if op.name != "All Node Scan" or not op.variables:
            continue
        variable = op.variables[0]
        filtered = any(a.name == "Filter" for a in op.ancestors())
        if filtered and _reads_property(text, variable) and variable not in found:
            found.append(variable)
    return found


_REL = re.compile(r"\[[^\[\]]*\*[^\[\]]*\]")
_HOPS = re.compile(r"\*\s*(\d*)\s*(\.\.)?\s*(\d*)")


def _unbounded(rel: str) -> bool:
    """Whether the hops of a relationship pattern (``[:R*]``, ``[:R*2..]``) have no upper limit."""
    match = _HOPS.search(rel)
    if match is None:
        return False
    low, dots, high = match.groups()
    return not high if dots else not low


_SHORTEST = re.compile(r"\b(?:all)?shortestpaths?\s*\(", re.IGNORECASE)


def _without_shortest_paths(text: str) -> str:
    """``text`` without the arguments of ``shortestPath(...)`` and ``allShortestPaths(...)``: FalkorDB searches
    those breadth first, whatever the hops say, so they are not unbounded."""
    kept, position = [], 0
    for call in _SHORTEST.finditer(text):
        if call.start() < position:
            continue
        kept.append(text[position : call.start()])
        depth, position = 1, call.end()
        while position < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[position], 0)
            position += 1
    return "".join(kept) + text[position:]


def written_unbounded_paths(query: str) -> list[str]:
    """The variable-length paths with no upper limit, as the query writes them (from the text alone)."""
    text = _without_shortest_paths(patterns.strip_literals_and_comments(query))
    return list(
        dict.fromkeys(
            path.strip()
            for path in patterns.PATH_VARIABLE_LENGTH.findall(text, overlapped=True)
            if any(_unbounded(rel) for rel in _REL.findall(path))
        )
    )


def _unbounded_paths(root: plans.Op, query: str) -> list[Issue]:
    """A variable-length path with no upper limit, as the query writes it (the plan says there is one: ``*1..INF``)."""
    unbounded = [
        op
        for op in root.walk()
        if op.name.endswith("Variable Length Traverse") and "INF" in op.detail
    ]
    if not unbounded:
        return []
    paths = written_unbounded_paths(query) or [op.detail for op in unbounded[:1]]
    return [_issues.unbounded_path(path) for path in paths]
