"""A summary of the structure of one graph, that can be saved, loaded, written by hand and captured from a database.

A :class:`Snapshot` holds what the validators ask the database about the schema: which labels, relationship types
and property keys exist, which indexes there are, which sets of labels occur together on a node, which
relationships connect which sets of labels, and which properties the nodes of a label (the relationships of a type)
have. It is immutable and made of plain values, so it can be turned into JSON as it is.

The label sets are kept whole (``("Area", "GeoArea")`` is one node with two labels), because a pattern such as
``(:Area)`` matches every node that has that label among others.
"""

import contextlib
import json
import os
import secrets
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
from typing import Any

from cypher_guardrail.drivers import AsyncDriver, Driver

FORMAT_VERSION = 1
_ENTITIES = ("NODE", "RELATIONSHIP")


def _sync_directory(directory: str) -> None:
    """Make the rename of a file in ``directory`` last (POSIX; elsewhere there is no way to ask for it)."""
    if os.name != "posix":
        return
    with contextlib.suppress(OSError):
        handle = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)


def _check_sample(sample):
    """Raise a ValueError unless ``sample`` is None or a positive whole number."""
    if sample is not None and (
        not isinstance(sample, int) or isinstance(sample, bool) or sample < 1
    ):
        raise ValueError(
            f"sample must be a positive whole number or None, not {sample!r}"
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _names(values, what) -> tuple[str, ...]:
    """``values`` as a sorted tuple of distinct strings."""
    values = list(values)
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{what} must be strings, not {value!r}")
    return tuple(sorted(set(values)))


def _label_set(labels, what) -> tuple[str, ...]:
    if isinstance(labels, str):
        raise TypeError(f"{what}: a set of labels is a list of names, not {labels!r}")
    return _names(labels, what)


def _tuple(value) -> tuple:
    """``value`` (a list, a set, a dict) as the tuple the fields hold before they are normalised."""
    return tuple(value.items()) if isinstance(value, dict) else tuple(value)


def _ends(labels) -> tuple:
    """The labels at one end of a relationship written by hand: one label, or a list of them."""
    return (labels,) if isinstance(labels, str) else tuple(labels)


def _by_name(mapping, what) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``{name: [names]}`` as a sorted tuple of ``(name, sorted tuple of names)``."""
    return tuple(
        sorted(
            (name, _names(values, f"{what} of {name!r}"))
            for name, values in dict(mapping).items()
        )
    )


def _keys_of(nodes, relationship_properties):
    """Every property of the labels and of the relationship types."""
    return {
        key
        for properties in (*nodes.values(), *relationship_properties.values())
        for key in properties
    }


_Names = Mapping[
    str, Iterable[str]
]  # a label (or a type, or an attribute) and the names that go with it


def _need(names, known, what, where):
    """Raise if a name that ``where`` mentions is not one of the ``known`` names."""
    for name in names:
        if name not in known:
            raise ValueError(
                f"{where} mentions the {what} {name!r}, which is not in the snapshot's {what}s"
            )


def _typed_properties(node_property_types, relationship_property_types):
    """The ``property_types`` of a snapshot written by hand: None when no type was given."""
    if node_property_types is None and relationship_property_types is None:
        return None
    return (
        *(
            (label, "NODE", _tuple(attributes))
            for label, attributes in (node_property_types or {}).items()
        ),
        *(
            (kind, "RELATIONSHIP", _tuple(attributes))
            for kind, attributes in (relationship_property_types or {}).items()
        ),
    )


class SnapshotSource:
    """Where a validator gets its snapshot: a :class:`Snapshot` itself, or a ``LiveSnapshot`` that keeps one fresh.

    A validator asks :meth:`current` once, when a validation starts, and uses that snapshot for the whole validation,
    so a source that is replaced in the middle of one can never give a validation two different snapshots.

    Attributes:
        graph: The name of the graph the snapshots are of (``""`` for a snapshot written by hand: it takes any).
    """

    @property
    def graph(self) -> str:
        raise NotImplementedError

    def current(self) -> "Snapshot":
        """The snapshot to validate with now."""
        raise NotImplementedError


@dataclass(frozen=True)
class Snapshot(SnapshotSource):
    """The structure of one graph, as of ``captured_at``.

    Build one with :meth:`capture` (from a database), :meth:`load` (from a file), :meth:`from_schema` (by hand) or
    :meth:`from_dict`. Every field is normalised (sorted, without duplicates), so two snapshots of the same
    structure are equal whatever the order they were given in.

    Attributes:
        graph: The name of the graph it was captured from (``""`` when written by hand).
        captured_at: When it was captured, ISO 8601 in UTC (None when written by hand).
        format_version: The version of the format of ``to_dict()`` (only ``1`` exists).
        labels: The node labels (``db.labels()``).
        relationship_types: The relationship types (``db.relationshipTypes()``).
        property_keys: The property keys (``db.propertyKeys()``).
        indexes: ``(label or type, "NODE" or "RELATIONSHIP", ((attribute, (index types, ...)), ...))``.
        node_label_sets: Every set of labels that occurs on a node, e.g. ``(("Goal",), ("Area", "GeoArea"))``.
        edges: Every ``(labels of the start node, relationship type, labels of the end node)`` that occurs.
        node_properties: ``((label, (properties that at least one node with the label has, ...)), ...)``.
        relationship_properties: The same for relationship types.
        property_types: ``((label or type, "NODE" or "RELATIONSHIP", ((property, (type names, ...)), ...)), ...)``:
            the types of the values each property has (FalkorDB's ``typeof``: ``"String"``, ``"Integer"``,
            ``"Float"``, ``"Boolean"``, ...), several when the data is mixed. None when the types were not
            captured (``capture(..., types=True)``, or ``from_schema(..., node_property_types=...)``).
        sample: None when the whole graph was scanned. The ``sample`` of ``capture(..., sample=N)`` otherwise: the
            snapshot then has at most N nodes (or relationships) of each label (or type), so a path or a property
            that it lacks may just not be in the sample. The validators do not take its "not there" for an answer
            (see ``verify`` in the README). It is in ``to_dict()`` only when it is set.
    """

    graph: str = ""
    captured_at: str | None = None
    format_version: int = FORMAT_VERSION
    labels: tuple = ()
    relationship_types: tuple = ()
    property_keys: tuple = ()
    indexes: tuple = ()
    node_label_sets: tuple = ()
    edges: tuple = ()
    node_properties: tuple = ()
    relationship_properties: tuple = ()
    property_types: tuple | None = None
    sample: int | None = None

    def current(self) -> "Snapshot":
        """The snapshot itself (a snapshot is its own :class:`SnapshotSource`)."""
        return self

    def __post_init__(self):
        if self.format_version != FORMAT_VERSION:
            raise ValueError(
                f"Unknown snapshot format_version {self.format_version}: this version of cypher-guardrail reads "
                f"only format_version {FORMAT_VERSION}."
            )
        if not isinstance(self.graph, str):
            raise TypeError(f"graph must be a string, not {self.graph!r}")
        if self.captured_at is not None and not isinstance(self.captured_at, str):
            raise TypeError(
                f"captured_at must be a string or None, not {self.captured_at!r}"
            )
        _check_sample(self.sample)
        # A frozen dataclass: the normalised values are set around the freeze
        normalised = {
            "labels": _names(self.labels, "labels"),
            "relationship_types": _names(self.relationship_types, "relationship_types"),
            "property_keys": _names(self.property_keys, "property_keys"),
            "indexes": self._indexes(self.indexes),
            "node_label_sets": tuple(
                sorted({_label_set(s, "node_label_sets") for s in self.node_label_sets})
            ),
            "edges": tuple(
                sorted(
                    {
                        (
                            _label_set(start, "edges"),
                            self._text(kind, "edges"),
                            _label_set(end, "edges"),
                        )
                        for start, kind, end in self.edges
                    }
                )
            ),
            "node_properties": _by_name(self.node_properties, "node_properties"),
            "relationship_properties": _by_name(
                self.relationship_properties, "relationship_properties"
            ),
            "property_types": (
                None
                if self.property_types is None
                else self._property_types(self.property_types)
            ),
        }
        for name, value in normalised.items():
            object.__setattr__(self, name, value)
        self._check()

    @staticmethod
    def _text(value, what):
        if not isinstance(value, str):
            raise TypeError(f"{what} must be strings, not {value!r}")
        return value

    @staticmethod
    def _indexes(indexes):
        found = set()
        for label, entity, attributes in indexes:
            if entity not in _ENTITIES:
                raise ValueError(
                    f"An index is on {' or '.join(_ENTITIES)}, not {entity!r}"
                )
            found.add(
                (
                    Snapshot._text(label, "indexes"),
                    entity,
                    _by_name(dict(attributes), "indexes"),
                )
            )
        return tuple(sorted(found))

    @staticmethod
    def _property_types(property_types):
        found = set()
        for name, entity, attributes in property_types:
            if entity not in _ENTITIES:
                raise ValueError(
                    f"The types of properties are of {' or '.join(_ENTITIES)}, not {entity!r}"
                )
            found.add(
                (
                    Snapshot._text(name, "property_types"),
                    entity,
                    _by_name(dict(attributes), "property_types"),
                )
            )
        return tuple(sorted(found))

    def _check(self):
        """Every name that a part mentions must be in the list of names it belongs to."""
        labels, types, keys = (
            set(self.labels),
            set(self.relationship_types),
            set(self.property_keys),
        )
        for label_set in self.node_label_sets:
            _need(label_set, labels, "label", "node_label_sets")
        for start, kind, end in self.edges:
            _need([*start, *end], labels, "label", "edges")
            _need([kind], types, "relationship type", "edges")
        for label, properties in self.node_properties:
            _need([label], labels, "label", "node_properties")
            _need(properties, keys, "property key", "node_properties")
        for kind, properties in self.relationship_properties:
            _need([kind], types, "relationship type", "relationship_properties")
            _need(properties, keys, "property key", "relationship_properties")
        self._check_property_types(labels, types, keys)

    def _check_property_types(self, labels, types, keys):
        """The types are of properties that their label (or type) has."""
        node_properties = dict(self.node_properties)
        relationship_properties = dict(self.relationship_properties)
        for name, entity, attributes in self.property_types or ():
            if entity == "NODE":
                _need([name], labels, "label", "property_types")
                own = node_properties.get(name, ())
            else:
                _need([name], types, "relationship type", "property_types")
                own = relationship_properties.get(name, ())
            properties = [a for a, _ in attributes]
            _need(properties, keys, "property key", "property_types")
            for prop in properties:
                if prop not in own:
                    raise ValueError(
                        f"property_types mentions the property {prop!r} of {name!r}, which {name!r} does not have"
                    )

    # ------------------------------------------------------------------------------- reading it
    @cached_property
    def _node_property_map(self) -> dict[str, tuple[str, ...]]:
        return dict(self.node_properties)

    @cached_property
    def _relationship_property_map(self) -> dict[str, tuple[str, ...]]:
        return dict(self.relationship_properties)

    def node_properties_of(self, label: str) -> tuple[str, ...]:
        """The properties that at least one node with ``label`` has (``()`` for an unknown label)."""
        return self._node_property_map.get(label, ())

    def relationship_properties_of(self, relationship_type: str) -> tuple[str, ...]:
        """The properties that at least one relationship of the type has (``()`` for an unknown type)."""
        return self._relationship_property_map.get(relationship_type, ())

    @property
    def has_property_types(self) -> bool:
        """Whether the types of the properties were captured (or given)."""
        return self.property_types is not None

    @cached_property
    def _property_type_map(self) -> dict[tuple[str, str], dict[str, tuple[str, ...]]]:
        return {
            (name, entity): dict(attributes)
            for name, entity, attributes in self.property_types or ()
        }

    def property_types_of(
        self, name: str, entity: str = "NODE"
    ) -> dict[str, tuple[str, ...]]:
        """``{property: its types}`` of a label (``entity`` ``"NODE"``) or a relationship type (``"RELATIONSHIP"``).

        ``{}`` for a name that is unknown, and when the types were not captured (see :attr:`has_property_types`).
        """
        return self._property_type_map.get((name, entity), {})

    # --------------------------------------------------------------------------- JSON and files
    def to_dict(self) -> dict[str, Any]:
        """The snapshot as plain JSON values (dicts, lists, strings, None). :meth:`from_dict` undoes it."""
        data = {
            "format_version": self.format_version,
            "graph": self.graph,
            "captured_at": self.captured_at,
            "labels": list(self.labels),
            "relationship_types": list(self.relationship_types),
            "property_keys": list(self.property_keys),
            "indexes": [
                [label, entity, {name: list(types) for name, types in attributes}]
                for label, entity, attributes in self.indexes
            ],
            "node_label_sets": [list(s) for s in self.node_label_sets],
            "edges": [
                [list(start), kind, list(end)] for start, kind, end in self.edges
            ],
            "node_properties": {
                label: list(properties) for label, properties in self.node_properties
            },
            "relationship_properties": {
                kind: list(properties)
                for kind, properties in self.relationship_properties
            },
            "property_types": (
                None
                if self.property_types is None
                else [
                    [name, entity, {prop: list(types) for prop, types in attributes}]
                    for name, entity, attributes in self.property_types
                ]
            ),
        }
        if self.sample is not None:
            data["sample"] = self.sample
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        """The snapshot that :meth:`to_dict` gave ``data`` for.

        Raises:
            ValueError: If ``data`` has another ``format_version``, lacks a field or has one that is unknown.
        """
        if not isinstance(
            data, dict
        ):  # a file that holds something else: a problem with its value
            raise ValueError("This is not a snapshot: expected a dict")  # noqa: TRY004
        if "format_version" not in data:
            raise ValueError("This is not a snapshot: it has no format_version")
        if data["format_version"] != FORMAT_VERSION:
            raise ValueError(
                f"Unknown snapshot format_version {data['format_version']}: this version of cypher-guardrail "
                f"reads only format_version {FORMAT_VERSION}."
            )
        # `property_types` is optional: a snapshot written without the types has none. So is `sample`: a snapshot of
        # the whole graph does not have it
        expected = set(cls().to_dict()) - {"property_types"}
        if missing := sorted(expected - set(data)):
            raise ValueError(f"The snapshot lacks the fields {', '.join(missing)}")
        if unknown := sorted(set(data) - expected - {"property_types", "sample"}):
            raise ValueError(f"The snapshot has unknown fields: {', '.join(unknown)}")
        try:
            return cls(
                graph=data["graph"],
                captured_at=data["captured_at"],
                format_version=data["format_version"],
                sample=data.get("sample"),
                property_types=(
                    None
                    if data.get("property_types") is None
                    else _tuple(data["property_types"])
                ),
                **{
                    name: _tuple(data[name])
                    for name in (
                        "labels",
                        "relationship_types",
                        "property_keys",
                        "indexes",
                        "node_label_sets",
                        "edges",
                        "node_properties",
                        "relationship_properties",
                    )
                },
            )
        except (TypeError, AttributeError, KeyError, IndexError, ValueError) as error:
            # a field of the wrong kind (a number where a list goes, a short row) surfaces as one of these
            raise ValueError(f"This is not a valid snapshot: {error}") from error

    def to_json(self, indent: int | None = 2) -> str:
        """The snapshot as a JSON text."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Snapshot":
        """The snapshot in a JSON text made by :meth:`to_json`. Raises ValueError if it is not one."""
        try:
            data = json.loads(text)
        except (ValueError, RecursionError) as e:
            raise ValueError(f"This is not a snapshot: not valid JSON ({e})") from e
        return cls.from_dict(data)

    def save(self, path: str | os.PathLike) -> None:
        """Write the snapshot to ``path`` as JSON, replacing a file there atomically.

        The text goes to a temporary file in the same directory first, so a crash or a full disk leaves the old
        file as it was, never half of a new one. The file and then its directory are synced to disk. A file that is
        replaced keeps its mode (a snapshot shared with another user stays readable); a new one gets the mode an
        ordinary file would, with the umask applied.
        """
        directory = os.path.dirname(os.path.abspath(path))
        mode = None
        with contextlib.suppress(OSError):  # no file yet
            mode = stat.S_IMODE(os.stat(path).st_mode)
        temporary = os.path.join(directory, f".snapshot-{secrets.token_hex(8)}.tmp")
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(self.to_json())
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            if mode is not None:
                os.chmod(temporary, mode)
            os.replace(temporary, path)
            _sync_directory(directory)
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Snapshot":
        """Read a snapshot written by :meth:`save`. Raises ValueError if the file is not one."""
        with open(path, encoding="utf-8") as file:
            return cls.from_json(file.read())

    # ------------------------------------------------------------------------------ by hand
    @classmethod
    def from_schema(  # noqa: PLR0913, PLR0917 - the public signature: every part of a snapshot, all optional but the first two
        cls,
        nodes: _Names,
        relationships: Iterable[tuple[str | Sequence[str], str, str | Sequence[str]]],
        relationship_properties: _Names | None = None,
        indexes: Iterable[tuple[str, str, _Names]] = (),
        node_label_sets: Iterable[Iterable[str]] = (),
        graph: str = "",
        node_property_types: Mapping[str, _Names] | None = None,
        relationship_property_types: Mapping[str, _Names] | None = None,
    ) -> "Snapshot":
        """A snapshot written by hand, for when there is no database to capture it from.

        It describes the schema as declared, not as observed, so it says what *can* exist: a node with just one
        label for every label of ``nodes`` that is in none of ``node_label_sets`` (those labels are only found
        together, as the sets say), and one edge for every relationship.

        Args:
            nodes: ``{label: [the properties its nodes have]}``.
            relationships: ``[(start, relationship type, end), ...]``. The start and the end are a label, or a
                list of labels for nodes that have several, e.g. ``(["GeoArea", "Area"], "HAS_NUTS1", "Nuts1")``.
            relationship_properties: ``{relationship type: [properties]}`` (optional).
            indexes: ``[(label or type, "NODE" or "RELATIONSHIP", {attribute: [index types]})]``, as
                ``db.indexes()`` reports them, e.g. ``("Doc", "NODE", {"embedding": ["VECTOR"]})``.
            node_label_sets: Sets of labels that occur together on a node, e.g. ``[["GeoArea", "Area"]]``.
                Every label in them must also be a key of ``nodes``.
            graph: The name of the graph it describes.
            node_property_types: ``{label: {property: [type names]}}``, e.g. ``{"Goal": {"code": ["String"]}}``
                (FalkorDB's ``typeof`` names: ``"String"``, ``"Integer"``, ``"Float"``, ``"Boolean"``, ...).
                Optional: without it (and ``relationship_property_types``) the snapshot has no types.
            relationship_property_types: The same for relationship types.

        Raises:
            ValueError: If a relationship or a set of labels names a label that is not in ``nodes``, or a
                property with a type is not one of the properties of its label.
        """
        relationships = [tuple(r) for r in relationships]
        given_sets = [tuple(s) for s in node_label_sets]
        together = {label for labels in given_sets for label in labels}  # (not alone)
        relationship_properties = dict(relationship_properties or {})
        types = {kind for _, kind, _ in relationships} | set(relationship_properties)
        return cls(
            graph=graph,
            labels=tuple(nodes),
            relationship_types=tuple(types),
            property_keys=_tuple(_keys_of(nodes, relationship_properties)),
            indexes=tuple(tuple(i) for i in indexes),
            node_label_sets=(
                *((label,) for label in nodes if label not in together),
                *given_sets,
            ),
            edges=tuple((_ends(a), kind, _ends(b)) for a, kind, b in relationships),
            node_properties=_tuple(nodes),
            relationship_properties=tuple(
                (kind, relationship_properties.get(kind, ())) for kind in types
            ),
            property_types=_typed_properties(
                node_property_types, relationship_property_types
            ),
        )

    # --------------------------------------------------------------------- from a database
    @classmethod
    def capture(
        cls,
        driver: Driver,
        graph: str | None = None,
        *,
        sample: int | None = None,
        types: bool = False,
    ) -> "Snapshot":
        """Capture the snapshot of a graph, with read-only queries.

        Args:
            driver: A ``falkordb.FalkorDB`` client (or FalkorDBLite), or a ``falkordb.Graph``.
            graph: The graph to capture (optional when a ``falkordb.Graph`` is given).
            sample: None scans the whole graph, which is exact but costs a pass over every node and
                relationship. A number limits each scan to that many nodes (or relationships) per label (or
                type), whether it reads the label sets, the edges or the properties: quicker, but it can miss
                rare properties and edges (and a node with no label). The snapshot records it as ``sample``, and
                the validators do not report what a sample lacks.
            types: True also records the types of the values of every property (``typeof``), for
                ``PropertiesValidator(..., check_types=True)``. It reads the same data as the scan for the
                properties, so it costs no more queries. It needs the whole graph (no ``sample``): a sample can
                miss a type, and a type that was missed would be reported as a mismatch.

        Raises:
            ValueError: If there is no graph name, ``sample`` is not a positive whole number, or ``types`` is
                True together with a ``sample``.
        """
        from cypher_guardrail._internal.capture_driver import capture

        return capture(driver, graph, sample=sample, types=types)

    @classmethod
    async def capture_async(
        cls,
        driver: AsyncDriver,
        graph: str | None = None,
        *,
        sample: int | None = None,
        types: bool = False,
        max_concurrency: int = 8,
    ) -> "Snapshot":
        """The async version of :meth:`capture`, for a ``falkordb.asyncio`` client or graph."""
        from cypher_guardrail._internal.capture_driver import capture_async

        return await capture_async(
            driver,
            graph,
            sample=sample,
            types=types,
            max_concurrency=max_concurrency,
        )
