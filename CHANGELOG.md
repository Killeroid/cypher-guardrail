# Changelog

## 1.4.0 (unreleased)

### Added

* **A snapshot keeps the user-defined functions (UDFs) of the FalkorDB instance.** `Snapshot.capture` (and `capture_async`, and `LiveSnapshot`) also asks `GRAPH.UDF LIST` (one request, names only: FalkorDB tells no signatures, and the code of a function is never asked for or kept) and keeps the libraries and their functions in `snapshot.udfs`, with `functions_of(library)`, `function_names` and `has_udfs` to read them, and `from_schema(..., udfs=...)` to write them by hand. `udfs=False` does not ask. A server that has no such command, or does not let the user run it, gives `None`; a server with no UDFs gives `()`. UDFs belong to the instance, so a graph that does not exist yet still has them.
* **The fingerprint of a live snapshot includes the UDFs**, so a `GRAPH.UDF LOAD`, `DELETE` or `FLUSH` makes the next refresh capture again.

### Changed

* **A snapshot with UDFs is `format_version` 2.** A snapshot without them (a server that has none to list, `udfs=False`, or written by hand) is still 1, so older versions can read it; a file of format 1 still loads here. Older versions refuse a file of format 2 with "Unknown snapshot format_version 2".

## 1.3.0 (2026-10-20)

### Added

* **The public API is fully type annotated.** Every parameter and result of the validators, the snapshots, `LiveSnapshot` and `AsyncLiveSnapshot`, `GraphNotFoundError` and the helpers of `cypher_guardrail.utils` has a type, and a test keeps it so. `cypher_guardrail.drivers` has the types of what they take: `Driver` (a `falkordb.FalkorDB` client, FalkorDBLite or a `falkordb.Graph`), `AsyncDriver` (the same for `falkordb.asyncio`) and `Verify` (`"misses"` or `"never"`). The sync validators and `LiveSnapshot` take a `Driver`, and the async ones an `AsyncDriver`, so a type checker now turns away an async client given to a sync validator. `validate()` returns `tuple[float, list[Issue]]` (`tuple[bool, ...]` for `SyntaxValidator`, `tuple[float | None, ...]` for `PropertiesValidator`).
* **`cypher_guardrail.CypherError`** is exported from the package (it was only importable from `cypher_guardrail.backend`).

### Deprecated

* **The codes and `CypherError` are no longer to be imported from `cypher_guardrail.backend`**, and `identify_variables_and_construct_query` no longer from `cypher_guardrail.utils`. The old imports work and warn (`DeprecationWarning`); they are removed in 2.0. `CypherError` is `cypher_guardrail.CypherError` (it is the base of `GraphNotFoundError`, and what the validators raise when the database rejects a query), and the codes (`UNKNOWN_LABEL`, `SYNTAX_ERROR`, ...) are in `cypher_guardrail.issues`.

## 1.2.0 (2026-10-20)

### Changed

* **A graph that does not exist is an error, `GraphNotFoundError`, and no longer a misleading answer.** FalkorDB creates a graph with its first write and answers "empty key" to everything until then. `SyntaxValidator` reported `ExecutionFailed` for every query, `SchemaValidator` scored 0 with no issue, and `PropertiesValidator` blamed each label for the properties; all three, online or hybrid, and `is_write`, now raise `GraphNotFoundError`, which names the graph. It is a `CypherError` (what `is_write` raised) and a `ValueError`, and is exported by the package. It comes from the requests the validation made anyway, so none is added: a query that asks nothing about the graph (`RETURN 1`), `extract()` of a query whose labels are in its text, an offline validator and `Snapshot.capture` (an empty snapshot) are as before.

### Internal

* **The code was reorganised, with the same answers.** The validators were split into small functions (none is over 10 in cyclomatic complexity or 60 lines), the package has layers that a test enforces (`import-linter`), and the issue codes are defined in `cypher_guardrail.issues`. Every answer of the validators is the same, to the last digit of a score: the recorded answers of 714 queries, the order of the requests sent to the database, and the labelled accuracy corpus did not change. Nothing public was renamed (`cypher_guardrail.backend` still exports the codes); the private modules and methods were (`__name` methods are `_name`, and `_internal.sources.capture` no longer wraps a driver: see `_internal.capture_driver`).

## 1.1.1 (2026-10-20)

### Fixed

* **A `$parameter` on a label that has an index, or in `LIMIT` / `SKIP`, no longer makes `SyntaxValidator` report `ExecutionFailed`.** FalkorDB cannot plan such a query without the value of the parameter and answers "Missing parameters", so a valid query was flagged even when the parameter was given in `parameters=`, and a parameter that was not given was reported as `ExecutionFailed` and not as `ParameterNotProvided`. The validator now plans the query again with a placeholder for each parameter (only when FalkorDB asks for a value), so the answer is the one for the query itself.
* **A variable-length path with no upper bound (`*3..`) is read and checked.** It was skipped, so `MATCH (a:Person)-[:KNOWS*3..]->(b:Person)` was never reported as `VariableLengthPath`.
* **`WrongDirectionPath` is no longer reported for a multi-hop path that only exists undirected.** The check looked for the path without its arrows, which over several hops can mix directions, so the suggested path could have no rows. It now looks for the flipped path itself; if that does not exist either, the path is reported as `WrongDirectionVariableLengthPath` (it exists the other way round with any length) or as `UnknownPath`.

## 1.1.0 (2026-10-19)

### Changed

