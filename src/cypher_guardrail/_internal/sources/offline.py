"""Answering the lookups of the logic (``cypher_guardrail._internal.core.io.Lookup``) from a ``Snapshot``, without asking the database.

``SnapshotResolver.answer`` is pure: it takes a lookup and returns a :class:`Resolution`. ``SnapshotResolver.resolve``
wraps it as a resolver for the drivers (see ``cypher_guardrail._internal.core.io``) that answers with the plain value, the way the online
resolver (``cypher_guardrail._internal.sources.online``) does. Where the database would raise a ``CypherError`` (it cannot parse ``(:A|B)``, a
label with a space in it, ``x.2020``), so does this.

A snapshot only knows the *kinds* of nodes (the sets of labels that occur together) and which kinds are connected by
which relationship types. That makes every one-hop question exact. Two things it cannot know, and so reports as a
"maybe" (``reliable=False``), always together with a value of ``True`` (a "no" is always certain, as long as the
snapshot is complete):

* a path of 2 or more hops that exists between kinds of nodes, but may not exist between real nodes (one node of a
  kind receives the relationship, another node of the same kind sends the next one);
* a property value (``{code: '1'}``), or the same variable used twice in a path (the same real node on both ends): the
  kinds fit, the data may not.

A snapshot that is a sample (``Snapshot.sample``) is not complete, so its "no" is a "maybe" as well: a path or a
property it lacks may just not be in the sample, and is reported as ``True`` (``sampled=True``), so that nothing is
reported against a sample. The names are not sampled (they come from the schema procedures): an unknown name is
unknown.

A pattern this parser does not read (a back-quoted name, a brace in a string, ...) is a "maybe" as well, with ``True``:
nothing is known against it. A name that is not ASCII, outside back-quotes, is a syntax error here as it is in FalkorDB.
"""

from contextlib import suppress
from dataclasses import dataclass
from enum import IntEnum
from itertools import product
from typing import Any

import pandas as pd
import regex as re

from cypher_guardrail._internal.core.errors import SYNTAX_ERROR, CypherError
from cypher_guardrail._internal.core.io import Lookup
from cypher_guardrail._internal.core.valuetypes import fits
from cypher_guardrail._internal.sources import online as _online
from cypher_guardrail.patterns import strip_literals_and_comments, unquote_name
from cypher_guardrail.snapshot import Snapshot


class Fit(IntEnum):
    """How well a pattern fits what the snapshot knows."""

    NO = 0  # certainly not: a "no" is certain, as long as the snapshot is complete
    MAYBE = 1  # the kinds fit, the data may not (a property value, a variable used twice, a chain of hops)
    CERTAIN = 2  # the kinds fit and nothing else is in question


# A chain of this many hops or more is only a "maybe": its hops may be of different real nodes
MIN_CHAIN_HOPS = 2


@dataclass(frozen=True)
class Resolution:
    """What the snapshot says about a lookup.

    Attributes:
        value: What the database would have answered.
        reliable: False when ``value`` is only a "maybe" (see the module docstring).
        miss: True when the snapshot says the thing is not there (an unknown name, a path or a property that does
            not exist, an empty table).
        sampled: True when the snapshot is a sample and says the thing is not there: it may just not be in the sample, so
            this is no answer. ``value`` is then the answer that raises no alarm (``True``), or the one the snapshot
            has (a table); the hybrid mode asks the database, unless ``verify="never"``.
    """

    value: Any
    reliable: bool = True
    miss: bool = False
    sampled: bool = False


# ------------------------------------------------------------------------------ parsing a pattern
# ASCII only, as FalkorDB's names are when they are not back-quoted (`MATCH (:Ünï)` is a syntax error there)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# a name in a pattern: a word, or what is between back-quotes (which may be anything but a back-quote)
_WORD = re.compile(r"`([^`]*)`|\w+", re.ASCII)


def _names(text: str) -> tuple[str, ...]:
    """The names in ``text``: words, and what is between back-quotes, without them."""
    return tuple(
        m.group(1) if m.group(1) is not None else m.group()
        for m in _WORD.finditer(text)
    )


