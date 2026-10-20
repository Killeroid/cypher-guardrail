# Snapshots: validating without a database

A validator can answer its questions about the graph (does this label exist? does this path? does that label have this property?) in three ways. They differ in who answers, and you choose with the arguments of the constructor:

| | online | offline | hybrid |
|---|---|---|---|
| Create it with | `SchemaValidator(client)` | `SchemaValidator(snapshot)` | `SchemaValidator(client, snapshot=snapshot)` |
| Who answers | the database | the snapshot | the snapshot where it is sure, else the database |
| A database is needed | yes | **no** | yes |
| Round trips for a valid query | several (4 to 9 for a `SchemaValidator` query) | none | none (the syntax check still asks FalkorDB once) |
| Fresh data | always | as of the snapshot | new labels, paths and properties are found (the database is asked before a problem is reported) |
| `SyntaxValidator` | yes | **no** (`TypeError`: only FalkorDB can check syntax) | yes |

A **snapshot** is a summary of the structure of one graph: its labels, relationship types, property keys and indexes, which sets of labels occur together on a node, which relationships connect which sets of labels, and which properties the nodes of a label have. Capture it once, save it, and load it wherever you validate:

```python
from cypher_guardrail import Snapshot, SchemaValidator

# Capture: read-only queries that scan the graph once (sample=1000 would limit them to 1000 nodes per label, and relationships per type)
snapshot = Snapshot.capture(client, "kg")
# Save: written atomically, plain JSON
snapshot.save("kg.snapshot.json")

# types=True also records the types of the values of each property (the same scan, no more queries), for
# PropertiesValidator(..., check_types=True); it needs the whole graph, so it cannot be combined with sample=
snapshot = Snapshot.capture(client, "kg", types=True)

# Later, with no database
snapshot = Snapshot.load("kg.snapshot.json")

# Offline: no database_name needed
score, metadata = SchemaValidator(snapshot).validate(query)

# Hybrid
validator = SchemaValidator(client, snapshot=snapshot)
score, metadata = validator.validate(query, "kg")
```

Without a database to capture from, write one by hand: `Snapshot.from_schema(nodes={"Goal": ["code", "title"], "Target": ["code"]}, relationships=[("Goal", "HAS_TARGET", "Target")])`. It says what *can* exist; use `node_label_sets=[["GeoArea", "Area"]]` for nodes that carry several labels. Async code uses `await Snapshot.capture_async(async_client, "kg")` and `AsyncSchemaValidator(...)` the same way.

What to know:

* **A snapshot goes stale.** Offline it knows nothing newer than its capture, so a label added since is reported as unknown. In the hybrid mode a thing the snapshot says is missing is checked with the database before it is reported (`verify="misses"`, the default; `verify="never"` trusts the snapshot and is the quickest). What the hybrid cannot notice is something *deleted* since the capture: it is still reported as there (a missed warning, never a false alarm). Capture again when the schema changes.
* **A graph that does not exist.** Online, a validation raises `GraphNotFoundError` (FalkorDB answers "empty key" until a graph has had its first write). A snapshot of such a graph is empty ("no label exists"), so offline, and in the hybrid mode with `verify="never"`, every name is reported as unknown. The hybrid mode with the default `verify="misses"` asks the database and raises like the online mode.
* **A path of 2 or more hops, a property value and a variable used twice are only "maybe"s offline.** The snapshot knows which *kinds* of nodes are connected, not which real nodes: a path like `(:Src)-[:R|S*2]->(:Dst)` can exist between kinds and still not between nodes. Offline such a path is taken as existing (so the validator may miss a warning, never invent one), and a "does not exist" is always certain. In the hybrid mode those questions go to the database, so they are exact. One-hop paths are exact in every mode. A pattern the snapshot does not read (a back-quoted name, a brace inside a string, ...) is a "maybe" too, and so is never answered with a "no"; the hybrid mode asks the database. As in FalkorDB, a name that is not ASCII must be between back-quotes: offline `(:Ünï)` is a syntax error, ``(:`Ünï`)`` is not.
* **The database name.** A snapshot is of one graph. Offline you may leave `database_name` out, or give the graph's name; another name is a `ValueError`. In the hybrid mode another graph is simply answered by the database. A snapshot written by hand has no graph name and takes any.
* **Where did the answer come from?** `validator.mode` is `"online"`, `"offline"` or `"hybrid"`, and `validator.stats` counts the lookups answered by the snapshot (`snapshot_answers`) and by the database (`database_answers`), how many misses were checked (`verified_misses`) and how many of them the database contradicted (`corrected_misses`, a sign that the snapshot is stale).
* **User-defined functions (UDFs).** A capture also asks the FalkorDB instance which UDFs it has (`GRAPH.UDF LIST`: one request), and keeps their names in `snapshot.udfs`, `((library, (function, ...)), ...)`, with `snapshot.functions_of("library")` and `snapshot.function_names` (`{"library.function", ...}`) to read them. UDFs belong to the instance, not to the graph, and FalkorDB tells their names only: no signatures, and their code is never asked for or kept. A server that has no such command (or does not let the user run it) gives `None` ("not known"), which is not the same as `()` ("it has none"); `capture(..., udfs=False)` does not ask. A snapshot that has them is `format_version` 2, and one that has not stays 1, so files of servers without UDFs can still be read by older versions. A snapshot written by hand takes them as `from_schema(..., udfs={"geo": ["distance"]})`. The catalog is also what a text-to-Cypher prompt can list so that a model calls only functions that exist.
* **Capture costs a pass over the graph** (every node and relationship is scanned once). On a large graph capture rarely, or use `sample=N`, which looks at no more than N nodes of each label and N relationships of each type (the label sets, the edges and the properties are each scanned per label or per type), so a rare label or type is never lost behind a common one. It can still miss a rare property, a rare combination of labels at the two ends of a relationship type, and a node with no label. A snapshot records that it is a sample (`snapshot.sample`), and the validators do not take its "not there" for an answer: offline they report nothing about a path or a property of a known label that the snapshot lacks (an unknown name is still reported: the names are not sampled), and in the hybrid mode the database is asked, even with `verify="never"`, which then reports nothing either. Which label is blamed when a label is inferred, or chosen among alternatives, depends on the properties in the sample and can differ from the database's.

