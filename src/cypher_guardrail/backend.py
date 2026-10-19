"""Everything the validators need from FalkorDB.

FalkorDB has some peculiarities that cypher-guardrail compensates for here, so the validators can stay simple:

* ``database_name`` is the **graph name**. It may be omitted when a ``falkordb.Graph`` was supplied.
* ``EXPLAIN`` is a separate command (``GRAPH.EXPLAIN``); sending ``EXPLAIN ...`` as a query would execute it.
* FalkorDB sends no notifications, so unknown label / relationship type / property warnings and missing
  parameters are derived from the graph schema and the query text.
* Queries are run with ``GRAPH.RO_QUERY``, so a validator can never modify the graph.
* There is no ``EXISTS { ... }`` subquery, so existence is checked with ``MATCH ... RETURN 1 LIMIT 1``.

The operations themselves are written once, as generators, in ``cypher_guardrail._internal.core.queries``; this module runs them
on a sync client (:class:`FalkorDBBackend`) or on a ``falkordb.asyncio`` one (:class:`AsyncFalkorDBBackend`).
"""

import functools
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from typing import Any

import falkordb
from falkordb.asyncio import FalkorDB as AsyncFalkorDB
from falkordb.asyncio.graph import AsyncGraph

from cypher_guardrail._internal.core import io as _io
from cypher_guardrail._internal.core import queries as _queries

# The codes and CypherError are part of this module's API, though they are defined in cypher_guardrail._internal.core.errors
from cypher_guardrail._internal.core.errors import (  # noqa: F401
    ARITHMETIC_ERROR,
    EXECUTION_FAILED,
    PARAMETER_NOT_PROVIDED,
    SYNTAX_ERROR,
    UNKNOWN_INDEX,
    UNKNOWN_LABEL,
    UNKNOWN_PROPERTY,
    UNKNOWN_REL_TYPE,
    CypherError,
)
from cypher_guardrail._internal.sources import hybrid as _modes
from cypher_guardrail.snapshot import SnapshotSource


def snapshotted(method):
    """Run a validator method inside ``self.backend.snapshot()``, so its lookups are shared."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.backend.snapshot():
            return method(self, *args, **kwargs)

    return wrapper


def async_snapshotted(method):
    """Run an async validator method inside ``self.backend.snapshot()``, so its lookups are shared."""

    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        with self.backend.snapshot():
            return await method(self, *args, **kwargs)

    return wrapper


class _Backend:
    """What the sync and the async backend share: which graph to use, and snapshots."""

    # Set by the subclasses
    default_graph: str | None
    _select_graph: Callable[[str], Any]
    _transport: Any

    # How the lookups are answered (see ``cypher_guardrail._internal.sources.hybrid``)
    mode: str  # "online", "offline" or "hybrid"
    stats: _modes.LookupStats
    _resolver: Callable
    # The snapshot source of the hybrid and the offline mode; None online
    _pins: _modes.Pins | None = None

    def _answer_lookups(self, snapshot, verify):
        """Choose the mode of a backend that has a database: online, or hybrid when it is given a snapshot."""
        self.stats = _modes.LookupStats()
        if snapshot is None:
            if verify is not None:
                raise TypeError(
                    "verify= applies only when a snapshot is given with the client: snapshot=<Snapshot>."
                )
            self.mode = "online"
            self._resolver = _modes.online_resolver(self.stats)
        else:
            if not isinstance(snapshot, SnapshotSource):
                raise TypeError(
                    f"snapshot= must be an cypher_guardrail.Snapshot or a LiveSnapshot, not {type(snapshot).__name__}."
                )
            self.mode = "hybrid"
            check = _modes.check_verify(verify)
            self._pins = _modes.Pins(snapshot)
            self._resolver = self._pins.resolver(
                lambda current: _modes.chain_resolver(current, check, self.stats)
            )

    # ------------------------------------------------------------------ snapshots
    def snapshot(self):
        """Share schema lookups and existence checks for the duration of the ``with`` block.

        A single validation asks the same questions many times (is ``Goal`` a label? does ``Goal``
        have ``code``? does this path exist?). Inside a snapshot each is asked once. Nothing is kept
        after the outermost block ends, so the next validation sees any change made to the graph.
        Snapshots nest. A snapshot belongs to the thread or asyncio task that opened it: tasks it starts and
        a copied context (``asyncio.to_thread``, ``contextvars.copy_context().run``) share it, a new
        ``threading.Thread`` does not.
        """
        return self._shared()

    @contextmanager
    def _shared(self):
        with _io.snapshot(self._transport), self._pin():
            yield

    def _pin(self):
        """A validation uses one snapshot of the snapshot source, if there is one (see ``_modes.Pins``)."""
        return nullcontext() if self._pins is None else self._pins.pin()

    # ------------------------------------------------------------------ helpers
    def check_database(self, database_name=None):
        """Raise ValueError if there is no graph to use. Called by the validators' public methods."""
        self.graph_name(database_name)

    def graph_name(self, database_name):
        """``database_name``, else the graph this backend was created with. Raises ValueError if neither."""
        name = database_name or self.default_graph
        if not name:
            raise ValueError(
                "FalkorDB needs a graph name: pass database_name='<graph>' or give the validator a falkordb.Graph."
            )
        return name

    def _graph(self, database_name):
        """The ``falkordb.Graph`` to query (a graph is selected by name on every call, not kept open)."""
        return self._select_graph(self.graph_name(database_name))


