# API reference

## Validators

Each validator has an async twin for the `falkordb.asyncio` client, with the same arguments and results.

```{eval-rst}
.. autoclass:: cypher_guardrail.SyntaxValidator
   :members:
.. autoclass:: cypher_guardrail.AsyncSyntaxValidator
   :members:
.. autoclass:: cypher_guardrail.SchemaValidator
   :members:
.. autoclass:: cypher_guardrail.AsyncSchemaValidator
   :members:
.. autoclass:: cypher_guardrail.PropertiesValidator
   :members:
.. autoclass:: cypher_guardrail.AsyncPropertiesValidator
   :members:
```

## Snapshots

```{eval-rst}
.. autoclass:: cypher_guardrail.Snapshot
   :members:
.. autoclass:: cypher_guardrail.SnapshotSource
   :members:
.. autoclass:: cypher_guardrail.LiveSnapshot
   :members:
.. autoclass:: cypher_guardrail.AsyncLiveSnapshot
   :members:
.. autoexception:: cypher_guardrail.StaleSnapshotError
```

## Helpers

```{eval-rst}
.. autofunction:: cypher_guardrail.utils.is_write
.. autofunction:: cypher_guardrail.utils.is_write_async
```

## Issues

The types and codes of the items in the `metadata` that `validate()` returns. See [Reading the metadata](metadata.md).

```{eval-rst}
.. automodule:: cypher_guardrail.issues
   :members:
```