## Keeping a snapshot fresh

A snapshot goes stale. A `LiveSnapshot` holds a client and the name of a graph, and replaces its snapshot from time to time, so a service that validates many queries needs no database round trip per query and still follows the graph:

```python
from datetime import timedelta

from cypher_guardrail import LiveSnapshot, SchemaValidator

live = LiveSnapshot(
    client, "kg", refresh_every=timedelta(minutes=10), path="kg.snapshot.json"
)

validator = SchemaValidator(
    client, snapshot=live
)  # hybrid: a miss is checked with the database
validator = SchemaValidator(live)  # the validator never asks the database
```

For async code use `AsyncLiveSnapshot` with a `falkordb.asyncio` client. `current()` never waits there, so it needs a first snapshot: `async with AsyncLiveSnapshot(client, "kg", refresh_every=...) as live:` makes it (or `await live.refresh()`, or a `path=` that holds one).

* **It refreshes when it is used, not on a timer.** A validation gets the snapshot there is at once. If it was last checked more than `refresh_every` ago, a refresh also starts in the background (a daemon thread; an asyncio task for the async class) and the next validations get the result. Nothing runs while nobody validates. To refresh at a fixed rhythm, or right after a migration, call `live.refresh()` yourself.
* **A refresh looks before it scans.** It compares a *fingerprint* (the labels, relationship types, property keys, indexes, and the number of nodes and relationships: a few cheap queries) with the snapshot's, and captures the graph only if it changed, because a capture scans every node and relationship. The fingerprint is a heuristic: removing one relationship and adding another leaves the counts as they were. `refresh(force=True)` always captures, and `full_every=timedelta(hours=6)` captures anyway after that long.
* **A validation sees one snapshot.** It is given the snapshot when it starts and keeps it, so a refresh that lands in the middle can never mix two.
* **A failing refresh never reaches a validation.** The last good snapshot goes on being served, the error is in `live.last_error`, and the next try waits `retry_after` (30 seconds). With `max_age=timedelta(hours=1)` a snapshot that was not confirmed for that long is not served: `current()` raises `StaleSnapshotError` (`max_age` must be longer than `refresh_every`).
* **One refresh at a time.** Callers that arrive during a refresh get the snapshot they have.
* **`path=`** keeps a copy on disk, written atomically after each capture (a file that is replaced keeps its mode) and read when the live snapshot is created, so a restart is warm. A file that is unreadable, corrupt or of another graph is reported in `last_error` and not used; a date in it without a zone is read as UTC, and one in the future as now. It holds no fingerprint, so the first refresh after a restart captures.
* **The first `current()` of a `LiveSnapshot` that has nothing yet captures the graph and waits for it** (and raises if that fails), once however many threads ask. While the database stays down the next calls raise the error of the last try without asking it again, until `retry_after` has passed (a `refresh()` of your own always tries). In async code use `AsyncLiveSnapshot`, which does not block the event loop.
* **Forking.** A live snapshot can be made before a `fork` (a web server that preloads its application): the child has the snapshot, and no refresh is running in it whatever the parent was doing at the moment of the fork.
* **Event loops.** An `AsyncLiveSnapshot` is for one event loop at a time, with a client that belongs to it; the next loop may use it (a second `asyncio.run`) and starts with a lock and a refresh of its own. Outside an event loop `current()` serves what it has and starts no refresh.
* **What you can read:** `live.captured_at`, `live.last_checked_at`, `live.age` (how long since it was last known to be current), `live.last_error`. `live.close()` (or `with live:`) stops new refreshes.
* **Options of the capture** are `sample=`, `types=` and `udfs=` (see above); `types=True` is what `PropertiesValidator(..., check_types=True)` needs. With `udfs=True` (the default) the fingerprint includes the names of the UDFs, so a `GRAPH.UDF LOAD`, `DELETE` or `FLUSH` makes the next refresh capture again.
* **Cost.** Each full capture scans the whole graph, so on a large graph use a long `refresh_every`, rely on the fingerprint, and set `full_every` only if you must.
