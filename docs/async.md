# Async usage

Each validator has an async twin for the `falkordb.asyncio` client: `AsyncSyntaxValidator`, `AsyncSchemaValidator` and `AsyncPropertiesValidator`. They take the same arguments and return the same results as the sync validators; their `validate()` and `extract()` are coroutines.

```python
import asyncio
from falkordb.asyncio import FalkorDB
from cypher_guardrail import AsyncSchemaValidator


async def main():
    driver = FalkorDB(host="localhost", port=6379)
    try:
        schema_validator = AsyncSchemaValidator(driver, max_concurrency=8)
        query = "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g, t"
        schema_score, schema_metadata = await schema_validator.validate(
            query, database_name="my_graph"
        )
        print(f"Schema Validation Score: {schema_score}")
    finally:
        await driver.aclose()


asyncio.run(main())
```

* **One implementation.** The sync and the async validators run the same validation logic, so they give identical results and make the same requests to the database.
* **Pass an async client.** `driver` is a `falkordb.asyncio.FalkorDB` client or one of its graphs (then `database_name` can be omitted). The sync validators refuse an async client, and the async validators a sync one, with an error that names the right classes.
* **Independent lookups run at the same time.** Within one validation the checks that do not depend on each other are asked together: whether each node, relationship and path of the query exists, the label inference of its paths, and the property checks. With a simulated 20 ms round trip, validating a query with three paths took about 300 ms before and takes about 50 ms now. The same requests are asked as before, so the results do not change.
* **`max_concurrency`** (default 8) is the most requests the validator has in flight at once on its connection, across everything it is validating. Set it to what your database and connection can take; `1` asks one thing at a time. It must be at least 1.
* **One client per event loop.** The async FalkorDB client is bound to the event loop that first used it, so create it, and the validators that use it, inside the loop that runs them.
* **Validating many queries together** shares their schema lookups when you run them inside a snapshot. Each lookup (is this a label? does it have this property?) is then asked once, even by queries running at the same time, and nothing is kept after the block ends:

  ```sh
  validator = AsyncSchemaValidator(driver)
  with validator.backend.snapshot():
      results = await asyncio.gather(*(validator.validate(q, database_name) for q in queries))
  ```
* **Timeouts and cancellation work.** `await asyncio.wait_for(validator.validate(query, database_name), timeout=5)` cancels the queries it started when it times out, which frees their `max_concurrency` slots for the next call.

## Using FalkorDBLite's async client

cypher-guardrail works with any `falkordb.asyncio` client, but FalkorDBLite 0.10.0's own `AsyncFalkorDB` (`redislite.async_falkordb_client`) has a bug on exit: you see `RuntimeWarning: coroutine 'AsyncManagementCommands.shutdown' was never awaited`. It is not only cosmetic. FalkorDBLite then waits about 5 seconds and kills its embedded server with `SIGKILL` without saving, so exiting is slower and **data written to a persistent database file can be lost**. The problem is in FalkorDBLite ([#61](https://github.com/FalkorDB/falkordblite/issues/61)), not in cypher-guardrail.

Until it is fixed, let the sync embedded client own the server and attach the regular async client to its socket:

```python
from falkordb.asyncio import FalkorDB
from redislite.falkordb_client import FalkorDB as EmbeddedFalkorDB

embedded = EmbeddedFalkorDB(
    "/path/to/graph.db"
)  # starts the server, stops it cleanly with a save
client = FalkorDB(unix_socket_path=embedded.client.socket_file)
try:
    validator = AsyncSchemaValidator(client)
    score, metadata = await validator.validate(query, "my_graph")
finally:
    await client.aclose()
```

If you keep using `AsyncFalkorDB`, shut the server down yourself before leaving the `async with`:

```python
with contextlib.suppress(redis.RedisError):
    await db.connection.shutdown(save=True, now=True, force=True)
```

Don't set `_async_managed = True` to silence the warning: it leaves the server running after your program exits.