_NODE = re.compile(
    r"""\(\s*(?P<variable>\w*)\s*
        (?P<labels>(?::\s*(?:\w+|`[^`]*`)\s*)*)
        (?P<alternatives>(?:\|\s*(?:\w+|`[^`]*`)\s*)*)
        (?P<properties>\{[^}]*\})?\s*\)""",
    re.VERBOSE | re.ASCII,
)
_RELATIONSHIP = re.compile(
    r"""\[\s*(?P<variable>\w*)\s*
        (?::\s*(?P<types>(?:\w+|`[^`]*`)(?:\s*\|\s*:?\s*(?:\w+|`[^`]*`))*))?\s*
        (?P<length>\*[^\]{]*)?\s*
        (?P<properties>\{[^}]*\})?\s*\]""",
    re.VERBOSE | re.ASCII,
)
_ARROW = re.compile(
    r"\s*(?P<left><)?\s*-\s*(?P<relationship>\[[^\]]*\])?\s*-\s*(?P<right>>)?\s*"
)
_LENGTH = re.compile(r"^\*\s*(?:(?P<lo>\d+)?\s*(?P<dots>\.\.)?\s*(?P<hi>\d+)?)?$")
# a key is a word or a back-quoted name: {code: 1}, {`the code`: 1}
_PROPERTY_KEY = re.compile(r"(?:^|[{,])\s*(?:(\w+)|`([^`]*)`)\s*:", re.ASCII)


@dataclass(frozen=True)
class _Node:
    variable: str
    labels: tuple[str, ...]
    keys: tuple[str, ...]  # the property names of a {map}


@dataclass(frozen=True)
class _Relationship:
    variable: str
    types: tuple[str, ...]  # empty: any
    keys: tuple[str, ...]
    lo: int
    hi: int | None  # None: no limit


@dataclass(frozen=True)
class _Pattern:
    left: _Node
    relationship: _Relationship | None
    direction: str  # "->", "<-" or "-"
    right: _Node | None


def _keys(properties: str | None) -> tuple[str, ...]:
    if not properties:
        return ()
    # Strings are blanked first: a ':' inside a value is not a key
    return tuple(
        word or quoted
        for word, quoted in _PROPERTY_KEY.findall(
            strip_literals_and_comments(properties)
        )
    )


def _node(match) -> _Node:
    if match["alternatives"]:
        # FalkorDB has no (:A|B): it is a syntax error there
        raise CypherError(
            SYNTAX_ERROR,
            f"Invalid input '|': alternatives of labels in {match.group()}",
        )
    labels = _names(match["labels"])
    return _Node(match["variable"], labels, _keys(match["properties"]))


def _parse_length(text: str | None) -> tuple[int, int | None] | None:
    """``*``, ``*2``, ``*2..``, ``*..3``, ``*1..3`` as ``(lowest, highest)``; None if it cannot be read."""
    if text is None:
        return 1, 1
    match = _LENGTH.match(text.strip())
    if match is None:
        return None
    lo, dots, hi = match["lo"], match["dots"], match["hi"]
    if not dots:
        if lo is None:
            return 1, None  # *
        return int(lo), int(lo)  # *N
    return (int(lo) if lo else 1), (int(hi) if hi else None)


def _parse_relationship(text: str) -> _Relationship | None:
    match = _RELATIONSHIP.fullmatch(text)
    if match is None:
        return None
    length = _parse_length(match["length"])
    if length is None:
        return None
    types = _names(match["types"] or "")
    return _Relationship(
        match["variable"], types, _keys(match["properties"]), length[0], length[1]
    )


def _reject_names_outside_ascii(text: str) -> None:
    """FalkorDB takes only ASCII names unless they are back-quoted (strings and comments may hold anything)."""
    outside = re.sub(r"`[^`]*`", "", strip_literals_and_comments(text))
    if not outside.isascii():
        raise CypherError(
            SYNTAX_ERROR,
            f"Invalid input in {text!r}: a name that is not ASCII must be between back-quotes",
        )


