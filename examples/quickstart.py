"""Validate a few queries against a small movie graph, with an embedded FalkorDB (no server needed).

Run with: uv run python examples/quickstart.py   (needs FalkorDBLite, Python >= 3.12: `uv sync` installs it)
"""

import tempfile
from pathlib import Path

from redislite.falkordb_client import FalkorDB

from cypher_guardrail import PropertiesValidator, SchemaValidator, SyntaxValidator

GRAPH = "movies"
QUERIES = {
    "valid": "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) WHERE m.title = 'Heat' RETURN p.name",
    "unknown label": "MATCH (p:Actor)-[:ACTED_IN]->(m:Movie) RETURN p",
    "unknown property": "MATCH (m:Movie) WHERE m.rating > 8 RETURN m.title",
    "writes": "MATCH (m:Movie) DETACH DELETE m",
}


def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        driver = FalkorDB(str(Path(folder) / "movies.db"))
        driver.select_graph(GRAPH).query(
            "CREATE (:Person {name: 'Al Pacino'})-[:ACTED_IN]->(:Movie {title: 'Heat', year: 1995})"
        )

        syntax = SyntaxValidator(driver)
        schema = SchemaValidator(driver)
        properties = PropertiesValidator(driver)

        for name, query in QUERIES.items():
            is_valid, syntax_metadata = syntax.validate(query, database_name=GRAPH)
            schema_score, schema_metadata = schema.validate(query, database_name=GRAPH)
            props_score, props_metadata = properties.validate(
                query, database_name=GRAPH
            )
            kg_valid = is_valid and schema_score == 1 and props_score in (1, None)

            print(f"{name}: {query}")
            print(
                f"  syntax valid: {is_valid}  schema score: {schema_score}  properties score: {props_score}"
            )
            print(f"  KG valid query: {kg_valid}")
            for issue in syntax_metadata + schema_metadata + props_metadata:
                print(f"  - {issue['code']}")


if __name__ == "__main__":
    main()
