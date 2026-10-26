"""The corpus: what was recorded as the baseline, held against every validator in every mode.

``golden_results.json`` has what each validator returned for a set of queries on the seeded graphs, and
``request_trace.json`` what each asked the database. Three safety nets read them: the online validators must answer
what was recorded (the first part), ask what was recorded as asked (the second), and the offline and hybrid modes must answer it
too, from a snapshot of the same graph (the third).
"""

import json
from collections import Counter
from contextlib import suppress
from functools import partial
from pathlib import Path

import pytest

from cypher_guardrail import (
    AsyncPropertiesValidator,
    AsyncSchemaValidator,
    AsyncSyntaxValidator,
    GraphNotFoundError,
    PropertiesValidator,
    SchemaValidator,
    Snapshot,
    SyntaxValidator,
)
from cypher_guardrail._internal.core import io as _io

# ----------------------------------------------------------------------------------------------------
# The validators answer what was recorded in ``golden_results.json``.
#
# ``golden_results.json`` holds what every validator returned (or raised) when it was recorded
# for a set of queries on the seeded graphs of ``conftest.py``, and on a graph that does not exist. The queries
# mix right and wrong nodes, relationships, paths and properties, so that a result handed to the wrong place, or
# used in the wrong order, changes an answer. The sync and the async validators are both held to it.
#
# The issues in the metadata have gained fields since the baseline was recorded (``label``, ``properties``, ...: see
# the issue-field tests in ``test_public_api.py``). Here each issue is compared as the baseline reported it, by its ``code`` and ``description``,
# so this file keeps guarding everything else: the scores, the messages, which issues are reported and their order.
#
# If a change to the validators is meant to change an answer, or ``conftest.py`` changes the graphs, record the
# file again with the validators in the state it should describe.


GOLDEN = json.loads(
    (Path(__file__).parent / "data" / "golden_results.json").read_text()
)


# result name -> (sync call, async call)
CALLS = {
    "syntax.validate": (
        lambda client, q, graph: SyntaxValidator(client).validate(q, graph),
        lambda client, q, graph: AsyncSyntaxValidator(client).validate(q, graph),
    ),
    "schema.validate": (
        lambda client, q, graph: SchemaValidator(client).validate(q, graph),
        lambda client, q, graph: AsyncSchemaValidator(client).validate(q, graph),
    ),
    "schema.extract": (
        lambda client, q, graph: SchemaValidator(client).extract(q, graph),
        lambda client, q, graph: AsyncSchemaValidator(client).extract(q, graph),
    ),
    "properties.validate": (
        lambda client, q, graph: PropertiesValidator(client).validate(q, graph),
        lambda client, q, graph: AsyncPropertiesValidator(client).validate(q, graph),
    ),
    "properties.extract": (
        lambda client, q, graph: PropertiesValidator(client).extract(q, graph),
        lambda client, q, graph: AsyncPropertiesValidator(client).extract(q, graph),
    ),
    "properties.validate.strict": (
        lambda client, q, graph: PropertiesValidator(client).validate(
            q, graph, strict=True
        ),
        lambda client, q, graph: AsyncPropertiesValidator(client).validate(
            q, graph, strict=True
        ),
    ),
    "properties.extract.strict": (
        lambda client, q, graph: PropertiesValidator(client).extract(
            q, graph, strict=True
        ),
        lambda client, q, graph: AsyncPropertiesValidator(client).extract(
            q, graph, strict=True
        ),
    ),
}


