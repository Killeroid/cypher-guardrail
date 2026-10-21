from collections.abc import Generator
from typing import Any, Generic, TypeVar

from cypher_guardrail._internal.core import advice as _advice
from cypher_guardrail._internal.core.io import Request
from cypher_guardrail._internal.sources import hybrid as _modes
from cypher_guardrail.backend import _Backend, get_async_backend, get_backend
from cypher_guardrail.drivers import AsyncDriver, Driver, Verify
from cypher_guardrail.snapshot import SnapshotSource
from cypher_guardrail.suggestions import Suggestion

_D = TypeVar("_D")  # the kind of driver: a client or graph, sync or async


class _Advisor(_modes.ModeInfo, Generic[_D]):
    """The logic of Advisor, as generators (see ``cypher_guardrail._internal.core.io``): one copy for the sync and the async advisor."""

    def __init__(
        self,
        driver: _D,
        *,
        snapshot: SnapshotSource | None = None,
        verify: Verify | None = None,
        suggest_limit: bool = False,
    ) -> None:
        """
        Initialize the advisor with a FalkorDB client.

        Args:
            driver: A ``falkordb.FalkorDB`` client (or FalkorDBLite), or a ``falkordb.Graph``.
                ``database_name`` is the graph name (optional when a ``falkordb.Graph`` is given).
            snapshot (Snapshot | None): A snapshot of the graph, which answers the lookups it can answer for sure
                (the plan still comes from the database). This is the hybrid mode.
            verify (str | None): Hybrid mode: ``"misses"`` (the default) asks the database before saying that a
                thing the snapshot lacks is not there, ``"never"`` trusts the snapshot.
            suggest_limit (bool): Also suggest a ``LIMIT`` for a query that reads rows of the graph and returns every
                one of them (default: False: it is advice about any such query, so it is asked for).

        Raises:
            TypeError: If the driver is not a FalkorDB client or graph, or it is a ``Snapshot``: the plan of a
                query is FalkorDB's, so the advisor needs a database.
        """
        if isinstance(driver, SnapshotSource):
            raise TypeError(
                "Advisor needs a database: only FalkorDB can say what it will do with a query. Give it the client, "
                "and the snapshot as snapshot= to save the lookups."
            )
        self.driver = driver
        self.suggest_limit = suggest_limit
        self.backend = self._open_backend(driver, snapshot, verify)

    def _open_backend(
        self, driver: _D, snapshot: SnapshotSource | None, verify: Verify | None
    ) -> _Backend:
        """The backend the advisor runs on. The async advisor overrides this."""
        return get_backend(driver, snapshot=snapshot, verify=verify)

    def _advise(
        self, query: str, database_name: str | None
    ) -> Generator[Request, Any, list[Suggestion]]:
        graph = self.backend.graph_name(database_name)
        return _advice.advise(graph, query, suggest_limit=self.suggest_limit)


class Advisor(_Advisor[Driver]):
    """
    Suggest what a person can do to make a valid query cheaper: give a path an upper limit, label a node that is
    looked for among all the nodes, relate two patterns that are a product, compare a property that has an index
    without a function around it.

    A suggestion says what it is about (``fragment``), why, and, when the replacement is one thing, the query as it
    could be written (``example``). Nothing is applied: each of them can change what the query returns, and
    ``changes_results`` says so. See :mod:`cypher_guardrail.suggestions`.
    """

    def advise(self, query: str, database_name: str | None = None) -> list[Suggestion]:
        """
        The suggestions for ``query``, in the order of the list in the class docstring.

        Args:
            query (str): The Cypher query. It is planned, never run. A query that FalkorDB rejects has no
                suggestions: validate it first.
            database_name (str | None): The graph. None (default) uses the graph the advisor was given.

        Returns:
            list[Suggestion]: Empty when there is nothing to say.

        Raises:
            GraphNotFoundError: If the graph does not exist.
        """
        return self.backend.run(self._advise(query, database_name))


class AsyncAdvisor(_Advisor[AsyncDriver]):
    """The async version of :class:`Advisor`, for a ``falkordb.asyncio`` client or graph: ``advise`` is a coroutine."""

    def __init__(
        self,
        driver: AsyncDriver,
        *,
        max_concurrency: int = 8,
        snapshot: SnapshotSource | None = None,
        verify: Verify | None = None,
        suggest_limit: bool = False,
    ) -> None:
        """
        Initialize the advisor with an async FalkorDB client.

        Args:
            driver: A ``falkordb.asyncio.FalkorDB`` client, or one of its graphs.
            max_concurrency (int): The most requests in flight at once on this advisor's connection.
            snapshot (Snapshot | None): As for :class:`Advisor`.
            verify (str | None): As for :class:`Advisor`.
            suggest_limit (bool): As for :class:`Advisor`.

        Raises:
            TypeError: If the driver is not an async FalkorDB client or graph, or it is a ``Snapshot``.
            ValueError: If ``max_concurrency`` is less than 1.
        """
        self._max_concurrency = max_concurrency
        super().__init__(
            driver, snapshot=snapshot, verify=verify, suggest_limit=suggest_limit
        )

    def _open_backend(
        self,
        driver: AsyncDriver,
        snapshot: SnapshotSource | None,
        verify: Verify | None,
    ) -> _Backend:
        return get_async_backend(
            driver, self._max_concurrency, snapshot=snapshot, verify=verify
        )

    async def advise(
        self, query: str, database_name: str | None = None
    ) -> list[Suggestion]:
        """The async version of :meth:`Advisor.advise`: the same arguments and result."""
        return await self.backend.run(self._advise(query, database_name))
