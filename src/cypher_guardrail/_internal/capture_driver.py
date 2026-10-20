"""Capturing a snapshot, and taking the fingerprint of a graph, with a ``falkordb`` client or graph.

The logic is in ``cypher_guardrail._internal.sources.capture``; this runs it on a backend, sync or async.
"""

from cypher_guardrail._internal.sources import capture as _capture
from cypher_guardrail.backend import get_async_backend, get_backend
from cypher_guardrail.snapshot import Snapshot


def fingerprint(driver: object, graph: str | None = None) -> tuple:
    """The fingerprint of ``graph`` (see ``fingerprint_logic``): equal fingerprints mean the structure very likely has not changed.

    It is a heuristic: removing a relationship and adding another leaves the counts as they were.
    """
    backend = get_backend(driver)
    return backend.run(_capture.fingerprint_logic(backend.graph_name(graph)))


async def fingerprint_async(
    driver: object, graph: str | None = None, *, max_concurrency: int = 8
) -> tuple:
    """The async version of :func:`fingerprint`."""
    backend = get_async_backend(driver, max_concurrency)
    return await backend.run(_capture.fingerprint_logic(backend.graph_name(graph)))


def capture(
    driver: object,
    graph: str | None = None,
    *,
    sample: int | None = None,
    types: bool = False,
) -> Snapshot:
    """The :class:`~cypher_guardrail.snapshot.Snapshot` of ``graph`` on a ``falkordb`` client or graph."""
    _capture.check_sample(sample)
    _capture.check_types(sample, types)
    backend = get_backend(driver)
    name = backend.graph_name(graph)
    return _capture.assemble(
        name, backend.run(_capture.scan_graph(name, sample, types)), sample
    )


async def capture_async(
    driver: object,
    graph: str | None = None,
    *,
    sample: int | None = None,
    types: bool = False,
    max_concurrency: int = 8,
) -> Snapshot:
    """The async version of :func:`capture`."""
    _capture.check_sample(sample)
    _capture.check_types(sample, types)
    backend = get_async_backend(driver, max_concurrency)
    name = backend.graph_name(graph)
    return _capture.assemble(
        name, await backend.run(_capture.scan_graph(name, sample, types)), sample
    )