def legacy(value):
    """``value`` as the baseline reported it: every issue (a dict with a ``code`` and a ``description``) without the
    fields added since."""
    if isinstance(value, dict):
        if all(isinstance(value.get(key), str) for key in ("code", "description")):
            return {"code": value["code"], "description": value["description"]}
        return {key: legacy(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(legacy(item) for item in value)
    return value


def recorded(call, *args):
    """What a call returns, or raises, the way ``golden_results.json`` records it."""
    try:
        return repr(legacy(call(*args)))
    except Exception as e:  # noqa: BLE001 - the error is part of the answer
        return f"raised {type(e).__name__}: {e}"


async def async_recorded(call, *args):
    try:
        return repr(legacy(await call(*args)))
    except Exception as e:  # noqa: BLE001 - the error is part of the answer
        return f"raised {type(e).__name__}: {e}"


def test_the_recording_is_not_empty():
    assert set(GOLDEN) == {"kg", "ambiguous", "no_such_graph"}
    assert all(len(queries) >= 34 for queries in GOLDEN.values())
    answers = [
        a for queries in GOLDEN.values() for q in queries.values() for a in q.values()
    ]
    assert any(
        "SyntaxError" in a for a in answers
    )  # problems are reported in the metadata
    assert any("Warning" in a for a in answers) and any(
        a.startswith("(1.0, [])") for a in answers
    )


@pytest.mark.parametrize("graph_name", GOLDEN)
def test_the_sync_validators_answer_what_was_recorded(falkordb_client, graph_name):
    different = {}
    for query, expected in GOLDEN[graph_name].items():
        for name, (call, _) in CALLS.items():
            answer = recorded(call, falkordb_client, query, graph_name)
            if answer != expected[name]:
                different[(query, name)] = (expected[name], answer)
    assert different == {}


@pytest.mark.parametrize("graph_name", GOLDEN)
def test_the_async_validators_answer_what_was_recorded(run_async, graph_name):
    async def ask(client):
        different = {}
        for query, expected in GOLDEN[graph_name].items():
            for name, (_, call) in CALLS.items():
                answer = await async_recorded(call, client, query, graph_name)
                if answer != expected[name]:
                    different[(query, name)] = (expected[name], answer)
        return different

    assert run_async(ask) == {}


# ----------------------------------------------------------------------------------------------------
# The validators ask the database what they asked before the lookups were introduced.
#
# ``request_trace.json`` holds, for every validator call of ``golden_results.json``, the requests that reached the
# database (``GRAPH.RO_QUERY`` and ``GRAPH.EXPLAIN``, with their text) in the order the sync validators sent them.
# It was recorded from the validators as they were before ``Lookup`` existed, so the refactor
# that moved the questions into resolvers is held to asking exactly the same things: the same number of round trips,
# the same text, the same order (the async validators send them at the same time, so for them the order is not
# compared, only what was asked and how many times).
#
# If a change is meant to ask something else, record the file again with the validators in the state it should
# describe: ``uv run python tests/test_corpus.py`` (needs FalkorDBLite, so Python 3.12 or newer).


TRACE_FILE = Path(__file__).parent / "data" / "request_trace.json"


def describe(request):
    return [
        type(request).__name__,
        request.graph,
        request.query,
        getattr(request, "read_only", False),
    ]


def trace_sync(client):
    """{graph: {query: {call name: [the requests, in order]}}} for every call of the golden corpus."""
    asked = []
    run = _io.SyncTransport.run
    _io.SyncTransport.run = lambda self, request: (
        asked.append(describe(request)) or run(self, request)
    )
    try:
        trace = {}
        for graph, queries in GOLDEN.items():
            for query in queries:
                for name, (call, _) in CALLS.items():
                    asked.clear()
                    # What was asked before an error is part of the trace
                    with suppress(Exception):
                        call(client, query, graph)
                    trace.setdefault(graph, {}).setdefault(query, {})[name] = list(
                        asked
                    )
        return trace
    finally:
        _io.SyncTransport.run = run


def test_the_trace_is_not_empty():
    trace = json.loads(TRACE_FILE.read_text())
    assert set(trace) == set(GOLDEN)
    lengths = [
        len(requests)
        for queries in trace.values()
        for calls in queries.values()
        for requests in calls.values()
    ]
    assert len(lengths) == sum(len(q) for q in GOLDEN.values()) * len(CALLS)
    assert max(lengths) >= 8 and sum(lengths) > 1000


def test_the_sync_validators_ask_what_they_always_asked(falkordb_client):
    expected = json.loads(TRACE_FILE.read_text())
    actual = json.loads(json.dumps(trace_sync(falkordb_client)))

    different = {
        (graph, query, name): (
            expected[graph][query][name],
            actual[graph][query][name],
        )
        for graph in expected
        for query in expected[graph]
        for name in expected[graph][query]
        if actual[graph][query][name] != expected[graph][query][name]
    }
    assert different == {}


@pytest.mark.parametrize("graph_name", GOLDEN)
def test_the_async_validators_ask_the_same_things(run_async, graph_name):
    expected = json.loads(TRACE_FILE.read_text())[graph_name]

    async def ask(client):
        asked = []
        run = _io.AsyncTransport.run

        async def spy(self, request):
            asked.append(describe(request))
            return await run(self, request)

        _io.AsyncTransport.run = spy
        different = {}
        try:
            for query in expected:
                for name, (_, call) in CALLS.items():
                    asked.clear()
                    with suppress(Exception):
                        await call(client, query, graph_name)
                    # One validation shares the answer of identical requests, so the same request is never
                    # asked twice; how many times each one is asked must match, the order need not
                    if Counter(map(json.dumps, asked)) != Counter(
                        map(json.dumps, expected[query][name])
                    ):
                        different[(query, name)] = (expected[query][name], list(asked))
        finally:
            _io.AsyncTransport.run = run
        return different

    assert run_async(ask) == {}


if __name__ == "__main__":
    import sys
    import tempfile

    sys.path.insert(0, str(Path(__file__).parent))
    from conftest import AMBIGUOUS_GRAPH_NAME, AMBIGUOUS_SEED, GRAPH_NAME, SEED
    from servers import running_falkordb

    with running_falkordb(Path(tempfile.mkdtemp())) as server:
        server.client.select_graph(GRAPH_NAME).query(SEED)
        server.client.select_graph(AMBIGUOUS_GRAPH_NAME).query(AMBIGUOUS_SEED)
        TRACE_FILE.write_text(
            json.dumps(trace_sync(server.client), indent=1, sort_keys=True) + "\n"
        )
    print("recorded", TRACE_FILE)


# ----------------------------------------------------------------------------------------------------
# Offline and hybrid give the answers online gives: every case of ``golden_results.json`` in every mode.
#
# ``golden_results.json`` records what the online validators answered for each query on the seeded graphs. Here the
# same queries go through the validators in the other two modes, with a ``Snapshot`` captured from the same graph, and
# the answers must be the recorded ones: scores, messages, the issues and their order. The sync and the async
# validators are both held to it.
#
# * offline: ``X(snapshot)``; no database (``SyntaxValidator`` has no offline mode: only FalkorDB checks syntax);
# * hybrid: ``X(client, snapshot=snapshot)``, with ``verify="misses"`` (the default) and with ``verify="never"``.


SYNC = {"schema": SchemaValidator, "properties": PropertiesValidator}


ASYNC = {"schema": AsyncSchemaValidator, "properties": AsyncPropertiesValidator}


# result name -> (validator, method, keyword arguments)
CASES = {
    "schema.validate": ("schema", "validate", {}),
    "schema.extract": ("schema", "extract", {}),
    "properties.validate": ("properties", "validate", {}),
    "properties.extract": ("properties", "extract", {}),
    "properties.validate.strict": ("properties", "validate", {"strict": True}),
    "properties.extract.strict": ("properties", "extract", {"strict": True}),
}


def build(kinds, mode, client, snapshot, name):
    """The validator that ``name`` ("schema", "properties", "syntax") is in ``mode``."""
    if mode == "offline":
        return kinds[name](snapshot)
    verify = "never" if mode == "hybrid-never" else None
    return kinds[name](client, snapshot=snapshot, verify=verify)


MODES = ["offline", "hybrid", "hybrid-never"]


@pytest.fixture(scope="module")
def snapshots(falkordb_client):
    return {name: Snapshot.capture(falkordb_client, name) for name in GOLDEN}


# A graph that does not exist: the database says nothing about it, and an empty snapshot says "nothing is there",
# so only the hybrid mode (which checks what the snapshot misses) gives the recorded answers; see the last test.
RECORDED = [
    (graph, mode)
    for graph in GOLDEN
    for mode in MODES
    if graph != "no_such_graph" or mode == "hybrid"
]


@pytest.mark.parametrize(("graph_name", "mode"), RECORDED)
def test_the_sync_validators(falkordb_client, snapshots, graph_name, mode):
    different = {}
    for query, expected in GOLDEN[graph_name].items():
        for result, (kind, method, kwargs) in CASES.items():
            validator = build(SYNC, mode, falkordb_client, snapshots[graph_name], kind)
            call = getattr(validator, method)
            answer = recorded(partial(call, **kwargs), query, graph_name)
            if answer != expected[result]:
                different[(query, result)] = (expected[result], answer)
    assert different == {}


@pytest.mark.parametrize(("graph_name", "mode"), RECORDED)
def test_the_async_validators(run_async, snapshots, graph_name, mode):
    async def ask(client):
        different = {}
        for query, expected in GOLDEN[graph_name].items():
            for result, (kind, method, kwargs) in CASES.items():
                validator = build(ASYNC, mode, client, snapshots[graph_name], kind)
                call = getattr(validator, method)
                answer = await async_recorded(
                    partial(call, **kwargs), query, graph_name
                )
                if answer != expected[result]:
                    different[(query, result)] = (expected[result], answer)
        return different

    assert run_async(ask) == {}


@pytest.mark.parametrize("graph_name", GOLDEN)
def test_the_syntax_validator_in_the_hybrid_mode(
    falkordb_client, snapshots, graph_name
):
    different = {}
    for query, expected in GOLDEN[graph_name].items():
        validator = SyntaxValidator(falkordb_client, snapshot=snapshots[graph_name])
        answer = recorded(validator.validate, query, graph_name)
        if answer != expected["syntax.validate"]:
            different[query] = (expected["syntax.validate"], answer)
    assert different == {}


@pytest.mark.parametrize("graph_name", GOLDEN)
def test_the_async_syntax_validator_in_the_hybrid_mode(
    run_async, snapshots, graph_name
):
    async def ask(client):
        different = {}
        for query, expected in GOLDEN[graph_name].items():
            validator = AsyncSyntaxValidator(client, snapshot=snapshots[graph_name])
            answer = await async_recorded(validator.validate, query, graph_name)
            if answer != expected["syntax.validate"]:
                different[query] = (expected["syntax.validate"], answer)
        return different

    assert run_async(ask) == {}


def test_a_graph_that_does_not_exist_is_an_empty_snapshot_offline(
    falkordb_client, snapshots
):
    # Online the database says that the graph is not there (GraphNotFoundError). A snapshot of it is empty ("no label
    # exists"), so the offline mode, and the hybrid with verify="never" (the database is not asked), report the names
    # as unknown; the hybrid mode that checks the misses asks the database and gets the online answer.
    query = "MATCH (n:Goal) RETURN n"
    snapshot = snapshots["no_such_graph"]
    assert snapshot.labels == ()

    offline = SchemaValidator(snapshot).validate(query, "no_such_graph")
    never = SchemaValidator(falkordb_client, snapshot=snapshot, verify="never")
    checked = SchemaValidator(falkordb_client, snapshot=snapshot)

    with pytest.raises(GraphNotFoundError):
        SchemaValidator(falkordb_client).validate(query, "no_such_graph")
    assert [i["code"] for i in offline[1]] == ["cypher_guardrail.Warning.UnknownLabel"]
    assert never.validate(query, "no_such_graph") == offline
    with pytest.raises(GraphNotFoundError):
        checked.validate(query, "no_such_graph")


def test_the_label_chosen_among_equals_does_not_depend_on_the_order_of_the_rows(
    falkordb_client, monkeypatch
):
    # Goal, Target, Indicator, Area and Nuts1 all have a `code`: any of them fits `n`, and the properties validator
    # takes the first combination in which every property exists. Sorted, that is the same in every order.
    from cypher_guardrail._internal.sources import online

    def reversed_rows(graph, path):
        frame, node_vars, rel_vars = yield from original(graph, path)
        if frame is not None:
            frame = frame.iloc[::-1].reset_index(drop=True)
        return frame, node_vars, rel_vars

    original = online.label_combinations
    query = "MATCH (n) WHERE n.code = '1' RETURN n"
    in_scan_order = PropertiesValidator(falkordb_client).extract(query, "kg")
    monkeypatch.setitem(online._RESOLVERS, "label_combinations", reversed_rows)
    reversed_order = PropertiesValidator(falkordb_client).extract(query, "kg")

    assert reversed_order == in_scan_order
    assert in_scan_order[1] == {"node": {"Area": ["code"]}, "relationship": {}}


# ----------------------------------------------------------------------------------------------------
# A score below 1 always comes with an issue that says why: a score that nothing explains is a result the caller
# cannot act on.


@pytest.mark.parametrize(
    "graph_name", [name for name in GOLDEN if name != "no_such_graph"]
)
def test_a_score_below_1_always_has_an_issue(falkordb_client, snapshots, graph_name):
    sampled = Snapshot.capture(falkordb_client, graph_name, sample=1)
    sources = {
        "online": falkordb_client,
        "snapshot": snapshots[graph_name],
        "sample": sampled,
    }
    unexplained = []
    for query in GOLDEN[graph_name]:
        for source, driver in sources.items():
            for name, kind in SYNC.items():
                kwargs = {"database_name": graph_name} if source == "online" else {}
                score, issues = kind(driver).validate(query, **kwargs)
                if score is not None and score < 1 and not issues:
                    unexplained.append((source, name, query, score))
    assert unexplained == []


@pytest.mark.parametrize(
    "graph_name", [name for name in GOLDEN if name != "no_such_graph"]
)
def test_a_sampled_snapshot_never_reports_more_than_the_database(
    falkordb_client, graph_name
):
    # What a sample lacks is not a problem of the query: offline, a sample of one node per label scores at least as
    # well as the database does, and has no kind of issue the database does not have. (Which label is blamed can
    # differ: the one chosen among alternatives, or for a variable without a label, depends on the properties in
    # the sample.)
    sampled = Snapshot.capture(falkordb_client, graph_name, sample=1)
    worse = []
    for query in GOLDEN[graph_name]:
        for name, kind in SYNC.items():
            if name == "syntax":
                continue
            online_score, online_issues = kind(falkordb_client).validate(
                query, graph_name
            )
            score, issues = kind(sampled).validate(query)
            if (score or 0) < (online_score or 0) or (
                {i["code"] for i in issues} - {i["code"] for i in online_issues}
            ):
                worse.append((name, query, score, online_score))
    assert worse == []
