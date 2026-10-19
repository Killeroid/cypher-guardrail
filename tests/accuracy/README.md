# The labelled accuracy corpus

Queries with the issues a person who knows Cypher and the graph says they have, so the validators can be measured for
precision and recall, and not only compared with what they answered before (that is `tests/data/golden_results.json`).

* `graphs.json`: the graphs the queries are written for (a movie graph with indexes, and one where a node label and a
  relationship type have the same name), as the Cypher that creates them.
* `syntax.json`, `schema.json`, `properties.json`: queries with the issues they must report, each with valid look-alikes
  (`near_miss`) of the issue it is about. `valid.json`: queries that must report nothing (the KG Valid Query of the README).
* `harness.py`: how a case is written, run and scored.

Run the tests with `uv run pytest tests/test_accuracy.py`, and print the precision and recall of each issue code with
`uv run python scripts/accuracy.py`.

**Labels.** They are written from the Cypher and the graph, never from what the library answered. The exception is an
issue from FalkorDB itself (`SyntaxError`, `ExecutionFailed`): the database is the judge, so those labels are what the
embedded engine of the tests answers, and belong to its version.

**Known gaps.** When the library gets a case wrong, the case keeps the true label and is marked `known_gap` with the reason.
It runs as an expected failure, so it stays in the measure, and the test fails when the library gets it right (the mark is
then taken out).

**Adding a case.** Put it in the file of its validator, with a unique `id`. Every issue code needs at least three cases that
report it and three valid look-alikes; `tests/test_accuracy.py` checks that, so a new code cannot be added without cases.

The queries are original. The error categories come from the FalkorDB documentation and from what text-to-Cypher systems
get wrong (unknown names, wrong direction, hallucinated properties, writes, missing parameters, syntax errors).
