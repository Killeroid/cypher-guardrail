# The labelled accuracy corpus

Queries with the issues a person who knows Cypher and the graph says they have, so the validators can be measured for
precision and recall, and not only compared with what they answered before (that is `tests/data/golden_results.json`).

* `graphs.json`: the graphs the queries are written for (a movie graph with indexes, and one where a node label and a
  relationship type have the same name), as the Cypher that creates them.
* `udf.json`: queries that call user-defined functions (the `scoring` library, loaded with the graph: UDFs belong to the
  instance, not to the graph, which is why `seed` and `unseed` take them from the spec of a graph).
* `syntax.json`, `schema.json`, `properties.json`: queries with the issues they must report, each with valid look-alikes
  (`near_miss`) of the issue it is about. `valid.json`: queries that must report nothing (the KG Valid Query of the README).
* `harness.py`: how a case is written, run and scored.

Run the tests with `uv run pytest tests/test_accuracy.py`, and print the precision and recall of each issue code, and of
each mode, with `uv run python scripts/accuracy.py`.

**Modes.** The schema and properties validators also run offline (from a snapshot of the graph, no database) and hybrid
(a snapshot and the database); `SyntaxValidator` needs the database, so it only runs online. The labels are the same, minus
what only `SyntaxValidator` reports. A snapshot never invents an issue, but cannot see them all: a path of two or more hops
is only a "maybe", so offline the warning about a variable-length path is missed (a `mode_gaps` entry in those cases). Hybrid
agrees with every label.

**Labels.** They are written from the Cypher and the graph, never from what the library answered. The exception is an
issue from FalkorDB itself (`SyntaxError`, `ExecutionFailed`): the database is the judge, so those labels are what the
database of the tests answers, and belong to its version. An answer that changed between major versions of FalkorDB has a case
for each, with the same query and `"servers": ["4"]` or `["6"]`; the tests and `scripts/accuracy.py` run the ones for the
server they are on (`tests/data/golden_results_6.json` does the same for `golden_results.json`).

**Known gaps.** When the library gets a case wrong, the case keeps the true label and is marked `known_gap` with the reason.
It runs as an expected failure, so it stays in the measure, and the test fails when the library gets it right (the mark is
then taken out).

**Real queries.** `scripts/false_positives.py` runs the gold queries of Neo4j's text2cypher dataset (downloaded on demand,
not stored here) through the offline validators and lists what is flagged, grouped. Reading the schema of a row is
`text2cypher.py`. A flagged query is a candidate, not a verdict: gold queries are sometimes wrong, and on the whole test split
(4438 rows, 2783 with a schema that is read) every group of findings that was checked was a mistake of the gold query.

**Adding a case.** Put it in the file of its validator, with a unique `id`. Every issue code needs at least three cases that
report it and three valid look-alikes; `tests/test_accuracy.py` checks that, so a new code cannot be added without cases.

The queries are original. The error categories come from the FalkorDB documentation and from what text-to-Cypher systems
get wrong (unknown names, wrong direction, hallucinated properties, writes, missing parameters, syntax errors).