class FalkorDBBackend(_Backend):
    """The database operations cypher-guardrail needs, on a ``falkordb.FalkorDB`` client (including
    FalkorDBLite) or a ``falkordb.Graph``."""

    def __init__(self, client, graph_name=None, snapshot=None, verify=None):
        if isinstance(
            client,
            AsyncGraph | AsyncFalkorDB | AsyncFalkorDBBackend | AsyncSnapshotBackend,
        ):
            # An AsyncGraph is also a falkordb.Graph, so it has to be turned away first
            raise TypeError(
                "This is an async FalkorDB client: use AsyncSyntaxValidator, AsyncSchemaValidator or "
                "AsyncPropertiesValidator (or AsyncFalkorDBBackend) with it."
            )
        if isinstance(client, falkordb.Graph):
            # Graph.client is the underlying connection, so sibling graphs are built from it directly
            connection = client.client
            self._select_graph = lambda name: falkordb.Graph(connection, name)
            self.default_graph = graph_name or client.name
        elif isinstance(client, falkordb.FalkorDB):
            self._select_graph = client.select_graph
            self.default_graph = graph_name
        else:
            raise TypeError(
                "Provided driver is not a valid FalkorDB client (falkordb.FalkorDB or falkordb.Graph)."
            )
        self._transport = _io.SyncTransport(self._select_graph)
        self._answer_lookups(snapshot, verify)

    def run(self, generator):
        """Run a logic generator (see ``cypher_guardrail._internal.core.io``) with this backend's connection, and return its value."""
        with self._pin():
            return _io.run_sync(generator, self._transport, self._resolver)

    def index_exists(self, kind, on, label, attribute=None, database_name=None):
        """Check whether the graph has an index that a full-text or vector query needs.

        Args:
            kind (str): 'fulltext' or 'vector'.
            on (str): 'nodes' or 'relationships'.
            label (str): The node label, or the relationship type, of the index.
            attribute (str | None): The attribute the index must cover. None: any attribute.
        """
        graph = self.graph_name(database_name)
        return self.run(_queries.index_exists(graph, kind, on, label, attribute))

    # -------------------------------------------------------------- running queries
    def execute(self, query, database_name=None):
        """Run a read query and return one dict per row, keyed by column name.

        Raises:
            CypherError: If the database rejects the query.
        """
        return self.run(_queries.execute(self.graph_name(database_name), query))

    def explain(self, query, database_name=None, parameters=None):
        """Check a query without executing it.

        ``parameters`` are the names (or a dict) of the parameters the caller will send with the query: they are
        not reported as missing.

        Returns:
            list[dict]: The problems found (dicts with 'code' and 'description'): currently only missing parameters.

        Raises:
            CypherError: If the database rejects the query.
        """
        return self.run(
            _queries.explain(
                self.graph_name(database_name), query, parameters=parameters
            )
        )

    def query_to_df(self, query, database_name=None):
        """Run a read query and return the rows as a DataFrame (columns = returned names)."""
        return self.run(_queries.query_to_df(self.graph_name(database_name), query))

    # ----------------------------------------------------------------- schema checks
    def path_exists(self, path, database_name=None):
        """Check whether a pattern (node, relationship or path) matches anything in the graph.

        Returns:
            tuple[bool, list[dict]]: Whether it exists, and warnings about unknown labels/types.
        """
        return self.run(_queries.path_exists(self.graph_name(database_name), path))

    def property_exists(self, label, kind, prop, database_name=None):
        """Check whether any node/relationship with ``label`` has property ``prop``.

        Args:
            kind (str): 'node' or 'relationship'.

        Returns:
            tuple[bool, list[dict]]: Whether it exists, and a warning if the property key is unknown.
        """
        return self.run(
            _queries.property_exists(self.graph_name(database_name), label, kind, prop)
        )


