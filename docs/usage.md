# Using the validators

## Importing the Library

```python
from cypher_guardrail import SyntaxValidator, SchemaValidator, PropertiesValidator
```

## Connect to your database

Pass a FalkorDB client to the validators as `driver`. `database_name` is the name of the graph:

```python
from falkordb import FalkorDB

driver = FalkorDB(host="localhost", port=6379)
database_name = "my_graph"
```

You can also pass a graph directly, in which case `database_name` can be omitted:

```python
driver = FalkorDB(host="localhost", port=6379).select_graph("my_graph")
```

[FalkorDBLite](https://pypi.org/project/falkordblite/) (`pip install falkordblite`, Python >= 3.12), an embedded FalkorDB, works too:

```python
from redislite.falkordb_client import FalkorDB

driver = FalkorDB("/path/to/graph.db")
```

### How validation works on FalkorDB

* Validation never executes or modifies your data: queries are checked with `GRAPH.EXPLAIN` and everything else runs read-only (`GRAPH.RO_QUERY`). One FalkorDB quirk touches the graph's schema, though, see the last bullet.
* FalkorDB sends no warnings of its own, so cypher-guardrail derives the *unknown label / relationship type / property* warnings from the graph schema, and detects missing `$parameters` from the query text.
* Cypher that FalkorDB cannot parse, such as `(n:!Label)`, `[:A&B]` or `(n:A|B)`, is reported as a syntax error by `SyntaxValidator`.
* **On older FalkorDB versions, a write with properties can make a property name look known.** Up to FalkorDB 4.18.8, which includes the 4.18.3 engine that FalkorDBLite 0.10.0 bundles, `CREATE (:X {p: 1})` and `MERGE (:X {p: 1})` register `p` in `db.propertyKeys()` as soon as the query is planned, even for `GRAPH.EXPLAIN` and for a `GRAPH.RO_QUERY` that is then refused (no node, relationship, label or index is created, and `SET n.p = 1` and reads never did this). `SyntaxValidator` checks every query with `EXPLAIN`, and with `read_only` it also hands the query to `RO_QUERY`, so validating such a write registers `p`. Afterwards `cypher_guardrail.Warning.UnknownPropertyKey` no longer fires for `p`, and a `Snapshot` captured later lists it. The effect is on that warning only (a property that no node with the label has is still reported by `cypher_guardrail.Warning.InvalidPropertyAccessByLabel`, which looks at the data). FalkorDB fixed it in 4.18.9 and 4.20.0: the names are now registered only when a write really runs. On an older engine, validate against a copy of the graph if you need the list of property names to stay exactly as your data has it.
* **A graph that does not exist.** FalkorDB creates a graph with its first write and answers every request about it with "Invalid graph operation on empty key" until then. A validation that asked the database about such a graph (`SyntaxValidator`, `SchemaValidator` and `PropertiesValidator` online or hybrid, and `is_write`) raises `GraphNotFoundError`, which names the graph: a mistake of the caller, usually the name of the graph, and not an answer about the query. It is a `CypherError` and a `ValueError`. A query that asks nothing about the graph (`RETURN 1`), `extract()` of a query whose labels are in its text, and an offline validator (below) raise nothing.
* **FalkorDB 4.22.1 crashes while planning some queries.** `EXPLAIN` of a query with an exact-length variable-length hop and a property filter on the node it reaches, such as `MATCH (a:A)-[r*2]->(b:B {p: 2}) RETURN a`, stops the server (verified on FalkorDBLite 0.11.0, which bundles 4.22.1; the same query is fine on 4.18.3, and other versions were not checked). It is a bug of the engine (reported as [FalkorDB/FalkorDB#3416](https://github.com/FalkorDB/FalkorDB/issues/3416)): `SyntaxValidator` only ever sends `EXPLAIN`, and the query would stop the server when you run it too, but a generated query meets the validator first.

### FalkorDB's own extensions to Cypher

* **Syntax:** `SyntaxValidator` asks FalkorDB itself, so whatever your server version supports passes (vector functions such as `vecf32`, `db.idx.*` and `algo.*` procedures, index commands such as `CREATE VECTOR INDEX`, ...) and whatever it does not is reported (an unregistered procedure, `shortestPath` inside `MATCH`, ...). cypher-guardrail does not need to know about them.
* **Schema and properties:** these validators understand node, relationship and path patterns and `variable.property` accesses, plus one extension: the full-text and vector index query procedures. In `CALL db.idx.fulltext.queryNodes('Goal', ...) YIELD node` (and `queryRelationships`, `db.idx.vector.queryNodes` / `queryRelationships`) the label or relationship type is checked like one in a pattern, the properties read from the yielded `node` / `relationship` are checked against it, and for a vector query so is its attribute. `SchemaValidator` also checks that the graph has a full-text or vector index for the call (`cypher_guardrail.Warning.UnknownIndex`), and its score is then the average of the pattern score and the share of the calls that have an index. This matters because FalkorDB answers an unknown label, or a label without an index, with an empty result, not an error.
* **Not checked:** a label that is not a plain string in the call (a `$parameter`, say), whether a vector index's dimension fits the vector you pass, and anything else inside other procedure calls or functions.

## Syntax Validator

```python
syntax_validator = SyntaxValidator(driver, check_multilabeled_nodes=False)
query = "MATCH (g:Goal) RETURN COUNT(g) AS output"
is_valid, syntax_metadata = syntax_validator.validate(
    query, database_name=database_name
)
print(f"Syntax Valid: {is_valid}")
print(f"Syntax Metadata: {syntax_metadata}")
```

### Read-only queries

`SyntaxValidator` accepts only queries that read: one that can modify the graph (`CREATE`, `MERGE`, `SET`, `DELETE`, `REMOVE`, an index command, a procedure that writes) is invalid and reported as `cypher_guardrail.Error.WriteQuery`. That is the default, `read_only=True`, because the queries it usually checks are generated ones that are about to be run against your data. If writes are fine, create the validator with `read_only=False`: a write is then valid, with the warning `cypher_guardrail.Warning.WriteQuery` in the metadata.

```python
SyntaxValidator(driver).validate("MATCH (n:Goal) SET n.done = true", "kg")
# (False, [{'code': 'cypher_guardrail.Error.WriteQuery', 'description': '...'}])
SyntaxValidator(driver, read_only=False).validate(
    "MATCH (n:Goal) SET n.done = true", "kg"
)
# (True, [{'code': 'cypher_guardrail.Warning.WriteQuery', 'description': '...'}])
```

FalkorDB decides what can write, so a word inside a string (`WHERE n.title = 'DELETE me'`) is not mistaken for a write, and the query is never executed. The `AsyncSyntaxValidator` takes the same `read_only`. The other validators do not look at writes.

To ask only that, without a validator, use `is_write` (and `is_write_async` for an async client):

```python
from cypher_guardrail.utils import is_write

is_write(driver, "MATCH (n:Goal) SET n.done = true", "kg")  # True
is_write(driver, "MATCH (n:Goal) RETURN n", "kg")  # False
```

It asks the database the same question (`EXPLAIN` in a read-only request, so the query is never run) and raises `CypherError` if the database rejects the query, because a query that cannot be read is not one known to be safe. No data is written, but FalkorDB registers the names that a `MERGE` mentions when it explains it, so a property key the graph did not have is in its list of keys afterwards (`SyntaxValidator` with `read_only` has the same effect).

### Parameterized queries

FalkorDB only notices a missing `$parameter` when a query runs, so `SyntaxValidator` looks for `$name` in the text of the query and reports each one as `cypher_guardrail.Warning.ParameterNotProvided`, which makes the query invalid. A `CYPHER` header in the query (`CYPHER code='1' MATCH ... WHERE g.code = $code`) provides its parameters. A client sends the others apart from the query, and cypher-guardrail cannot see them, so tell it which they are:

```python
params = {"code": "1", "title": "End poverty"}
query = "MATCH (g:Goal) WHERE g.code = $code AND g.title = $title RETURN g"
syntax_validator.validate(query, database_name, parameters=params)  # (True, [])
syntax_validator.validate(
    query, database_name, parameters={"code": "1"}
)  # title is reported
```

`parameters` is the dict you give the client (`graph.query(query, params=params)`) or just a collection of the names. Only the names are read, never the values. It works the same on `AsyncSyntaxValidator`, and `backend.explain()` takes it too.

**Parameters that FalkorDB needs the value of.** `EXPLAIN` plans the query without the values, and FalkorDB needs one in two places: a filter on a node of a label that has an index (`WHERE m.title = $title` when `Movie` has any index, even a full-text or a vector one), and `LIMIT $n` / `SKIP $n`. It then answers "Missing parameters". The validator does not pass this on: it plans the query again with a placeholder for each parameter (`0`, or `{}` for one whose property is read, as in `$p.title`), added to the `CYPHER` header if the query has one. The placeholders only let FalkorDB make the plan: a parameter that nobody provided is still reported as `cypher_guardrail.Warning.ParameterNotProvided`, and a real error in the query is still reported.

## Schema Validator

```python
schema_validator = SchemaValidator(driver)
query = "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g, t"
# Extraction
extracted_node_labels, extracted_rel_labels, extracted_paths = schema_validator.extract(
    query, database_name=database_name
)
print(f"Extracted Node patterns : {extracted_node_labels}")
print(f"Extracted Relationships patterns : {extracted_rel_labels}")
print(f"Extracted Path patterns : {extracted_paths}")
# Validation
schema_score, schema_metadata = schema_validator.validate(
    query, database_name=database_name
)
print(f"Schema Validation Score: {schema_score}")
print(f"Schema Validation Metadata: {schema_metadata}")
```

### Names with a space or a symbol

A label or a relationship type between back-quotes (``(n:`My Label`)``, ``-[:`Odd Type`]->``) is checked to exist, in every mode: an unknown one is reported like any other. The paths that use such a name are not checked (the validator reads plain names there), so a wrong direction on them is not reported. A property key between back-quotes (``n.`odd key` ``, ``{`odd key`: 1}``) is read and checked like any other, by the properties validator, online and offline, with `check_types` too; it is reported by its name, without the back-quotes. The property keys of a label that is itself back-quoted are not read.

### Queries with `UNION`

The branches of a `UNION` (or `UNION ALL`) are separate queries, so each is validated on its own and the results are merged (an issue is reported once): a variable `n` that is a `Person` in one branch and a `Movie` in another is not a conflict (`SyntaxValidator`), is checked against both labels (`SchemaValidator`), and has its properties checked against the label of its own branch (`PropertiesValidator`, with `check_types` too). `MATCH (n:Person) RETURN n.name UNION MATCH (n:Movie) RETURN n.title` is valid. A `UNION` inside the braces of a subquery (`CALL { ... UNION ... }`) is not a cut, but its branches have their own variables (see below).

A name that a `WITH` does not carry on, or that a `CALL { }` subquery does not import, is a new variable when it is declared again, as in Cypher: `MATCH (n:Person) WITH n.name AS name MATCH (n:Movie) RETURN n.title, name` is valid, and `n` has the label `Movie` in the second part. So are the names in the branches of a `UNION` inside a subquery. A name that is carried on (`WITH n`, `WITH *`, `CALL { WITH n ... }`) is still one variable, and in the one scope a variable that is used twice keeps all the labels written for it (`SchemaValidator`) or the last one (`PropertiesValidator`). Results use the names the query writes.

## Properties Validator

```python
props_validator = PropertiesValidator(driver)
query = "MATCH (g:Goal)-[:HAS_TARGET]-(t:Target) WHERE g.code =1 RETURN t"
# Extraction
variables_properties, labels_properties = props_validator.extract(
    query, strict=False, database_name=database_name
)
print(f"Accessed properties by variables: {variables_properties}")
print(f"Accessed properties by labels (including inferred): {labels_properties}")

# Validation
props_score, properties_metadata = props_validator.validate(
    query, database_name=database_name, strict=False
)
print(f"Properties Validation Score: {props_score}")
print(f"Properties Validation Metadata: {properties_metadata}")
```

`labels_properties` keeps node labels and relationship types apart, as the query writes them:
`{"node": {"Goal": ["code"], "Target": [...]}, "relationship": {"HAS_TARGET": [...]}}`. A node label and a relationship type
can have the same name (a `Rated` label and a `Rated` type), and each is checked against its own properties.

### Type mismatches (opt-in)

FalkorDB does not complain when a property is compared with a value of another type: `WHERE g.code = 1` finds nothing when `code` holds strings, and `{year: '2020'}` finds nothing when `year` is an integer. A query written by a model fails this way without any error. With `check_types=True` the validator warns about it:

```python
props_validator = PropertiesValidator(driver, check_types=True)
score, metadata = props_validator.validate(
    "MATCH (g:Goal) WHERE g.code = 1 RETURN g", database_name=database_name
)
# metadata: [{'code': 'cypher_guardrail.Warning.PropertyTypeMismatch', 'description': 'The property code of Goal holds String values, but the query compares it with the Integer 1: ...',
#             'label': 'Goal', 'property': 'code', 'expected_types': ['String'], 'value': '1', 'value_type': 'Integer'}]
```

* **A warning, and the score does not change.** It is off by default, so nothing changes unless you ask. Each `(label, property, literal)` is reported once.
* **What counts as a mismatch.** A literal (`'text'`, `1`, `1.5`, `true`) compared with `=`, `<>`, `<`, `>`, `<=`, `>=`, `IN [...]`, `STARTS WITH`, `ENDS WITH`, `CONTAINS` or `=~`, or written in the `{map}` of a pattern, whose type none of the property's values has. The types are the ones FalkorDB's `typeof` gives (`String`, `Integer`, `Float`, `Boolean`, ...). An integer and a float are the same kind of value (`2020 = 2020.0` in FalkorDB), and a property whose data is mixed (some integers, some strings) accepts a literal of either.
* **It only says what it is sure of.** Only variables that have a label in the query are checked (`(g:Goal)`, and the maps of a pattern): a variable whose label has to be inferred is left out, so a guess is never reported. Parameters (`$code`), other properties, expressions (`g.year + 1 = 2021`, `g.code = toString(1)`), `null`, and anything in a clause that writes (`SET g.code = 1`, `CREATE`, `MERGE`) are not compared.
* **The cost.** Online, one small query for each property that is compared with a literal. Offline and in the hybrid mode there is none for a literal that fits: capture the snapshot with `Snapshot.capture(client, "kg", types=True)` (see [Three ways to validate](snapshots.md)). A snapshot captured without the types says nothing offline, and in the hybrid mode the database is asked.
