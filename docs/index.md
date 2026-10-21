# cypher-guardrail

Syntax, schema and property-access validation of Cypher queries against a [FalkorDB](https://www.falkordb.com/) graph.
It checks a query before it runs and never changes your data. It does not run your query either, apart from one request (see [How validation works](usage.md#how-validation-works-on-falkordb)).

Used by [text2cypher](https://github.com/Killeroid/text2cypher), which turns a question in plain language into a read-only Cypher query: an LLM writes the query, cypher-guardrail checks it against your graph, and what is wrong is sent back to the LLM until the query passes.

```{toctree}
:maxdepth: 2
:caption: Guides

usage
metadata
snapshots
async
```

```{toctree}
:maxdepth: 2
:caption: Reference

reference
architecture
changelog
```
