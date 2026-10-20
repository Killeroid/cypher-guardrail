# How it is built

A short map for someone who is going to change the code. What the library does is in [Using the validators](usage.md);
this is how the pieces fit.

## The idea: logic that does not know how it is run

Everything that needs the database is written once, as a Python **generator**. Where it needs something it `yield`s a
request and is sent the answer:

```python
rows = yield RoQuery("kg", "MATCH (n) RETURN count(n)")
```

The logic never knows whether it runs in a thread or in an event loop, or whether a database answers at all. A *driver*
runs it: `run_sync` for the sync classes, `run_async` for the `Async*` twins. Errors are thrown into the generator at
the `yield`, so `try/except` in the logic reads as it would around a plain call. This is why every validator has an
async twin with the same behaviour and no second copy of the logic: `_SchemaValidator` holds the generators,
`SchemaValidator` and `AsyncSchemaValidator` only choose how to run them (`cypher_guardrail/_internal/core/io.py` has
the full description).

The requests are few:

| Request | What it is |
|---|---|
| `RoQuery` | `GRAPH.RO_QUERY`: a read-only query |
| `Explain` | `GRAPH.EXPLAIN` (the answer is the plan), or the read-only form that asks whether a query can write |
| `ListUdfs` | `GRAPH.UDF LIST` |
| `Lookup` | a **question about the graph** ("does this path exist?", "which of these labels are unknown?") |
| `Concurrently` | generators that do not depend on each other: the async driver runs them at the same time |
| `Remember`, `Recall` | a value kept for the length of one validation, never sent anywhere |

Inside a validation the answer to a request is kept, so asking the same thing twice costs one round trip.

## Lookups and the three modes

A `Lookup` is not a query: it names a question, and a *resolver* answers it. That is what makes the three modes one
code path:

* **online**: the resolver in `_internal/sources/online.py` asks the database (with `RoQuery`);
* **offline**: `SnapshotResolver` (`_internal/sources/offline.py`) answers from a `Snapshot` and asks nothing. Where the
  snapshot cannot be sure (a path of 2 or more hops is only a "maybe") the answer says so;
* **hybrid**: `chain_resolver` (`_internal/sources/hybrid.py`) takes the snapshot's answer where it is sure and asks the
  database for the rest, and checks a "not there" with the database before it is reported (`verify="misses"`).

The backend (`backend.py`: `FalkorDBBackend`, `SnapshotBackend` and their async twins) is what a validator holds: it
chooses the transport and the resolver, and runs the generators.

## The layers

The package is layered, and a test enforces it (`uv run lint-imports`): a module imports from the layers below it and
never from one above.

```
validators              the three validators and their async twins
live, utils             LiveSnapshot, and helpers such as is_write
_internal.capture_driver   runs a capture or a fingerprint on a backend
backend                 the backends, and the decorators that pin a snapshot for a validation
_internal.sources       online, offline, hybrid, capture: the answers to the lookups
snapshot                Snapshot, SnapshotSource
_internal.core          requests and drivers (io), queries, issue builders, plans, scopes, literals ...
issues                  the codes and the types of the issues
patterns, drivers       regular expressions of Cypher, and the types of what a validator is given
```

The one import that goes against it is on purpose: `Snapshot.capture` (a method of a data class) imports the capture
that is written above it, inside the method.

## What the validators read

* **Text.** Queries are read with regular expressions and small tokenisers (`patterns`, `_internal/core/literals.py`,
  `scopes.py`). Strings and comments are hidden first (`strip_literals_and_comments`), a `UNION` is split into its
  branches, and a name that a `WITH` dropped is renamed while the query is read and given back in the result
  (`scopes.separate_variables` and `restore_variables`).
* **The database.** FalkorDB decides what is valid (`EXPLAIN`), what can write (a read-only request refuses it), and,
  with `check_performance`, what is expensive: the plan (`_internal/core/plans.py`, `performance.py`) is read, never the
  text alone.
* **A snapshot**, for the lookups that do not need the database.

The result of a validation is a score and a list of **issues**. An issue is a plain `TypedDict` (`issues.py`) with a
`code`, a `description` and the fields that say what it is about; the builders that make them are in
`_internal/core/builders.py`.

## Snapshots and live snapshots

A `Snapshot` is a frozen summary of one graph (names, indexes, label sets, edges, properties, optionally the types of
the values and the user-defined functions), saved as JSON (`format_version` 1, or 2 when it has `udfs`). A
`LiveSnapshot` keeps one fresh: `_Tracker` in `live.py` holds the policy (what is due, what a failure does) and does no
I/O, so the sync and async classes share it and a test can drive it with a fake clock.

## How to add a check

1. Add the code and the type of the issue to `issues.py`, and its builder to `_internal/core/builders.py`.
2. Put the logic where the data is: a generator in `_internal/core` or in the validator. If it needs a fact about the
   graph, make it a `Lookup`, and write its three answers (online, offline, hybrid), so the three modes agree.
3. Write the labelled cases first, in `tests/accuracy/` (each code needs three cases that report it and three valid
   look-alikes), and the unit tests. `uv run python scripts/accuracy.py` prints precision and recall.
4. Document it in [Reading the metadata](metadata.md) and the guide it belongs to, and in the changelog.

## What keeps it honest

Every change runs these (CI runs them on every push):

```
uv run ruff check . && uv run ruff format --check .
uv run ty check
uv run lint-imports                        # the layers
uv run python scripts/metrics.py --check   # complexity 10, functions of 60 lines, no name-mangled methods
uv run coverage run -m pytest && uv run coverage report   # at least 95%
uv run python scripts/accuracy.py          # the labelled corpus, in the three modes
```

Two recorded files hold the behaviour still: `tests/data/golden_results.json` (714 answers of the validators to 102
queries, to the last digit of a score) and `tests/data/request_trace.json` (which requests reach the database, in which
order). A change that is meant to alter them re-records them on purpose
(`uv run python tests/test_corpus.py`).
