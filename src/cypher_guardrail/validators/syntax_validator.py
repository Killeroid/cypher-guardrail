from collections import defaultdict
from collections.abc import Collection, Generator, Iterable
from typing import Any, Generic, TypeVar

import regex as re

from cypher_guardrail._internal.core import builders as _issues
from cypher_guardrail._internal.core import performance as _performance
from cypher_guardrail._internal.core import queries as _queries
from cypher_guardrail._internal.core.arithmetic import zero_divisions
from cypher_guardrail._internal.core.errors import CypherError, GraphNotFoundError
from cypher_guardrail._internal.core.io import Lookup, Request
from cypher_guardrail._internal.core.scopes import restore_variables, separate_variables
from cypher_guardrail._internal.sources import hybrid as _modes
from cypher_guardrail.backend import get_async_backend, get_backend
from cypher_guardrail.drivers import AsyncDriver, Driver, Verify
from cypher_guardrail.issues import (
    ARITHMETIC_ERROR,
    CARTESIAN_PRODUCT,
    SYNTAX_ERROR,
    UNBOUNDED_PATH,
    UNLABELED_NODE_SCAN,
    WRITE_QUERY_WARNING,
    Issue,
)
from cypher_guardrail.patterns import split_union, strip_literals_and_comments
from cypher_guardrail.snapshot import SnapshotSource


def _with_several_labels(branch):
    """The variables of one query (or branch of a UNION) that have more than one label, with their labels in query order."""
    # Find the variable and label of every node or relationship that has a label (also when followed by {properties})
    matches = re.findall(
        r"[\(\[](\s*\w+\s*):(\s*\w+\s*)(?:\s*\{[^\}]*\})?[\)\]]", branch
    )
    # variable -> its labels, in query order (a dict is an ordered set)
    variable_labels = defaultdict(dict)
    for var, label in matches:
        variable_labels[var.strip()][label.strip()] = None
    # A variable conflicts if it has more than one label
    return {var: labels for var, labels in variable_labels.items() if len(labels) > 1}


def _together_on_a_node(labels, combinations):
    """Whether one of the label ``combinations`` that occur together on a node has all of ``labels``."""
    return any(
        all(label in combination for label in labels) for combination in combinations
    )


# the codes of the issues that leave a query valid
_WARNINGS = {
    WRITE_QUERY_WARNING,
    CARTESIAN_PRODUCT,
    UNLABELED_NODE_SCAN,
    UNBOUNDED_PATH,
}

_D = TypeVar("_D")  # the kind of driver: a client or graph, sync or async