class AsyncFalkorDBBackend(_Backend):
    """The database operations cypher-guardrail needs, on a ``falkordb.asyncio.FalkorDB`` client or one of its
    graphs. Every operation is a coroutine; see :class:`FalkorDBBackend` for what they do.

    Args:
        client: A ``falkordb.asyncio.FalkorDB`` client, or one of its graphs.
        graph_name (str | None): The graph to use when none is given. Defaults to the name of a given graph.
        max_concurrency (int): The most requests in flight at once (see :class:`cypher_guardrail._internal.core.io.AsyncTransport`).
    """

    def __init__(
        self, client, graph_name=None, max_concurrency=8, snapshot=None, verify=None
    ):
        if isinstance(client, AsyncGraph):
            # Graph.client is the underlying connection, so sibling graphs are built from it directly
            connection = client.client
            self._select_graph = lambda name: AsyncGraph(connection, name)
            self.default_graph = graph_name or client.name
        elif isinstance(client, AsyncFalkorDB):
            self._select_graph = client.select_graph
            self.default_graph = graph_name
        else:
            raise TypeError(
                "Provided driver is not a valid async FalkorDB client (falkordb.asyncio.FalkorDB or one of "
                "its graphs). For a sync client use SyntaxValidator, SchemaValidator or PropertiesValidator."
            )
        self._transport = _io.AsyncTransport(self._select_graph, max_concurrency)
        self._answer_lookups(snapshot, verify)

    async def run(self, generator):
        """Run a logic generator (see ``cypher_guardrail._internal.core.io``) with this backend's connection, and return its value."""
        with self._pin():
            return await _io.run_async(generator, self._transport, self._resolver)

    async def index_exists(self, kind, on, label, attribute=None, database_name=None):
        """The async version of :meth:`FalkorDBBackend.index_exists`."""
        graph = self.graph_name(database_name)
        return await self.run(_queries.index_exists(graph, kind, on, label, attribute))

    async def execute(self, query, database_name=None):
        """The async version of :meth:`FalkorDBBackend.execute`."""
        return await self.run(_queries.execute(self.graph_name(database_name), query))

    async def explain(self, query, database_name=None, parameters=None):
        """The async version of :meth:`FalkorDBBackend.explain`."""
        return await self.run(
            _queries.explain(
                self.graph_name(database_name), query, parameters=parameters
            )
        )

    async def query_to_df(self, query, database_name=None):
        """The async version of :meth:`FalkorDBBackend.query_to_df`."""
        return await self.run(
            _queries.query_to_df(self.graph_name(database_name), query)
        )

    async def path_exists(self, path, database_name=None):
        """The async version of :meth:`FalkorDBBackend.path_exists`."""
        return await self.run(
            _queries.path_exists(self.graph_name(database_name), path)
        )

    async def property_exists(self, label, kind, prop, database_name=None):
        """The async version of :meth:`FalkorDBBackend.property_exists`."""
        return await self.run(
            _queries.property_exists(self.graph_name(database_name), label, kind, prop)
        )


