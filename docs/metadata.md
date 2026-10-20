# Reading the metadata

Every item of the `metadata` that `validate()` returns is a dict with a `code` and a `description` (the text for a person). Most also carry fields that say what the issue is about, so you never have to parse the description:

```python
from cypher_guardrail.issues import UNKNOWN_LABEL

score, metadata = schema_validator.validate(
    "MATCH (g:Ghost) RETURN g", database_name=database_name
)
missing_labels = [
    issue["label"] for issue in metadata if issue["code"] == UNKNOWN_LABEL
]
print(missing_labels)  # ['Ghost']
```

| Code | Reported by | Fields |
|---|---|---|
| `cypher_guardrail.Warning.UnknownLabel` | Schema | `label` |
| `cypher_guardrail.Warning.UnknownRelationshipType` | Schema | `relationship_type` |
| `cypher_guardrail.Warning.UnknownPropertyKey` | Properties | `property` |
| `cypher_guardrail.Warning.InvalidPropertyAccess` | Properties | `kind` (`node` or `relationship`), `name`, `properties` (list) |
| `cypher_guardrail.Warning.PropertyTypeMismatch` | Properties (`check_types=True`) | `kind` (`node` or `relationship`), `name`, `property`, `expected_types` (list), `value`, `value_type` |
| `cypher_guardrail.Warning.AggregateTypeMismatch` | Properties (`check_types=True`) | `function` (`sum`, `avg`, `stDev`, `stDevP`, `percentileCont`, `percentileDisc`), `kind` (`node` or `relationship`), `name`, `property`, `types` (list: what the values are): an aggregate that needs numbers on a property that has none, which FalkorDB fails on when the query runs |
| `cypher_guardrail.Warning.UnknownIndex` | Schema | `index` (`fulltext` or `vector`), `on` (`nodes` or `relationships`), `label` (the label or relationship type), `attribute` (vector queries, else `None`) |
| `cypher_guardrail.Warning.UnknownFunction` | Schema | `name` (`library.function`, as the query writes it: a function with a dotted name that is not built in and that the instance does not have; it does not change the score, see [User-defined functions](usage.md#user-defined-functions-udfs)) |
| `cypher_guardrail.Warning.UnknownPath` | Schema | `path` (as extracted, without variables), `original_path` (as written in the query) |
| `cypher_guardrail.Warning.VariableLengthPath`, `cypher_guardrail.Warning.WrongDirectionPath`, `cypher_guardrail.Warning.WrongDirectionVariableLengthPath` | Schema | `path`, `original_path`, `suggested_path` |
| `cypher_guardrail.Error.ConflictingVariables` | Syntax | `variables` (`{variable: [labels]}`) |
| `cypher_guardrail.Warning.ParameterNotProvided` | Syntax | `parameters` (list) |
| `cypher_guardrail.Error.WriteQuery`, `cypher_guardrail.Warning.WriteQuery` | Syntax | none: the query can modify the graph (see [Read-only queries](usage.md#read-only-queries)) |
| `cypher_guardrail.Warning.CartesianProduct`, `cypher_guardrail.Warning.UnlabeledNodeScan`, `cypher_guardrail.Warning.UnboundedPath` | Syntax (`check_performance=True`) | `variables` (list of lists), `variable`, `path`: valid queries that may be expensive, see [Queries that are valid and may be expensive](usage.md#queries-that-are-valid-and-may-be-expensive-opt-in) |
| `cypher_guardrail.Error.UnboundVariable` | Syntax | `variable`: the query uses a variable that no clause before it defines (FalkorDB: `'x' not defined`) |
| `cypher_guardrail.Error.UnknownFunction` | Syntax | `name`: the database does not know a function the query calls (`Unknown function 'f'`). `cypher_guardrail.Warning.UnknownFunction` says it of a function with a dotted name, without a database |
| `cypher_guardrail.Error.InvalidLimit` | Syntax | `clause` (`LIMIT` or `SKIP`): the value is not a positive integer |
| `FalkorDB.ClientError.Statement.SyntaxError`, `FalkorDB.DatabaseError.Statement.ExecutionFailed`, `cypher_guardrail.Error.ArithmeticError` | Syntax | none: the message is all there is |

* **Plain data.** The fields are strings, lists and dicts, so an issue can be turned into JSON as it is. For `cypher_guardrail.Warning.InvalidPropertyAccess`, a name with alternatives (`A|B`) is reported as the first one.
* **Node label or relationship type.** In `cypher_guardrail.Warning.InvalidPropertyAccess` and `cypher_guardrail.Warning.PropertyTypeMismatch`, `kind` says which of the two `name` is, as the query writes it (`(n:Movie)` is a `node`, `[r:ACTED_IN]` a `relationship`). A property issue about a name that `SchemaValidator` reported as unknown repeats that report. Match a `node` with `UnknownLabel` and a `relationship` with `UnknownRelationshipType`, never by the name alone: a node label and a relationship type can have the same name.

  ```python
  unknown = {
      ("node", i["label"]) for i in schema_metadata if i["code"] == UNKNOWN_LABEL
  } | {
      ("relationship", i["relationship_type"])
      for i in schema_metadata
      if i["code"] == UNKNOWN_REL_TYPE
  }
  real = [i for i in props_metadata if (i["kind"], i["name"]) not in unknown]
  ```
* **Constants and types.** `cypher_guardrail.issues` has a constant for every code (`UNKNOWN_LABEL`, `UNKNOWN_PATH`, ...) and a `TypedDict` for every kind of issue. `Issue` is the union of them, so a type checker can tell the fields apart by `code`.
* **Codes.** Every code of the library starts with `cypher_guardrail.`, followed by `Warning` or `Error`. The two codes of FalkorDB itself keep their own names.