class _SyntaxValidator(_modes.ModeInfo, Generic[_D]):
    """The logic of SyntaxValidator, as generators (see ``cypher_guardrail._internal.core.io``): one copy for the sync and the async validator."""

    def __init__(  # noqa: PLR0913 - the options of the sync validator
        self,
        driver: _D,
        check_multilabeled_nodes: bool = True,
        read_only: bool = True,
        *,
        snapshot: SnapshotSource | None = None,
        verify: Verify | None = None,
        check_performance: bool | Collection[str] = False,
    ) -> None:
        """
        Initialize the validator with a FalkorDB client.

        Args:
            driver: A ``falkordb.FalkorDB`` client (or FalkorDBLite), or a ``falkordb.Graph``.
                ``database_name`` is the graph name (optional when a ``falkordb.Graph`` is given).
            check_multilabeled_nodes (bool): Whether a variable with several labels (``(a:Goal) (a:Target)``) is let
                off when a node of the graph has all of them (default: True). The label combinations of the graph
                are asked when a query has such a variable, and not kept: the next validation sees the graph as it
                is then.
            read_only (bool): Whether the queries may only read (default: True). A query that can modify the graph
                (CREATE, MERGE, SET, DELETE, REMOVE, an index, a procedure that writes) is then an error
                (``WRITE_QUERY_ERROR``); with False it is only a warning (``WRITE_QUERY_WARNING``) and the query is
                still valid. A query that only reads is run for a millisecond at most, to ask the database.
            snapshot (Snapshot | None): A snapshot of the graph, which answers the lookups it can answer for sure
                (the database is still asked to check the syntax). This is the hybrid mode.
            verify (str | None): Hybrid mode: ``"misses"`` (the default) asks the database before reporting a thing
                the snapshot says is not there, ``"never"`` trusts the snapshot.
            check_performance (bool | Collection[str]): Also warn about a query that is valid and may be expensive, from
                the plan that FalkorDB makes of it (one more, cheap, request): ``cypher_guardrail.Warning.CartesianProduct``,
                ``cypher_guardrail.Warning.UnlabeledNodeScan`` and ``cypher_guardrail.Warning.UnboundedPath``. They are
                heuristics, so each can be chosen: ``True`` is all of them, ``{"cartesian_product", "unlabeled_scan",
                "unbounded_path"}`` (or some of them) is those. Off by default. A warning does not make the query invalid.

        Raises:
            TypeError: If the driver is not a FalkorDB client or graph, or ``check_performance`` is a string.
            ValueError: If ``check_performance`` names a check that does not exist.
        """
        if isinstance(driver, SnapshotSource):
            raise TypeError(
                "SyntaxValidator needs a database: only FalkorDB can check syntax. Give it the client, and the "
                "snapshot as snapshot= to save the lookups."
            )
        self.driver = driver
        self.backend = self._open_backend(driver, snapshot, verify)

        self.check_multilabeled_nodes = check_multilabeled_nodes
        self.read_only = read_only
        self.performance = _performance.selected(check_performance)

    def _open_backend(self, driver, snapshot, verify):
        """The backend the validator runs on. The async validators override this."""
        return get_backend(driver, snapshot=snapshot, verify=verify)

    def _validate(
        self, query, database_name=None, parameters=None
    ) -> Generator[Request, Any, tuple[bool, list[Issue]]]:
        """The logic of ``validate``, as a generator."""
        graph = self.backend.graph_name(database_name)
        metadata: list[Issue] = []  # the errors and warnings found

        # Nothing to ask the database: it would only answer that the query is empty
        if not query.strip():
            return False, [_issues.database_error(SYNTAX_ERROR, "Error: empty query.")]

        try:
            # Check the query plan without executing the query (GRAPH.EXPLAIN)
            notifications = yield from _queries.explain(
                graph, query, self.read_only, parameters
            )
            # EXPLAIN does not execute the query, so what it gives are a missing $parameter and a query that writes
            metadata.extend(notification.copy() for notification in notifications)

            # The checks below look for Cypher syntax, so they must not see text inside strings or
            # comments (e.g. WHERE n.name = 'a / 0' or // (n:A) (n:B))
            code = strip_literals_and_comments(query)
            if (yield from self._divides_by_zero(code, graph)):
                metadata.append(
                    _issues.database_error(ARITHMETIC_ERROR, "Division by zero.")
                )
            conflict = yield from self._conflicting_variables(code, graph)
            if conflict:
                metadata.append(conflict)
            if self.performance:
                metadata += yield from self._performance_warnings(graph, query)

            # A warning does not make the query invalid, as an error does
            return all(i["code"] in _WARNINGS for i in metadata), metadata
        except GraphNotFoundError:
            raise  # a mistake of the caller, not an answer about the query
        except CypherError as e:
            # The database rejected the query: FalkorDB.ClientError.Statement.SyntaxError,
            # FalkorDB.DatabaseError.Statement.ExecutionFailed or cypher_guardrail.Error.ArithmeticError
            metadata.append(_issues.database_error(e.code, e.message))
            return False, metadata

    def _divides_by_zero(self, code, graph) -> Generator[Request, Any, bool]:
        """Whether the query divides by an integer zero. FalkorDB answers ``inf`` when the dividend is a decimal, and
        what a property holds is asked of the graph (or of the snapshot) when the dividend is one: a property whose
        values are all decimals is no failure (a string is: the query fails, if not for the zero)."""
        for dividend in zero_divisions(code):
            if dividend is None:
                return True
            if (yield from self._may_fail_to_divide(code, graph, *dividend)):
                return True
        return False

    def _may_fail_to_divide(
        self, code, graph, variable, prop
    ) -> Generator[Request, Any, bool]:
        """Whether dividing property ``prop`` of ``variable`` may fail: the types of its values are not known (the
        variable has no label, or the property no value), or one is not a decimal."""
        owner = re.search(rf"([(\[])\s*{re.escape(variable)}\s*:\s*(`[^`]+`|\w+)", code)
        if owner is None:
            return True
        kind = "node" if owner[1] == "(" else "relationship"
        try:
            observed = yield from _queries.property_types(
                graph, owner[2].strip("`"), kind, prop, "Integer"
            )
        except GraphNotFoundError:
            raise
        except CypherError:
            return True
        return not observed or set(observed) != {"Float"}

    def _performance_warnings(
        self, graph, query
    ) -> Generator[Request, Any, list[Issue]]:
        """The warnings of the performance checks that are on. A plan that cannot be had is no reason to say more."""
        try:
            plan = yield from _queries.plan(graph, query)
        except GraphNotFoundError:
            raise
        except CypherError:
            return []
        return _performance.warnings(query, plan, self.performance)

    def _conflicting_variables(
        self, code, graph
    ) -> Generator[Request, Any, Issue | None]:
        """The issue about the variables that the query gives different labels (None when there is no such variable)."""
        # The branches of a UNION are separate queries: a variable of one is not the variable of another. Nor is a
        # name that a WITH dropped, or that a CALL { } does not import, when it is declared again
        scoped, renames = separate_variables(code)
        several = [_with_several_labels(branch) for branch in split_union(scoped)]

        # A node can legitimately carry several labels at once (e.g. GeoArea and Area), so with
        # check_multilabeled_nodes the labels that occur together on a node in the graph are not a conflict. They
        # are asked of the database only when a variable has several labels (it is a scan of the graph), and not
        # kept: the next validation sees the graph as it is then.
        multilabels = None
        if self.check_multilabeled_nodes and any(several):
            multilabels = yield Lookup("multilabels", graph, ())

        conflicting: dict[str, dict[str, None]] = {}
        for branch_variables in several:
            for var, labels in branch_variables.items():
                if multilabels is None or not _together_on_a_node(labels, multilabels):
                    conflicting.setdefault(var, {}).update(labels)
        if not conflicting:
            return None
        return _issues.conflicting_variables(
            {
                var: list(labels)
                for var, labels in restore_variables(conflicting, renames).items()
            }
        )


