"""Validate several queries at once with the async validators.

The embedded server is owned by the sync FalkorDBLite client and the async client is attached to its socket
(see docs/async.md for why). With a real server, create `falkordb.asyncio.FalkorDB(host=..., port=...)` instead.

Run with: uv run python examples/async_validation.py   (needs FalkorDBLite, Python >= 3.12: `uv sync` installs it)
"""

import asyncio
import tempfile
from pathlib import Path

from falkordb.asyncio import FalkorDB as AsyncFalkorDB
from redislite.falkordb_client import FalkorDB as EmbeddedFalkorDB

from cypher_guardrail import AsyncSchemaValidator

GRAPH = "movies"
QUERIES = [
    "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) RETURN p.name",
    "MATCH (p:Person)-[:ACTED_IN]->(m:Film) RETURN p.name",
    "MATCH (m:Movie)-[:ACTED_IN]->(p:Person) RETURN p.name",
]


async def main(socket_file):
    client = AsyncFalkorDB(unix_socket_path=socket_file)
    try:
        validator = AsyncSchemaValidator(client, max_concurrency=8)
        # One snapshot block shares the schema lookups of all the queries
        with validator.backend.snapshot():
            results = await asyncio.gather(
                *(validator.validate(q, GRAPH) for q in QUERIES)
            )
        for query, (score, metadata) in zip(QUERIES, results, strict=True):
            print(f"{score:.2f}  {query}")
            for issue in metadata:
                print(f"      - {issue['code']}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as folder:
        embedded = EmbeddedFalkorDB(str(Path(folder) / "movies.db"))
        embedded.select_graph(GRAPH).query(
            "CREATE (:Person {name: 'Al Pacino'})-[:ACTED_IN]->(:Movie {title: 'Heat'})"
        )
        asyncio.run(main(embedded.client.socket_file))