class SnapshotBackend(_Backend):
    """What the validators need when they are given a :class:`~cypher_guardrail.snapshot.Snapshot` and no database.

    Every lookup is answered from the snapshot. Anything that needs a database (``EXPLAIN``, a query) is an error.
    The snapshot is of one graph: ``database_name`` may be left out, or must be that graph's name (a snapshot written
    by hand has no graph name, and takes any).
    """

    def __init__(self, snapshot):
        if not isinstance(snapshot, SnapshotSource):
            raise TypeError(
                f"Expected an cypher_guardrail.Snapshot or a LiveSnapshot, not {type(snapshot).__name__}."
            )
        self._source = snapshot
        self._pins = _modes.Pins(snapshot)
        self.default_graph = snapshot.graph or None
        self._select_graph = lambda name: None  # there is no graph to select
        self._transport = _modes.OfflineTransport()
        self.stats = _modes.LookupStats()
        self.mode = "offline"
        self._resolver = self._pins.resolver(
            lambda current: _modes.snapshot_resolver(current, self.stats)
        )

    def graph_name(self, database_name):
        """``database_name``, which must be the graph of the snapshot; or that graph. Raises ValueError otherwise."""
        known = self._source.graph
        if database_name and known and database_name != known:
            raise ValueError(
                f"This snapshot is of the graph {known!r}, not {database_name!r}: capture a snapshot of that graph "
                "or validate with a client (snapshot= is then used only for the graph it is of)."
            )
        return known or database_name or "snapshot"

    def run(self, generator):
        """Run a logic generator (see ``cypher_guardrail._internal.core.io``) on the snapshot, and return its value."""
        with self._pin():
            return _io.run_sync(generator, self._transport, self._resolver)


class AsyncSnapshotBackend(SnapshotBackend):
    """The async twin of :class:`SnapshotBackend`: ``run`` is a coroutine, though nothing in it ever waits."""

    async def run(self, generator):
        with self._pin():
            return _io.run_sync(generator, self._transport, self._resolver)


def get_backend(driver, snapshot=None, verify=None):
    """Wrap a ``falkordb.FalkorDB`` client, a ``falkordb.Graph`` or a :class:`~cypher_guardrail.snapshot.Snapshot` for the
    validators.

    Args:
        driver: What to validate with. A snapshot is the offline mode: no database. A client or a graph is the online
            mode, or the hybrid mode when ``snapshot`` is given too.
        snapshot: A :class:`~cypher_guardrail.snapshot.Snapshot` of the graph, to answer with what it can (hybrid mode).
        verify: ``"misses"`` (the default) or ``"never"``: whether the database is asked before a thing the snapshot
            says is not there is reported (hybrid mode only).

    A backend is returned unchanged (and has its mode already).

    Raises:
        TypeError: If ``driver`` is none of these (an async client is turned away, with a pointer to the async
            validators), or ``snapshot`` / ``verify`` are given where they do not apply.
    """
    if isinstance(driver, SnapshotSource):
        if snapshot is not None or verify is not None:
            raise TypeError(
                "A Snapshot (or LiveSnapshot) given as the driver is the whole validator: snapshot= and verify= are for a client "
                "that has a database to ask."
            )
        return SnapshotBackend(driver)
    if isinstance(driver, FalkorDBBackend | SnapshotBackend) and not isinstance(
        driver, AsyncSnapshotBackend
    ):
        if snapshot is not None or verify is not None:
            raise TypeError(
                "A backend has its mode already: snapshot= and verify= do not apply."
            )
        return driver
    return FalkorDBBackend(driver, snapshot=snapshot, verify=verify)


def get_async_backend(driver, max_concurrency=8, snapshot=None, verify=None):
    """Wrap a ``falkordb.asyncio.FalkorDB`` client, one of its graphs or a :class:`~cypher_guardrail.snapshot.Snapshot` for
    the async validators. See :func:`get_backend` for ``snapshot`` and ``verify``.

    An :class:`AsyncFalkorDBBackend` is returned unchanged (and keeps its own ``max_concurrency``).

    Raises:
        TypeError: If ``driver`` is none of these (a sync client is turned away, with a pointer to the sync validators).
    """
    if isinstance(driver, SnapshotSource):
        if snapshot is not None or verify is not None:
            raise TypeError(
                "A Snapshot (or LiveSnapshot) given as the driver is the whole validator: snapshot= and verify= are for a client "
                "that has a database to ask."
            )
        return AsyncSnapshotBackend(driver)
    if isinstance(driver, AsyncFalkorDBBackend | AsyncSnapshotBackend):
        if snapshot is not None or verify is not None:
            raise TypeError(
                "A backend has its mode already: snapshot= and verify= do not apply."
            )
        return driver
    return AsyncFalkorDBBackend(
        driver, max_concurrency=max_concurrency, snapshot=snapshot, verify=verify
    )
