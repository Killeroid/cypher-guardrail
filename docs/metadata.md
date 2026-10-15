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
| `cypher_guardrail.Warning.InvalidPropertyAccessByLabel` | Properties | `label`, `properties` (list) |
| `cypher_guardrail.Warning.PropertyTypeMismatch` | Properties (`check_types=True`) | `label`, `property`, `expected_types` (list), `value`, `value_type` |
| `cypher_guardrail.Warning.UnknownIndex` | Schema | `index` (`fulltext` or `vector`), `on` (`nodes` or `relationships`), `label` (the label or relationship type), `attribute` (vector queries, else `None`) |
| `cypher_guardrail.Warning.UnknownPath` | Schema | `path` (as extracted, without variables), `original_path` (as written in the query) |
| `cypher_guardrail.Warning.VariableLengthPath`, `cypher_guardrail.Warning.WrongDirectionPath`, `cypher_guardrail.Warning.WrongDirectionVariableLengthPath` | Schema | `path`, `original_path`, `suggested_path` |
| `cypher_guardrail.Error.ConflictingVariables` | Syntax | `variables` (`{variable: [labels]}`) |
| `cypher_guardrail.Warning.ParameterNotProvided` | Syntax | `parameters` (list) |
| `cypher_guardrail.Error.WriteQuery`, `cypher_guardrail.Warning.WriteQuery` | Syntax | none: the query can modify the graph (see [Read-only queries](usage.md#read-only-queries)) |
| `FalkorDB.ClientError.Statement.SyntaxError`, `FalkorDB.DatabaseError.Statement.ExecutionFailed`, `cypher_guardrail.Error.ArithmeticError` | Syntax | none: the message is all there is |

* **Plain data.** The fields are strings, lists and dicts, so an issue can be turned into JSON as it is. For `cypher_guardrail.Warning.InvalidPropertyAccessByLabel`, a label with alternatives (`A|B`) is reported as the first one.
* **Constants and types.** `cypher_guardrail.issues` has a constant for every code (`UNKNOWN_LABEL`, `UNKNOWN_PATH`, ...) and a `TypedDict` for every kind of issue. `Issue` is the union of them, so a type checker can tell the fields apart by `code`.
* **Codes.** Every code of the library starts with `cypher_guardrail.`, followed by `Warning` or `Error`. The two codes of FalkorDB itself keep their own names.
