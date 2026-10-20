"""The plan of a query, as ``GRAPH.EXPLAIN`` prints it: one operation to a line, a child indented four spaces deeper
than its parent, and what an operation works on after a bar::

    Results
        Project
            Cartesian Product
                Node By Label Scan | (a:Person)
                Node By Label Scan | (b:Movie)

The plan is FalkorDB's own and its words may change, so everything that reads it is tolerant: a line that is not
understood is left out, and what is not found is not reported.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field

import regex as re

_INDENT = 4
# the variables an operation works on: `(a:Person)`, `(a)->(b)`, `(a)-[r:KNOWS*1..INF]->(b)`; `@anon_0` is a name
# FalkorDB made up for what the query leaves anonymous
_VARIABLE = re.compile(r"[(\[]\s*(@?[A-Za-z_]\w*)")


@dataclass
class Op:
    """One operation of a plan, with the operations that feed it."""

    name: str
    detail: str = ""
    children: list["Op"] = field(default_factory=list)
    parent: "Op | None" = field(default=None, repr=False, compare=False)

    def walk(self) -> Iterator["Op"]:
        """This operation and every one below it, parents first."""
        yield self
        for child in self.children:
            yield from child.walk()

    def leaves(self) -> list["Op"]:
        """The operations that read from no other: the scans."""
        return [op for op in self.walk() if not op.children]

    def ancestors(self) -> Iterator["Op"]:
        """The operations above this one, nearest first."""
        op = self.parent
        while op is not None:
            yield op
            op = op.parent

    @property
    def variables(self) -> list[str]:
        """The named variables of its detail, in order and once each (an anonymous element has none)."""
        names = (match[1] for match in _VARIABLE.finditer(self.detail))
        return list(dict.fromkeys(name for name in names if not name.startswith("@")))


def parse(lines: object) -> Op | None:
    """The tree of the operations of ``lines`` (the reply of ``GRAPH.EXPLAIN``), or None if there is no plan in it."""
    if not isinstance(lines, list | tuple):
        return None
    root: Op | None = None
    stack: list[tuple[int, Op]] = []
    for raw in lines:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if not isinstance(text, str) or not text.strip():
            continue
        depth = (len(text) - len(text.lstrip(" "))) // _INDENT
        name, _, detail = text.strip().partition("|")
        op = Op(name.strip(), detail.strip())
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if stack:
            op.parent = stack[-1][1]
            stack[-1][1].children.append(op)
        elif root is None:
            root = op
        else:
            continue  # a second root: not a plan this reads
        stack.append((depth, op))
    return root
