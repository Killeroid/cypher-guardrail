"""Capture a snapshot of a graph, save it, and validate with no database at all.

Run with: uv run python examples/offline_snapshot.py   (needs FalkorDBLite, Python >= 3.12: `uv sync` installs it)
"""

import tempfile
from pathlib import Path

from redislite.falkordb_client import FalkorDB

from cypher_guardrail import PropertiesValidator, SchemaValidator, Snapshot

GRAPH = "movies"


def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        driver = FalkorDB(str(folder / "movies.db"))
        driver.select_graph(GRAPH).query(
            "CREATE (:Person {name: 'Al Pacino'})-[:ACTED_IN]->(:Movie {title: 'Heat', year: 1995})"
        )

        # Capture once (read-only queries), then save as plain JSON
        snapshot_file = folder / "movies.snapshot.json"
        Snapshot.capture(driver, GRAPH).save(snapshot_file)

        # Later, anywhere: load it and validate without a database
        snapshot = Snapshot.load(snapshot_file)
        schema = SchemaValidator(snapshot)
        properties = PropertiesValidator(snapshot)

        for query in (
            "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) RETURN p.name, m.title",
            "MATCH (p:Person)-[:DIRECTED]->(m:Movie) RETURN m.rating",
        ):
            schema_score, schema_metadata = schema.validate(query)
            props_score, props_metadata = properties.validate(query)
            print(query)
            print(f"  schema score: {schema_score}  properties score: {props_score}")
            for issue in schema_metadata + props_metadata:
                print(f"  - {issue['code']}: {issue['description']}")


if __name__ == "__main__":
    main()