def _parse(pattern: str) -> _Pattern | None:
    """A node, or a node, a relationship and a node (one hop, perhaps of variable length). None if it is neither.

    Raises:
        CypherError: For what the database cannot parse (alternatives of labels, names that are not ASCII).
    """
    _reject_names_outside_ascii(pattern)
    text = pattern.strip()
    first = _NODE.match(text)
    if first is None:
        return None
    left = _node(first)
    rest = text[first.end() :]
    if not rest.strip():
        return _Pattern(left, None, "-", None)
    arrow = _ARROW.match(rest)
    if arrow is None or (arrow["left"] and arrow["right"]):
        return None
    if arrow["relationship"]:
        relationship = _parse_relationship(arrow["relationship"])
        if relationship is None:
            return None
    else:
        relationship = _Relationship("", (), (), 1, 1)
    second = _NODE.fullmatch(rest[arrow.end() :].strip())
    if second is None:
        return None
    right = _node(second)
    direction = "<-" if arrow["left"] else "->" if arrow["right"] else "-"
    return _Pattern(left, relationship, direction, right)


# ------------------------------------------------------------------------------ the resolver
State = frozenset  # a set of labels: one kind of node


class SnapshotResolver:
    """Answers lookups from a :class:`~cypher_guardrail.snapshot.Snapshot`."""

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot
        self._edges = [
            (State(start), kind, State(end)) for start, kind, end in snapshot.edges
        ]
        self._states = frozenset(
            {State(labels) for labels in snapshot.node_label_sets}
            | {state for start, _, end in self._edges for state in (start, end)}
        )
        self._known_names = {
            "labels": frozenset(snapshot.labels),
            "relationship_types": frozenset(snapshot.relationship_types),
            "property_keys": frozenset(snapshot.property_keys),
        }
        self._answers = {
            "missing_names": self._missing_names,
            "index_exists": self._index_exists,
            "path_exists": self._path_exists,
            "property_exists": self._property_exists,
            "property_types": self._property_types,
            "multilabels": self._multilabels,
            "label_combinations": self._label_combinations,
        }

    # ---------------------------------------------------------------- the interface
    def answer(self, lookup: Lookup) -> Resolution:
        """What the snapshot says about ``lookup``.

        Raises:
            ValueError: If the lookup is of a kind that is not known.
            CypherError: Where the database would raise it.
        """
        try:
            answer = self._answers[lookup.kind]
        except KeyError:
            raise ValueError(f"Unknown lookup: {lookup.kind!r}") from None
        return answer(*lookup.args)

    def resolve(self, lookup: Lookup):
        """A resolver for the drivers: a logic generator that returns the plain value of :meth:`answer`."""
        return self._resolved(lookup)

    def _resolved(self, lookup: Lookup):
        return self.answer(lookup).value
        yield  # the unreachable yield makes this a generator that asks for nothing

    # ------------------------------------------------------------------------ names
    def _names(self, kind: str) -> frozenset[str]:
        return self._known_names[kind]

    def _missing_names(self, kind: str, names: tuple[str, ...]) -> Resolution:
        known = self._names(kind)
        missing = tuple(name for name in names if name not in known)
        return Resolution(missing, miss=bool(missing))

    def _index_exists(
        self, kind: str, on: str, label: str, attribute: str | None
    ) -> Resolution:
        entity = "NODE" if on == "nodes" else "RELATIONSHIP"
        exists = any(
            index_label == label
            and index_entity == entity
            and any(
                kind.upper() in types
                for name, types in attributes
                if attribute is None or name == attribute
            )
            for index_label, index_entity, attributes in self.snapshot.indexes
        )
        return Resolution(exists, miss=not exists)

    def _multilabels(self) -> Resolution:
        return Resolution(
            [
                list(labels)
                for labels in self.snapshot.node_label_sets
                if len(labels) > 1
            ]
        )

    def _property_exists(self, label: str, kind: str, prop: str) -> Resolution:
        # The database is asked `MATCH (x:Label) WHERE (x.prop) IS NOT NULL` and cannot parse a name that is
        # not an identifier
        # (a property key between back-quotes is written with them, and may be any name)
        name = unquote_name(prop)
        for text in (label,) if name != prop else (label, prop):
            if not _IDENTIFIER.match(text):
                raise CypherError(SYNTAX_ERROR, f"Invalid input: {text!r}")
        properties = (
            self.snapshot.node_properties_of(label)
            if kind == "node"
            else self.snapshot.relationship_properties_of(label)
        )
        exists = name in properties
        # (a sample may lack a property that a known label has; a key that is not a key of the graph is not there)
        if (
            not exists
            and self.snapshot.sample is not None
            and name in self._names("property_keys")
            and (
                label in self._names("labels")
                or label in self._names("relationship_types")
            )
        ):
            return Resolution(True, reliable=False, miss=True, sampled=True)
        return Resolution(exists, miss=not exists)

    def _property_types(
        self, label: str, kind: str, prop: str, literal_type: str
    ) -> Resolution:
        if not self.snapshot.has_property_types:
            # Nothing was recorded: no answer to give (the validators report nothing), and the database, if
            # there is one, is asked
            return Resolution(None, reliable=False)
        # (the database asks for the types with the names between back-quotes, so any name can be asked)
        entity = "NODE" if kind == "node" else "RELATIONSHIP"
        types = self.snapshot.property_types_of(label, entity).get(prop, ())
        # A type that was not seen is a "miss": a snapshot lacks what was added since, so the database is asked
        # before a mismatch is reported
        return Resolution(types, miss=not fits(literal_type, types))

    # ------------------------------------------------------------------------ paths
    def _fits(self, state: State, node: _Node) -> Fit:
        """How well a kind of node fits a node of a pattern (properties make it a "maybe")."""
        if not set(node.labels) <= state:
            return Fit.NO
        if not node.keys:
            return Fit.CERTAIN
        for key in node.keys:
            if state:
                if not all(
                    key in self.snapshot.node_properties_of(label) for label in state
                ):
                    return Fit.NO
            elif key not in self._names("property_keys"):
                return Fit.NO
        return Fit.MAYBE

    def _edge_fits(self, kind: str, relationship: _Relationship) -> Fit:
        if relationship.types and kind not in relationship.types:
            return Fit.NO
        if not relationship.keys:
            return Fit.CERTAIN
        properties = self.snapshot.relationship_properties_of(kind)
        if all(key in properties for key in relationship.keys):
            return Fit.MAYBE
        return Fit.NO

    def _oriented(self, pattern: _Pattern):
        """The edges the relationship of the pattern may follow, as ``(from, type, to)`` in the order of the pattern."""
        for start, kind, end in self._edges:
            if pattern.direction in ("->", "-"):
                yield start, kind, end
            if pattern.direction in ("<-", "-"):
                yield end, kind, start

    def _hops(self, pattern: _Pattern, settle: bool = False):
        """Every way the pattern matches between kinds of nodes: ``(left kind, right kind, type, how well)``.

        ``how well`` is 2 for a match that exists between real nodes whenever the snapshot is right, 1 for a
        "maybe". The type is None for a match of no hop or of several. The matches of 2 hops or more come last, and
        are all "maybe"s: with ``settle`` the search ends at the first of them, for a caller that only asks whether
        there is one.
        """
        left, right = pattern.left, pattern.right
        if right is None:
            for state in self._states:
                fit = self._fits(state, left)
                if fit:
                    yield state, state, None, fit
            return
        relationship = pattern.relationship
        assert relationship is not None
        # the same variable on both ends is the same node
        same_node = bool(left.variable) and left.variable == right.variable
        cap = Fit.MAYBE if same_node else Fit.CERTAIN

        def end_fit(a, b, quality):
            if same_node and a != b:
                return Fit.NO
            return min(self._fits(a, left), self._fits(b, right), quality, cap)

        lo, hi = relationship.lo, relationship.hi
        if lo == 0:
            for state in self._states:
                # one node is both ends: it must fit both nodes of the pattern
                fit = end_fit(state, state, Fit.CERTAIN)
                if fit:
                    yield state, state, None, fit
        if (hi is None or hi >= 1) and lo <= 1:
            for a, kind, b in self._oriented(pattern):
                fit = end_fit(a, b, self._edge_fits(kind, relationship))
                if fit:
                    yield a, b, kind, fit
        if hi is None or hi >= MIN_CHAIN_HOPS:
            yield from self._chains(pattern, end_fit, first_only=settle)

    def _chains(self, pattern: _Pattern, end_fit, first_only: bool = False):
        """The matches of 2 or more hops between kinds of nodes, each pair of kinds once. They are all "maybe"s."""
        relationship = pattern.relationship
        assert relationship is not None
        lo, hi = max(relationship.lo, MIN_CHAIN_HOPS), relationship.hi
        steps: dict[State, set[State]] = {}
        for a, kind, b in self._oriented(pattern):
            if self._edge_fits(kind, relationship):
                steps.setdefault(a, set()).add(b)
        # A walk of more than `lo` + (number of kinds) hops can be shortened by a loop and still be long enough
        limit = hi if hi is not None else lo + len(self._states)
        for start in self._states:
            if not self._fits(start, pattern.left):
                continue
            frontier = {start}
            reached: set[State] = set()  # the ends already given for this start
            layers: set[frozenset] = set()  # the frontiers of the hops from `lo` on
            for hops in range(1, limit + 1):
                frontier = {b for a in frontier for b in steps.get(a, ())}
                if not frontier:
                    break
                if hops >= lo:
                    for end in frontier - reached:
                        if end_fit(start, end, Fit.MAYBE):
                            yield start, end, None, Fit.MAYBE
                            if first_only:
                                return
                    reached |= frontier
                    # The next frontier follows from this one: if it has been seen, the walk goes round, and
                    # no end it reaches from here on is new
                    if frozenset(frontier) in layers:
                        break
                    layers.add(frozenset(frontier))

    def _path_exists(self, pattern: str) -> Resolution:
        parsed = _parse(pattern)
        if parsed is None:
            # Not a node or a one-hop path this reads: nothing is known against it, so a maybe (never a "no")
            return Resolution(True, reliable=False)
        best = Fit.NO
        for *_, fit in self._hops(parsed, settle=True):
            best = max(best, fit)
            if best == Fit.CERTAIN:  # nothing that follows can say more
                break
        if best == 0 and self._may_be_missing_from_the_sample(parsed):
            return Resolution(True, reliable=False, miss=True, sampled=True)
        return Resolution(best > 0, reliable=best != 1, miss=best == 0)

    def _may_be_missing_from_the_sample(self, pattern: _Pattern) -> bool:
        """Whether a pattern that the snapshot does not have may exist in the graph: the snapshot is a sample, and
        every name in the pattern is known (an unknown name is certainly not there: the names are not sampled)."""
        if self.snapshot.sample is None:
            return False
        nodes = [pattern.left] + ([pattern.right] if pattern.right else [])
        labels = (label for node in nodes for label in node.labels)
        keys = [key for node in nodes for key in node.keys]
        types = ()
        if pattern.relationship is not None:
            types = pattern.relationship.types
            keys += pattern.relationship.keys
        return (
            all(label in self._names("labels") for label in labels)
            and all(kind in self._names("relationship_types") for kind in types)
            and all(key in self._names("property_keys") for key in keys)
        )

    # ------------------------------------------------------------ label combinations
    def _label_combinations(self, path: str) -> Resolution:
        query, node_vars, rel_vars = _online.identify_variables_and_construct_query(
            path
        )
        columns = [*node_vars, *rel_vars]
        if not query:
            # Nothing to infer: the database is asked an empty query and rejects it
            return Resolution((None, node_vars, rel_vars), miss=True)
        parsed = None
        with suppress(CypherError):
            parsed = _parse(path)
        if parsed is None:
            # What the database rejects, or what is not understood here, is "no table"
            return Resolution((None, node_vars, rel_vars), reliable=False, miss=True)

        rows: set[tuple[str, ...]] = set()
        reliable = True
        for left, right, kind, fit in self._hops(parsed):
            # UNWIND of the labels of each node variable: a kind of node with no label gives no row
            per_variable = [
                sorted(
                    left
                    if variable == parsed.left.variable
                    else right
                    if parsed.right is not None and variable == parsed.right.variable
                    else ()
                )
                for variable in node_vars
            ]
            rel_values = [
                [kind]
                if kind is not None and variable == parsed.relationship.variable
                else []
                for variable in rel_vars
                if parsed.relationship is not None
            ]
            if any(not values for values in per_variable + rel_values):
                continue
            new_rows = set(product(*per_variable, *rel_values))
            if new_rows:
                rows |= new_rows
                reliable = reliable and fit == Fit.CERTAIN
        frame = (
            pd.DataFrame(sorted(rows), columns=columns)
            if rows
            else pd.DataFrame(columns=columns)
        )
        if not rows and self._may_be_missing_from_the_sample(parsed):
            return Resolution(
                (frame, node_vars, rel_vars), reliable=False, miss=True, sampled=True
            )
        return Resolution(
            (frame, node_vars, rel_vars), reliable=reliable, miss=not rows
        )
