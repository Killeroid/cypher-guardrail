"""FalkorDB is the oracle: the validator must say what the database says about a query.

``SyntaxValidator`` asks FalkorDB whether it accepts a query (``GRAPH.EXPLAIN``), so it can only disagree with the
database by taking a word of an answer for what it is not, or by a check of its own. This test makes that sure for
every query of the labelled corpus and for queries made from them by breaking them a little (``mutations.py``, with a
fixed seed), on whatever FalkorDB the tests run on. A new version of FalkorDB that words an error in another way, or
stops catching one at the planning, shows up here as a query that the database refuses and the validator accepts.

* A query that ``GRAPH.EXPLAIN`` refuses is invalid.
* A query that ``GRAPH.EXPLAIN`` accepts is invalid only for one of the checks that cypher-guardrail makes itself.

What the database refuses only when the query runs on the data (``sum('x')``, a divisor that is a property minus
itself) is not asked of the validator: see ``check_runtime``.
"""

import random
import re
import sys
from pathlib import Path

import pytest
import redis
from mutations import mutate

sys.path.insert(0, str(Path(__file__).parent / "accuracy"))

import harness

from cypher_guardrail import SyntaxValidator

MUTATIONS = 800
# The codes of the checks that cypher-guardrail makes on the text of the query, whatever the database says
ITS_OWN = {
    "cypher_guardrail.Error.ConflictingVariables",
    "cypher_guardrail.Error.ArithmeticError",
}


@pytest.fixture(scope="module")
def corpus_graphs(falkordb_client):
    graphs = harness.load_graphs()
    harness.seed(falkordb_client, graphs)
    yield graphs
    harness.unseed(falkordb_client, graphs)


def _queries(falkordb_major, falkordb_version):
    cases = [
        case
        for case in harness.load_cases(falkordb_major)
        if "syntax" in case.validators
        and "$" not in case.query
        and not case.query.lstrip().upper().startswith("CYPHER")
    ]
    queries = [(case.graph, case.query) for case in cases]
    if (
        not 42200 <= falkordb_version < 42300
    ):  # FalkorDB 4.22 stops on some of the plans of a broken query
        rng = random.Random(7)
        queries += [
            (case.graph, mutate(case.query, rng))
            for case in rng.choices(cases, k=MUTATIONS)
        ]
    return queries


def _refused_by_the_database(client, graph, query):
    try:
        client.connection.execute_command("GRAPH.EXPLAIN", graph, query)
    except redis.ResponseError as error:
        return str(error)
    return None


def test_the_validator_says_what_the_database_says(
    falkordb_client, corpus_graphs, falkordb_major, falkordb_version
):
    validator = SyntaxValidator(falkordb_client, read_only=False)
    accepted_but_invalid, refused_but_valid = [], []

    for graph, query in _queries(falkordb_major, falkordb_version):
        valid, issues = validator.validate(query, graph)
        refusal = _refused_by_the_database(falkordb_client, graph, query)
        if refusal is not None and valid:
            refused_but_valid.append((query, re.sub(r"\s+", " ", refusal)[:100]))
        if refusal is None and not valid:
            own = {issue["code"] for issue in issues}
            if not own <= ITS_OWN:
                accepted_but_invalid.append((query, sorted(own - ITS_OWN)))

    assert refused_but_valid == []
    assert accepted_but_invalid == []


def _refused_as_a_write(client, graph, query):
    try:
        client.connection.execute_command(
            "GRAPH.RO_QUERY", graph, query, "TIMEOUT", 2000
        )
    except redis.ResponseError as error:
        return "only on read-only queries" in str(error)
    return False


def test_the_validator_finds_the_writes_the_database_refuses(
    falkordb_client, corpus_graphs, falkordb_major, falkordb_version
):
    validator = SyntaxValidator(falkordb_client)  # read_only, the default
    missed, made_up = [], []

    for graph, query in _queries(falkordb_major, falkordb_version):
        issues = validator.validate(query, graph)[1]
        found = "cypher_guardrail.Error.WriteQuery" in {i["code"] for i in issues}
        refused = _refused_as_a_write(falkordb_client, graph, query)
        if refused and not found:
            missed.append(query)
        if found and not refused:
            made_up.append(query)

    assert missed == []
    assert made_up == []