class SyntaxValidator(_SyntaxValidator[Driver]):
    """
    A class to validate Cypher queries for correct syntax and label consistency.
    """

    def validate(
        self,
        query: str,
        database_name: str | None = None,
        parameters: Iterable[str] | None = None,
    ) -> tuple[bool, list[Issue]]:
        """
        Checks if the query has correct Cypher syntax.
        It detects these errors and warnings:

        - FalkorDB.DatabaseError.Statement.ExecutionFailed: Raised when the database was unable to execute the statement.
        - FalkorDB.ClientError.Statement.SyntaxError: Raised when the provided statement has invalid syntax.
        - cypher_guardrail.Warning.ParameterNotProvided: Raised when a parameter is not provided.
        - cypher_guardrail.Error.WriteQuery: Raised when the query can modify the graph and ``read_only`` is True (the default).
        - cypher_guardrail.Warning.WriteQuery: The same when ``read_only`` is False. It is the only warning that leaves the
          result True: the query is valid, the warning is there to be read.
        - cypher_guardrail.Error.ArithmeticError: Raised when an arithmetic error occurs during query execution due to division or modulo by an integer zero that is in the text of the query ("/ 0", "% 0", "/ (0)", "/ (1 - 1)", "/ (2 * 0)"),
          but not when the divisor becomes zero only as the query runs (``x / (y - y)``, ``abs(0)``), and not for a decimal zero, which gives
          ``inf`` or ``nan`` in FalkorDB and is no error (``1.0 / 0``, ``1 / 0.0``).
        - cypher_guardrail.Error.ConflictingVariables: Raised when a variable is given different labels (unless they are labels that occur together on a node, see ``check_multilabeled_nodes``).

        Args:
            query (str): The Cypher query to validate.
            database_name (str | None): The graph to validate the query against (optional when a ``falkordb.Graph`` was given).
            parameters (dict | collection of str | None): The parameters you will send with the query, as the dict you
                give the client (``graph.query(q, params=...)``) or just their names. Only the names are read. A
                ``$name`` that is in neither ``parameters`` nor a ``CYPHER`` header of the query is reported as not provided.

        Returns:
            tuple: ``(is_valid, metadata)``.

            is_valid (bool): True if the syntax is correct and there is no error (a warning about a write is not one), False otherwise.

            metadata (list[dict]): The errors and warnings found, empty if there are none. Each dictionary includes:

            - 'code' (str): The error or warning code.
            - 'description' (str): A detailed message describing the issue.

        Raises:
            TypeError: If ``parameters`` is a string.
            GraphNotFoundError: If the graph does not exist.
        """
        return self.backend.run(self._validate(query, database_name, parameters))