* **`InvalidPropertyAccess` and `PropertyTypeMismatch` say whether they are about a node label or a relationship type.** They have `kind` (`"node"` or `"relationship"`, as the query writes the name) and `name`, which replaces `label` (it held either). A consumer can tell an issue that only repeats an unknown label or relationship type from a real one, even when a node label and a relationship type have the same name (see "Reading the metadata").
* **`cypher_guardrail.Warning.InvalidPropertyAccessByLabel` is now `cypher_guardrail.Warning.InvalidPropertyAccess`**: it is not only about labels. The description of an issue about a relationship type says "The relationship type X does not have the following properties".
* **`PropertiesValidator.extract()` returns the labels split by kind**: `{"node": {label: properties}, "relationship": {type: properties}}`, where it returned one dict keyed by name.

### Fixed

* **A node label and a relationship type with the same name no longer share their properties.** With a `Rated` label (property `note`) and a `Rated` relationship type (property `stars`), `MATCH (a)-[r:Rated]->(b:Rated) RETURN r.stars, b.note` was reported as having an invalid property and scored 0.5, and `r.note, b.stars` was reported for one property only. Each is now checked against its own kind.
* The properties validator no longer asks the database whether a name is a node label or a relationship type, as the query says which it is. One request fewer for each label.

## 1.0.0 (2026-10-19)

First release of cypher-guardrail, licensed under Apache 2.0. Requires Python 3.11 or later and a FalkorDB graph (a server, or the embedded FalkorDBLite).

### Validators

* **`SyntaxValidator`** asks FalkorDB itself whether a query is valid (`GRAPH.EXPLAIN`, so the query is never run), so whatever the server supports passes: vector functions, `db.idx.*` and `algo.*` procedures, index commands.
  * Rejects a query that can modify the graph (`cypher_guardrail.Error.WriteQuery`) unless the validator is created with `read_only=False`, which accepts it with `cypher_guardrail.Warning.WriteQuery`. `is_write` and `is_write_async` ask only that question.
  * Reports `$parameters` that are not provided (`cypher_guardrail.Warning.ParameterNotProvided`). A `CYPHER` header provides its own; the ones a client sends apart from the query are named with `parameters=`.
  * Reports a variable that carries conflicting labels (`check_multilabeled_nodes`), and an empty or blank query without asking the database.
* **`SchemaValidator`** checks that the labels, relationship types and paths of a query exist in the graph, and that a path is written in the right direction (`UnknownLabel`, `UnknownRelationshipType`, `UnknownPath`, `WrongDirectionPath`, `VariableLengthPath`). It also checks that a full-text or vector index query procedure has an index to use (`UnknownIndex`). The score is explained in the README.
* **`PropertiesValidator`** checks that each property exists on the label it is read from (`UnknownPropertyKey`, `InvalidPropertyAccessByLabel`), inferring the label of a variable that has none. With `check_types=True` it warns when a property is compared with a literal of a type that none of its values has (`PropertyTypeMismatch`); this warning does not change the score.
* **`UNION` and scopes.** Each branch of a `UNION` is validated on its own, and a name that a `WITH` or a `CALL { }` does not carry on is a new variable, as in Cypher.
* **Names between back-quotes** (labels, relationship types and property keys with a space or a symbol) are read and checked.

### Three ways to validate

* **Online**: the database answers every question (`SchemaValidator(client)`).
* **Offline**: a `Snapshot`, a JSON summary of the structure of one graph (labels, relationship types, property keys, indexes, label sets, edges and the properties of each label), answers them, with no database (`SchemaValidator(snapshot)`). Capture it with `Snapshot.capture` (`sample=` limits the scans, `types=True` records the types of the values), save and load it with `save` / `load`, or write one by hand with `Snapshot.from_schema`.
* **Hybrid**: the snapshot answers where it is sure, and the database is asked before a problem is reported (`SchemaValidator(client, snapshot=snapshot)`).
* **`LiveSnapshot` and `AsyncLiveSnapshot`** keep a snapshot fresh from a live graph: they refresh in the background, compare a fingerprint before they scan, keep a copy on disk, and serve the last good snapshot when a refresh fails.

### Async

* `AsyncSyntaxValidator`, `AsyncSchemaValidator` and `AsyncPropertiesValidator` for the `falkordb.asyncio` client. They run the same validation logic as the sync validators, so results are identical. Independent lookups run together, up to `max_concurrency` (keyword-only, default 8).

### Results

* Every validator returns a verdict or score plus `metadata`: a list of issues, each a dict with a `code`, a `description` and fields that say what it is about, so nothing has to be parsed from text. `cypher_guardrail.issues` has a constant for every code and a `TypedDict` for every kind of issue.
* Every code starts with `cypher_guardrail.` followed by `Warning` or `Error` (for example `cypher_guardrail.Warning.UnknownLabel`). FalkorDB's own errors keep their names (`FalkorDB.ClientError.Statement.SyntaxError`, `FalkorDB.DatabaseError.Statement.ExecutionFailed`).
* FalkorDB sends no notifications, so the unknown label, relationship type and property key warnings are derived from `db.labels()`, `db.relationshipTypes()` and `db.propertyKeys()`.

### Documentation and examples

* The scores and the KG Valid Query check are written out in the README. Longer guides are on [Read the Docs](https://cypher-guardrail.readthedocs.io/en/latest/): usage, reading the metadata, snapshots and async.
* Runnable examples in `examples/` that need no server (they use FalkorDBLite): a quickstart, offline validation with a snapshot, and async validation.