class AsyncSyntaxValidator(_SyntaxValidator[AsyncDriver]):
    """The async version of :class:`SyntaxValidator`, for a ``falkordb.asyncio`` client or graph: ``validate`` is a coroutine."""

    def __init__(  # noqa: PLR0913 - the options of the sync validator, and max_concurrency
        self,
        driver: AsyncDriver,
        check_multilabeled_nodes: bool = True,
        read_only: bool = True,
        *,
        max_concurrency: int = 8,
        snapshot: SnapshotSource | None = None,
        verify: Verify | None = None,
        check_performance: bool | Collection[str] = False,
    ) -> None:
        """
        Initialize the validator with an async FalkorDB client.

        Args:
            driver: A ``falkordb.asyncio.FalkorDB`` client, or one of its graphs.
                ``database_name`` is the graph name (optional when a graph is given).
            check_multilabeled_nodes (bool): Whether a variable with several labels (``(a:Goal) (a:Target)``) is let
                off when a node of the graph has all of them (default: True). The label combinations of the graph
                are asked when a query has such a variable, and not kept: the next validation sees the graph as it
                is then.
            read_only (bool): Whether the queries may only read (default: True), as in :class:`SyntaxValidator`.
            max_concurrency (int): The most requests in flight at once on this validator's connection.
            snapshot (Snapshot | None): As for :class:`SyntaxValidator`: the hybrid mode.
            verify (str | None): As for :class:`SyntaxValidator`.
            check_performance (bool | Collection[str]): As for :class:`SyntaxValidator`.

        Raises:
            TypeError: If the driver is not an async FalkorDB client or graph, or ``check_performance`` is a string.
            ValueError: If ``max_concurrency`` is less than 1.
        """
        self._max_concurrency = max_concurrency
        super().__init__(
            driver,
            check_multilabeled_nodes,
            read_only,
            snapshot=snapshot,
            verify=verify,
            check_performance=check_performance,
        )

    def _open_backend(self, driver, snapshot, verify):
        return get_async_backend(
            driver, self._max_concurrency, snapshot=snapshot, verify=verify
        )

    async def validate(
        self,
        query: str,
        database_name: str | None = None,
        parameters: Iterable[str] | None = None,
    ) -> tuple[bool, list[Issue]]:
        """The async version of :meth:`SyntaxValidator.validate`: the same arguments and result."""
        return await self.backend.run(self._validate(query, database_name, parameters))
