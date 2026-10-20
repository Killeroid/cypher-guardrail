"""The three ways to validate: online (a database), offline (a snapshot) and hybrid (both), and snapshot sources.

The first parts check the rules of the hybrid resolver without a database and validate real queries in every mode; the
last one is ``SnapshotSource``: a validation asks it for its snapshot once and keeps it.
"""

import asyncio
import dataclasses
import random
import threading

import pandas as pd
import pytest
from support import LAB, codes

from cypher_guardrail import (
    AsyncPropertiesValidator,
    AsyncSchemaValidator,
    AsyncSyntaxValidator,
    PropertiesValidator,
    SchemaValidator,
    Snapshot,
    SyntaxValidator,
)
from cypher_guardrail._internal.core import io as _io
from cypher_guardrail._internal.core.errors import CypherError
from cypher_guardrail._internal.core.io import Lookup
from cypher_guardrail._internal.sources import hybrid as _modes
from cypher_guardrail.backend import get_async_backend, get_backend
from cypher_guardrail.snapshot import SnapshotSource

# ----------------------------------------------------------------------------------------------------
# The three ways to validate: online (a database), offline (a snapshot) and hybrid (both).
#
# ``SchemaValidator(client)`` is online, ``SchemaValidator(snapshot)`` offline and
# ``SchemaValidator(client, snapshot=snapshot)`` hybrid. The first part checks the rules of the hybrid resolver without
# a database; the second validates real queries in every mode.


# --------------------------------------------------------------------- the rules of the hybrid


class Transport:
    def run(self, request):
        raise AssertionError(f"the database is not asked: {request}")


def asked_online(monkeypatch, value="ONLINE"):
    calls = []

    def resolve(lookup):
        calls.append(lookup)
        return value
        yield

    monkeypatch.setattr(_modes._online, "resolve", resolve)
    return calls


def hybrid(lookup, verify="misses", snapshot=LAB):
    stats = _modes.LookupStats()
    resolver = _modes.chain_resolver(snapshot, verify, stats)

    def logic():
        return (yield lookup)

    return _io.run_sync(logic(), Transport(), resolver), stats


PRESENT = Lookup("path_exists", "lab", ("(:A)-[:R]->(:B)",))


MISSING = Lookup("path_exists", "lab", ("(:B)-[:R]->(:A)",))


def test_what_the_snapshot_has_is_answered_without_the_database(monkeypatch):
    calls = asked_online(monkeypatch)

    value, stats = hybrid(PRESENT)

    assert value is True
    assert calls == []
    assert stats.as_dict() == {
        "snapshot_answers": 1,
        "database_answers": 0,
        "verified_misses": 0,
        "corrected_misses": 0,
    }


def test_what_the_snapshot_lacks_is_checked_with_the_database(monkeypatch):
    calls = asked_online(monkeypatch, value=True)

    value, stats = hybrid(MISSING)

    assert value is True  # the database has the last word: the snapshot was stale
    assert calls == [MISSING]
    assert stats.as_dict() == {
        "snapshot_answers": 0,
        "database_answers": 1,
        "verified_misses": 1,
        "corrected_misses": 1,
    }


def test_a_miss_the_database_confirms_is_not_a_correction(monkeypatch):
    asked_online(monkeypatch, value=False)

    value, stats = hybrid(MISSING)

    assert value is False
    assert (stats.verified_misses, stats.corrected_misses) == (1, 0)


def test_with_verify_never_the_snapshot_is_trusted(monkeypatch):
    calls = asked_online(monkeypatch)

    value, stats = hybrid(MISSING, verify="never")

    assert value is False
    assert calls == []
    assert stats.snapshot_answers == 1


def test_an_answer_the_snapshot_is_not_sure_of_goes_to_the_database(monkeypatch):
    calls = asked_online(monkeypatch)
    lookup = Lookup("path_exists", "lab", ("(:Src)-[:R|S*2]->(:Dst)",))

    value, stats = hybrid(lookup, verify="never")

    assert value == "ONLINE"
    assert calls == [lookup]
    assert stats.database_answers == 1


SAMPLED = dataclasses.replace(LAB, sample=5)


def test_what_a_sampled_snapshot_lacks_is_asked_of_the_database_even_with_verify_never(
    monkeypatch,
):
    # A sample can miss a path that exists, so a miss in it is no answer; with verify="never" the database is not
    # asked, and the snapshot gives the answer that raises no alarm
    calls = asked_online(monkeypatch, value=False)

    value, stats = hybrid(MISSING, snapshot=SAMPLED)
    assert (value, calls) == (False, [MISSING])  # the database has the last word
    assert stats.database_answers == 1

    calls.clear()
    value, stats = hybrid(MISSING, verify="never", snapshot=SAMPLED)
    assert (value, calls) == (True, [])
    assert stats.snapshot_answers == 1


def test_what_a_sampled_snapshot_has_is_answered_without_the_database(monkeypatch):
    calls = asked_online(monkeypatch)

    value, stats = hybrid(PRESENT, snapshot=SAMPLED)

    assert (value, calls, stats.snapshot_answers) == (True, [], 1)


def test_the_names_of_a_sampled_snapshot_are_not_a_sample(monkeypatch):
    # Labels, types and keys come from the schema procedures: an unknown one is unknown
    calls = asked_online(monkeypatch)
    lookup = Lookup("missing_names", "lab", ("labels", ("Nope",)))

    value, _ = hybrid(lookup, verify="never", snapshot=SAMPLED)

    assert (value, calls) == (("Nope",), [])


def test_offline_a_sampled_snapshot_does_not_report_what_it_may_just_not_have():
    path = "MATCH (b:B)-[:R]->(a:A) RETURN a"
    prop = "MATCH (a:A) RETURN a.z"  # z is a key of the graph, but not of A

    assert SchemaValidator(LAB).validate(path)[0] < 1
    assert SchemaValidator(SAMPLED).validate(path) == (1.0, [])
    assert PropertiesValidator(LAB).validate(prop)[0] == 0.0
    assert PropertiesValidator(SAMPLED).validate(prop) == (1.0, [])


def test_offline_a_sampled_snapshot_still_reports_a_property_no_label_of_the_graph_has():
    score, issues = PropertiesValidator(SAMPLED).validate("MATCH (a:A) RETURN a.zzz")

    assert score == 0.0
    assert {i["code"] for i in issues} >= {
        "cypher_guardrail.Warning.InvalidPropertyAccess"
    }


def test_offline_a_sampled_snapshot_still_reports_an_unknown_name():
    score, issues = SchemaValidator(SAMPLED).validate("MATCH (x:Nope) RETURN x")

    assert score < 1
    assert [i["code"] for i in issues] == ["cypher_guardrail.Warning.UnknownLabel"]


def test_another_graph_goes_to_the_database(monkeypatch):
    calls = asked_online(monkeypatch)
    lookup = Lookup("path_exists", "other", ("(:A)",))

    value, stats = hybrid(lookup, verify="never")

    assert value == "ONLINE"
    assert calls == [lookup]
    assert stats.snapshot_answers == 0


def test_a_snapshot_written_by_hand_answers_for_any_graph(monkeypatch):
    calls = asked_online(monkeypatch)
    snapshot = Snapshot.from_schema(nodes={"A": []}, relationships=[])

    value, _ = hybrid(Lookup("path_exists", "whatever", ("(:A)",)), snapshot=snapshot)

    assert value is True
    assert calls == []


def test_the_label_combinations_that_occur_together_are_always_asked(monkeypatch):
    calls = asked_online(monkeypatch)
    lookup = Lookup("multilabels", "lab", ())

    value, _ = hybrid(lookup, verify="never")

    assert value == "ONLINE"
    assert calls == [lookup]


def test_a_syntax_error_of_the_snapshot_is_raised_without_the_database(monkeypatch):
    calls = asked_online(monkeypatch)

    with pytest.raises(CypherError):
        hybrid(Lookup("path_exists", "lab", ("(:A|B)",)))
    assert calls == []


def test_a_name_that_is_missing_is_checked(monkeypatch):
    calls = asked_online(monkeypatch, value=())
    lookup = Lookup("missing_names", "lab", ("labels", ("A", "Nope")))

    value, stats = hybrid(lookup)

    assert value == ()  # the database knows Nope after all
    assert calls == [lookup]
    assert stats.corrected_misses == 1


def test_a_table_is_compared_by_its_rows_not_their_order():
    one = (pd.DataFrame([["A", "B"], ["C", "D"]], columns=["a", "b"]), ["a"], ["b"])
    other = (pd.DataFrame([["C", "D"], ["A", "B"]], columns=["a", "b"]), ["a"], ["b"])
    different = (pd.DataFrame([["C", "D"]], columns=["a", "b"]), ["a"], ["b"])

    assert _modes._same(one, other)
    assert not _modes._same(one, different)
    assert _modes._same((None, [], []), (None, [], []))
    assert not _modes._same((one[0], [], []), (None, [], []))
    assert not _modes._same((None, [], []), one)


def test_verify_must_be_one_of_the_two_words():
    assert _modes.check_verify(None) == "misses"
    assert _modes.check_verify("never") == "never"
    with pytest.raises(ValueError, match="verify"):
        _modes.check_verify("always")


# ------------------------------------------------------------------ real validators, every mode

QUERIES = [
    "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) WHERE g.code = '1' RETURN g.title, t.code",
    "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target)-[:HAS_INDICATOR]->(i:Indicator) RETURN g, t, i",
    "MATCH (a)-[r]->(b) RETURN a, b",
    "MATCH (g:Goal) RETURN g",
    "MATCH (g:Ghost)-[:HAS_TARGET]->(t:Target) RETURN g",
    "MATCH (g:Goal)-[:NOPE]->(t:Target) RETURN g",
    "MATCH (g:Goal)<-[:HAS_TARGET]-(t:Target) RETURN g",
    "MATCH (g:Goal)-[:HAS_TARGET*2]->(t:Target) RETURN g",
    "MATCH (g:Goal) WHERE g.bogus = 1 RETURN g",
    "MATCH (g:Goal) WHERE g.code = '1' AND g.bogus = 1 RETURN g",
    "MATCH (a:GeoArea)-[:HAS_NUTS1]->(n:Nuts1) WHERE n.name = 'x' RETURN a.name",
    "CALL db.idx.fulltext.queryNodes('Goal', 'x') YIELD node RETURN node",
]


GOOD = (
    "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) WHERE g.code = '1' RETURN g.title, t.code"
)


@pytest.fixture
def kg(falkordb_client):
    return Snapshot.capture(falkordb_client, "kg")


VALIDATORS = {
    "schema": (SchemaValidator, AsyncSchemaValidator),
    "properties": (PropertiesValidator, AsyncPropertiesValidator),
}


@pytest.mark.parametrize("name", VALIDATORS)
def test_the_mode_of_a_validator(falkordb_client, kg, name):
    sync, asynchronous = VALIDATORS[name]

    assert sync(falkordb_client).mode == "online"
    assert sync(kg).mode == "offline"
    assert sync(falkordb_client, snapshot=kg).mode == "hybrid"
    assert asynchronous(kg).mode == "offline"


def test_the_mode_of_the_syntax_validator(falkordb_client, kg):
    assert SyntaxValidator(falkordb_client).mode == "online"
    assert SyntaxValidator(falkordb_client, snapshot=kg).mode == "hybrid"


@pytest.mark.parametrize("name", VALIDATORS)
def test_offline_never_asks_the_database(falkordb_client, kg, requests, name):
    # (that it gives the online answers is the corpus's: test_corpus holds every mode to the recorded answers)
    sync, _ = VALIDATORS[name]

    offline = sync(kg)
    for query in QUERIES:
        offline.validate(query, "kg")

    assert requests == []
    assert offline.stats.snapshot_answers > 0
    assert offline.stats.database_answers == 0


@pytest.mark.parametrize("name", VALIDATORS)
def test_a_valid_query_needs_no_lookup_in_the_hybrid_mode(
    falkordb_client, kg, requests, name
):
    sync, _ = VALIDATORS[name]
    sync(falkordb_client).validate(GOOD, "kg")
    assert len(requests) > 0  # online, the lookups cost round trips
    requests.clear()

    validator = sync(falkordb_client, snapshot=kg)
    validator.validate(GOOD, "kg")

    assert requests == []
    assert validator.stats.database_answers == 0
    assert validator.stats.snapshot_answers > 0


def test_the_syntax_validator_asks_the_database_once_for_a_valid_query(
    falkordb_client, kg, requests
):
    validator = SyntaxValidator(
        falkordb_client, check_multilabeled_nodes=False, snapshot=kg
    )

    assert validator.validate(GOOD, "kg") == (True, [])

    assert len(requests) == 1  # the EXPLAIN: only FalkorDB can say the syntax is right


def test_the_syntax_is_still_checked_by_the_database(falkordb_client, kg):
    validator = SyntaxValidator(falkordb_client, snapshot=kg)

    valid, metadata = validator.validate("MATCH (n:Goal RETURN n", "kg")

    assert valid is False
    assert metadata[0]["code"] == "FalkorDB.ClientError.Statement.SyntaxError"


# ---------------------------------------------------------------------- a snapshot gone stale


@pytest.fixture
def stale(falkordb_client):
    """A snapshot of a graph, and then a label and a property that the snapshot does not know."""
    graph = falkordb_client.select_graph("stale")
    graph.query("CREATE (:Old {a: 1})-[:LINKS]->(:Old {a: 2})")
    snapshot = Snapshot.capture(falkordb_client, "stale")
    graph.query("CREATE (:Fresh {x: 1})-[:LINKS]->(:Old {a: 3, b: 4})")
    yield snapshot
    graph.delete()


FRESH = "MATCH (n:Fresh)-[:LINKS]->(o:Old) WHERE o.b = 4 RETURN n"


def test_a_stale_snapshot_is_wrong_offline_and_right_in_the_hybrid_mode(
    falkordb_client, stale
):
    online = SchemaValidator(falkordb_client).validate(FRESH, "stale")
    offline = SchemaValidator(stale).validate(FRESH, "stale")
    checked = SchemaValidator(falkordb_client, snapshot=stale)
    trusting = SchemaValidator(falkordb_client, snapshot=stale, verify="never")

    assert online == (1.0, [])
    assert "cypher_guardrail.Warning.UnknownLabel" in codes(
        offline[1]
    )  # Fresh is not in the snapshot
    assert checked.validate(FRESH, "stale") == online
    assert trusting.validate(FRESH, "stale") == offline
    assert checked.stats.corrected_misses > 0
    assert trusting.stats.corrected_misses == 0
    assert trusting.stats.verified_misses == 0


def test_a_stale_snapshot_for_the_properties(falkordb_client, stale):
    query = "MATCH (o:Old) WHERE o.b = 4 RETURN o"
    online = PropertiesValidator(falkordb_client).validate(query, "stale")

    offline = PropertiesValidator(stale).validate(query, "stale")
    checked = PropertiesValidator(falkordb_client, snapshot=stale).validate(
        query, "stale"
    )

    assert online[0] == 1.0
    assert offline != online
    assert checked == online


def test_what_was_deleted_since_the_snapshot_is_still_reported_as_there(
    falkordb_client,
):
    graph = falkordb_client.select_graph("deleted")
    try:
        graph.query("CREATE (:Gone {a: 1})-[:LINKS]->(:Stays)")
        snapshot = Snapshot.capture(falkordb_client, "deleted")
        graph.query("MATCH (g:Gone) DETACH DELETE g")
        query = "MATCH (g:Gone)-[:LINKS]->(s:Stays) RETURN s"

        online = SchemaValidator(falkordb_client).validate(query, "deleted")
        hybrid_answer = SchemaValidator(falkordb_client, snapshot=snapshot).validate(
            query, "deleted"
        )

        # A missed warning, never a false alarm: the documented price of not asking for what the snapshot has
        assert online[0] < 1.0
        assert hybrid_answer == (1.0, [])
    finally:
        graph.delete()


# -------------------------------------------------------------------- other graphs and names


def test_another_graph_is_answered_by_the_database_in_the_hybrid_mode(
    falkordb_client, kg
):
    query = "MATCH (a:A)-[:R]->(b:B) RETURN a"
    online = SchemaValidator(falkordb_client).validate(query, "ambiguous")
    validator = SchemaValidator(falkordb_client, snapshot=kg)

    assert validator.validate(query, "ambiguous") == online
    assert validator.stats.snapshot_answers == 0
    assert validator.stats.database_answers > 0


def test_offline_the_graph_must_be_the_snapshots(kg):
    validator = SchemaValidator(kg)

    assert validator.validate(GOOD)[0] == 1.0  # no name needed
    assert validator.validate(GOOD, "kg")[0] == 1.0
    with pytest.raises(ValueError, match="'kg'"):
        validator.validate(GOOD, "other")


def test_a_snapshot_written_by_hand_has_no_graph_name_and_takes_any():
    snapshot = Snapshot.from_schema(
        nodes={"Goal": ["code"], "Target": ["code"]},
        relationships=[("Goal", "HAS_TARGET", "Target")],
    )
    validator = SchemaValidator(snapshot)
    good = "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g"

    assert validator.validate(good)[0] == 1.0
    assert validator.validate(good, "x")[0] == 1.0
    score, metadata = validator.validate("MATCH (g:Ghost) RETURN g", "anything")
    assert score < 1.0
    assert codes(metadata) == ["cypher_guardrail.Warning.UnknownLabel"]


def test_the_utility_that_infers_labels_takes_a_snapshot(kg):
    from cypher_guardrail.utils import get_paths_after_inference

    (frame,) = get_paths_after_inference(kg, ["(a)-[r]->(b)"], "kg")

    assert {tuple(row) for row in frame.to_numpy().tolist()} >= {
        ("Goal", "Target", "HAS_TARGET")
    }


# ------------------------------------------------------------------------------- the rules


def test_the_syntax_validator_needs_a_database(kg):
    with pytest.raises(TypeError, match="only FalkorDB can check syntax"):
        SyntaxValidator(kg)
    with pytest.raises(TypeError, match="only FalkorDB can check syntax"):
        AsyncSyntaxValidator(kg)


@pytest.mark.parametrize("name", VALIDATORS)
def test_a_snapshot_as_the_driver_and_as_snapshot_are_two_ways_to_say_the_same_thing(
    kg, name
):
    for validator in VALIDATORS[name]:
        with pytest.raises(TypeError, match="whole validator"):
            validator(kg, snapshot=kg)
        with pytest.raises(TypeError, match="whole validator"):
            validator(kg, verify="never")


@pytest.mark.parametrize("name", VALIDATORS)
def test_verify_and_snapshot_must_be_what_they_say(falkordb_client, kg, name):
    sync, _ = VALIDATORS[name]

    with pytest.raises(ValueError, match="verify"):
        sync(falkordb_client, snapshot=kg, verify="always")
    with pytest.raises(TypeError, match="verify"):
        sync(falkordb_client, verify="never")  # no snapshot to verify
    with pytest.raises(TypeError, match="Snapshot"):
        sync(falkordb_client, snapshot={"labels": []})


def test_the_async_validators_take_the_same_arguments(run_async, kg):
    async def ask(client):
        with pytest.raises(ValueError, match="verify"):
            AsyncSchemaValidator(client, snapshot=kg, verify="always")
        with pytest.raises(TypeError, match="verify"):
            AsyncPropertiesValidator(client, verify="never")
        validator = AsyncSchemaValidator(client, max_concurrency=4, snapshot=kg)
        return validator.mode, validator._max_concurrency

    assert run_async(ask) == ("hybrid", 4)


def test_a_sync_validator_with_a_snapshot_still_turns_an_async_client_away(
    run_async, kg
):
    async def ask(client):
        with pytest.raises(TypeError, match="async"):
            SchemaValidator(client, snapshot=kg)

    run_async(ask)


def test_offline_only_asks_the_snapshot():
    # nothing the offline mode is given can reach a database: it has none
    transport = _modes.OfflineTransport()

    with pytest.raises(RuntimeError, match="needs a database"):
        transport.run(_io.RoQuery("g", "RETURN 1"))


def test_online_the_database_answers_every_lookup(falkordb_client):
    validator = SchemaValidator(falkordb_client)

    validator.validate(GOOD, "kg")

    assert validator.stats.database_answers > 0
    assert validator.stats.snapshot_answers == 0
    assert validator.stats.verified_misses == validator.stats.corrected_misses == 0


def test_a_backend_that_has_only_a_snapshot_cannot_run_a_query(kg, run_async):
    from cypher_guardrail.backend import AsyncSnapshotBackend, SnapshotBackend

    def logic():
        return (yield _io.RoQuery("kg", "RETURN 1"))

    with pytest.raises(RuntimeError, match="needs a database"):
        SnapshotBackend(kg).run(logic())

    async def ask(_client):
        return await AsyncSnapshotBackend(kg).run(logic())

    with pytest.raises(RuntimeError, match="needs a database"):
        run_async(ask)


# ----------------------------------------------------------------- what the snapshot cannot know: paths of 2+ hops


@pytest.fixture
def chain(falkordb_client):
    """Src -R-> Mid and Mid -S-> Dst, but through two different Mid nodes: the kinds connect, the nodes do not."""
    graph = falkordb_client.select_graph("two_hops")
    graph.query(
        "CREATE (:Src)-[:R]->(:Mid {n: 1}), (:Mid {n: 2})-[:S]->(:Dst)",
    )
    yield Snapshot.capture(falkordb_client, "two_hops")
    graph.delete()


TWO_HOPS = "MATCH (a:Src)-[:R|S*2]->(d:Dst) RETURN a"


def test_a_path_of_two_hops_is_answered_by_the_database_in_the_hybrid_mode(
    falkordb_client, chain, requests
):
    online = SchemaValidator(falkordb_client).validate(TWO_HOPS, "two_hops")
    offline = SchemaValidator(chain).validate(TWO_HOPS, "two_hops")
    del requests[:]
    hybrid = SchemaValidator(falkordb_client, snapshot=chain)
    answer = hybrid.validate(TWO_HOPS, "two_hops")

    # the snapshot only knows that the kinds connect ("maybe"), so offline reports nothing; the database knows better
    assert offline == (1.0, [])
    assert online[1] != []
    assert answer == online
    assert requests  # the database was asked
    assert hybrid.stats.database_answers > 0


def test_a_cancelled_validation_frees_its_slot_in_the_hybrid_mode(
    run_async, kg, monkeypatch
):
    import asyncio

    from falkordb.asyncio.graph import AsyncGraph

    running = {"now": 0, "started": 0}
    original = AsyncGraph.ro_query
    delay = {"seconds": 5}

    async def slow(self, *args, **kwargs):
        running["now"] += 1
        running["started"] += 1
        try:
            await asyncio.sleep(delay["seconds"])
            return await original(self, *args, **kwargs)
        finally:
            running["now"] -= 1

    monkeypatch.setattr(AsyncGraph, "ro_query", slow)
    # a label the snapshot lacks: the database is asked to check it, and that is the request that gets cancelled
    query = "MATCH (g:Ghost) RETURN g"

    async def ask(client):
        validator = AsyncSchemaValidator(client, max_concurrency=1, snapshot=kg)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(validator.validate(query, "kg"), 0.05)
        await asyncio.sleep(0.05)  # let the cancelled query unwind
        left_running = running["now"]
        delay["seconds"] = 0
        # the only slot is free again, so the next validation is not held up
        result = await asyncio.wait_for(validator.validate(query, "kg"), 5)
        return left_running, result

    left_running, result = run_async(ask)
    assert running["started"] > 0
    assert left_running == 0
    assert [i["code"] for i in result[1]] == ["cypher_guardrail.Warning.UnknownLabel"]


# ------------------------------------------------------------------ the types of a property in the hybrid mode

TYPED_LAB = dataclasses.replace(
    LAB,
    property_types=(
        ("A", "NODE", (("p", ("Integer",)), ("q", ("String",)))),
        ("R", "RELATIONSHIP", (("w", ("Float",)),)),
    ),
)


def types_lookup(label, kind, prop, literal_type):
    return Lookup("property_types", "lab", (label, kind, prop, literal_type))


def test_a_type_the_snapshot_has_seen_is_answered_without_the_database(monkeypatch):
    calls = asked_online(monkeypatch)

    value, stats = hybrid(types_lookup("A", "node", "p", "Integer"), snapshot=TYPED_LAB)

    assert value == ("Integer",)
    assert calls == []
    assert (stats.snapshot_answers, stats.database_answers) == (1, 0)


def test_a_type_the_snapshot_has_not_seen_is_checked_with_the_database(monkeypatch):
    # the snapshot is stale: the property has been given strings since
    calls = asked_online(monkeypatch, value=("Integer", "String"))
    lookup = types_lookup("A", "node", "p", "String")

    value, stats = hybrid(lookup, snapshot=TYPED_LAB)

    assert value == ("Integer", "String")
    assert calls == [lookup]
    assert (stats.verified_misses, stats.corrected_misses) == (1, 1)


def test_a_type_the_database_also_has_not_seen_is_a_confirmed_miss(monkeypatch):
    asked_online(monkeypatch, value=("Integer",))

    value, stats = hybrid(types_lookup("A", "node", "p", "String"), snapshot=TYPED_LAB)

    assert value == ("Integer",)
    assert (stats.verified_misses, stats.corrected_misses) == (1, 0)


def test_with_verify_never_the_types_of_the_snapshot_are_trusted(monkeypatch):
    calls = asked_online(monkeypatch)

    value, stats = hybrid(
        types_lookup("A", "node", "p", "String"), verify="never", snapshot=TYPED_LAB
    )

    assert value == ("Integer",)
    assert calls == []
    assert (stats.snapshot_answers, stats.database_answers) == (1, 0)


def test_a_snapshot_without_the_types_leaves_them_to_the_database(monkeypatch):
    calls = asked_online(monkeypatch, value=("String",))
    lookup = types_lookup("A", "node", "q", "String")

    value, stats = hybrid(lookup, verify="never", snapshot=LAB)

    assert value == ("String",)
    assert calls == [lookup]
    assert (stats.snapshot_answers, stats.database_answers) == (0, 1)


def test_the_types_of_another_graph_are_asked_of_the_database(monkeypatch):
    calls = asked_online(monkeypatch, value=("String",))
    lookup = Lookup("property_types", "other", ("A", "node", "p", "Integer"))

    value, _ = hybrid(lookup, verify="never", snapshot=TYPED_LAB)

    assert value == ("String",)
    assert calls == [lookup]


# ----------------------------------------------------------------------------------------------------
# A snapshot source (``SnapshotSource``): a validation asks it for its snapshot once and keeps it.
#
# A ``Snapshot`` is its own source. A ``LiveSnapshot`` is one that is replaced from time to time, so a validation must not
# see the replacement halfway: these tests use a source that gives a different snapshot every time it is asked, which
# shows any validation that asks twice. They need no database, except for the hybrid ones.


WITH_GOAL = Snapshot.from_schema(
    nodes={"Goal": ["code"], "Target": ["code"]},
    relationships=[("Goal", "HAS_TARGET", "Target")],
    graph="kg",
)


WITHOUT = Snapshot.from_schema(
    nodes={"Other": []}, relationships=[], graph="kg"
)  # a graph where nothing of the query exists


QUERY = "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) WHERE g.code = '1' RETURN g"


class Source(SnapshotSource):
    """Gives the snapshots in turn (the last one for ever), and counts how often it is asked."""

    def __init__(self, *snapshots, graph="kg"):
        self._snapshots = list(snapshots)
        self._graph = graph
        self.calls = 0

    @property
    def graph(self):
        return self._graph

    def current(self):
        self.calls += 1
        return self._snapshots[min(self.calls, len(self._snapshots)) - 1]


def unknown_labels(metadata):
    return sorted(i["label"] for i in metadata if "label" in i)


# ------------------------------------------------------------------------------ the protocol


def test_a_snapshot_is_its_own_source():
    assert isinstance(WITH_GOAL, SnapshotSource)
    assert WITH_GOAL.current() is WITH_GOAL
    assert WITH_GOAL.graph == "kg"


def test_what_has_a_current_method_is_not_a_source_unless_it_says_so(falkordb_client):
    class Duck:
        graph = "kg"

        def current(self):
            return WITH_GOAL

    # not a driver (it is not a FalkorDB client), and not a snapshot= either
    with pytest.raises(TypeError, match="valid FalkorDB client"):
        get_backend(Duck())
    with pytest.raises(TypeError, match="LiveSnapshot"):
        SchemaValidator(falkordb_client, snapshot=Duck())


# --------------------------------------------------------------------- one snapshot for one validation


def test_a_validation_asks_the_source_once_and_uses_that_snapshot_throughout():
    source = Source(WITH_GOAL, WITHOUT)

    score, metadata = SchemaValidator(source).validate(QUERY)

    # the source would give the other snapshot to a second question: it was not asked one
    assert source.calls == 1
    assert (score, metadata) == (1.0, [])


def test_the_next_validation_asks_again_and_sees_the_new_snapshot():
    source = Source(WITH_GOAL, WITHOUT)
    validator = SchemaValidator(source)

    first = validator.validate(QUERY)
    second = validator.validate(QUERY)

    assert source.calls == 2
    assert first == (1.0, [])
    assert unknown_labels(second[1]) == ["Goal", "Target"]


def test_each_public_method_pins_the_snapshot():
    source = Source(WITH_GOAL, WITHOUT)
    props = PropertiesValidator(source)

    props.extract(QUERY)
    assert source.calls == 1
    props.validate(QUERY)
    assert source.calls == 2
    SchemaValidator(source).extract(QUERY)
    assert source.calls == 3


def test_a_run_outside_a_validation_pins_for_its_own_length():
    source = Source(WITH_GOAL, WITHOUT)
    backend = get_backend(source)

    def logic():
        from cypher_guardrail._internal.core import queries

        labels = yield from queries.name_exists("kg", "node", "Goal")
        again = yield from queries.name_exists("kg", "node", "Goal")
        return labels, again

    assert backend.run(logic()) == (True, True)
    assert source.calls == 1
    assert backend.run(logic()) == (False, False)  # the second run asked again
    assert source.calls == 2


def test_nested_blocks_keep_the_snapshot_of_the_outer_one():
    source = Source(WITH_GOAL, WITHOUT)
    backend = get_backend(source)

    with backend.snapshot():
        with backend.snapshot():
            pass
        assert backend.run(iter_label("Goal")) is True
        assert backend.run(iter_label("Goal")) is True

    assert source.calls == 1


def iter_label(name):
    from cypher_guardrail._internal.core import queries

    return queries.name_exists("kg", "node", name)


def two_lookups():
    """A run that asks two different questions: pinned, they are answered from one snapshot."""
    from cypher_guardrail._internal.core import queries

    goal = yield from queries.name_exists("kg", "node", "Goal")
    target = yield from queries.name_exists("kg", "node", "Target")
    return goal, target


def test_a_resolver_is_built_once_for_as_long_as_the_snapshot_is_the_same(monkeypatch):
    built = []
    original = _modes.snapshot_resolver

    def counting(snapshot, stats):
        built.append(snapshot)
        return original(snapshot, stats)

    monkeypatch.setattr(_modes, "snapshot_resolver", counting)
    validator = SchemaValidator(
        Source(WITH_GOAL, WITH_GOAL, WITHOUT, WITHOUT, WITH_GOAL)
    )

    for _ in range(5):
        validator.validate(QUERY)

    # kept while the snapshot is the same, rebuilt when it changes: WITH_GOAL, WITHOUT, WITH_GOAL
    assert built == [WITH_GOAL, WITHOUT, WITH_GOAL]


def test_a_plain_snapshot_is_pinned_and_changes_nothing():
    validator = SchemaValidator(WITH_GOAL)

    assert validator.validate(QUERY) == (1.0, [])
    assert validator.validate("MATCH (g:Ghost) RETURN g")[1][0]["label"] == "Ghost"
    assert validator.mode == "offline"


def test_the_graph_of_the_source_is_the_graph_of_the_validator():
    source = Source(WITH_GOAL, graph="kg")

    assert SchemaValidator(source).validate(QUERY, "kg") == (1.0, [])
    with pytest.raises(ValueError, match="kg"):
        SchemaValidator(source).validate(QUERY, "other")
    # a source with no graph name takes any, like a snapshot written by hand
    assert SchemaValidator(Source(WITH_GOAL, graph="")).validate(QUERY, "any") == (
        1.0,
        [],
    )


def test_the_syntax_validator_still_needs_a_database_whatever_the_source():
    with pytest.raises(TypeError, match="needs a database"):
        SyntaxValidator(Source(WITH_GOAL))


# ---------------------------------------------------------------------- never a mix of two snapshots


def test_concurrent_validations_in_threads_never_mix_snapshots():
    class Random(SnapshotSource):
        """Gives a snapshot at random, and says which it gave this thread last."""

        graph = "kg"

        def __init__(self):
            self.rng = random.Random(3)
            self.given = threading.local()
            self.lock = threading.Lock()

        def current(self):
            with self.lock:
                snapshot = self.rng.choice([WITH_GOAL, WITHOUT])
            self.given.snapshot = snapshot
            return snapshot

    source = Random()
    validator = SchemaValidator(source)
    wrong = []

    def work():
        for _ in range(150):
            metadata = validator.validate(QUERY)[1]
            expected = [] if source.given.snapshot is WITH_GOAL else ["Goal", "Target"]
            if unknown_labels(metadata) != expected:  # half of each would be a mix
                wrong.append(metadata)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert wrong == []


def test_concurrent_async_validations_never_mix_snapshots():
    source = Source(WITH_GOAL, WITHOUT, WITH_GOAL, WITHOUT)
    validator = AsyncSchemaValidator(source)

    async def ask():
        return await asyncio.gather(*(validator.validate(QUERY) for _ in range(4)))

    results = asyncio.run(ask())

    assert source.calls == 4  # once each
    assert [unknown_labels(m) for _, m in results] == [
        [],
        ["Goal", "Target"],
        [],
        ["Goal", "Target"],
    ]


def test_the_async_backend_pins_for_its_own_run():
    source = Source(WITH_GOAL, WITHOUT)
    backend = get_async_backend(source)

    async def ask():
        return await backend.run(two_lookups()), await backend.run(two_lookups())

    # one snapshot for each run: both questions of a run get the same answer
    assert asyncio.run(ask()) == ((True, True), (False, False))
    assert source.calls == 2


# --------------------------------------------------------------------------------------- hybrid


def test_the_hybrid_mode_takes_a_source(falkordb_client):
    source = Source(Snapshot.capture(falkordb_client, "kg"))

    validator = SchemaValidator(falkordb_client, snapshot=source)

    assert validator.mode == "hybrid"
    assert validator.validate(QUERY, "kg") == (1.0, [])
    assert source.calls == 1


def test_the_hybrid_mode_pins_the_snapshot_and_checks_a_miss_with_the_database(
    falkordb_client,
):
    stale = Source(WITHOUT, Snapshot.capture(falkordb_client, "kg"))
    validator = SchemaValidator(falkordb_client, snapshot=stale)

    first = validator.validate(QUERY, "kg")

    assert stale.calls == 1
    assert first == (
        1.0,
        [],
    )  # the snapshot said nothing exists, and the database corrected it
    assert validator.stats.corrected_misses > 0


@pytest.mark.parametrize("bad", [object(), "kg", {"graph": "kg"}, None.__class__])
def test_snapshot_must_be_a_snapshot_or_a_source(falkordb_client, bad):
    with pytest.raises(TypeError, match="LiveSnapshot"):
        SchemaValidator(falkordb_client, snapshot=bad)


def test_a_run_on_a_database_backend_pins_for_its_own_length(falkordb_client):
    # the snapshot is of a graph with Goal and Target for the first run and without them for the second (stale: the
    # database corrects it), but that is not the point: the source is asked once for each run
    source = Source(WITH_GOAL, WITHOUT)
    backend = get_backend(falkordb_client, snapshot=source)

    backend.run(two_lookups())
    assert source.calls == 1
    backend.run(two_lookups())
    assert source.calls == 2


def test_a_run_on_an_async_database_backend_pins_for_its_own_length(run_async):
    source = Source(WITH_GOAL, WITHOUT)

    async def ask(client):
        backend = get_async_backend(client, snapshot=source)
        await backend.run(two_lookups())
        first = source.calls
        await backend.run(two_lookups())
        return first, source.calls

    assert run_async(ask) == (1, 2)


# ----------------------------------------------------------------------------------------------------
# The label combinations that occur together on a node: asked when a variable has several labels, and not kept


def test_an_ordinary_query_does_not_ask_for_the_label_combinations(
    falkordb_client, kg, requests
):
    validator = SyntaxValidator(
        falkordb_client, snapshot=kg
    )  # check_multilabeled_nodes is on

    validator.validate(GOOD, "kg")
    validator.validate(GOOD, "kg")

    assert (
        len(requests) == 2
    )  # one EXPLAIN each: no variable has two labels, so there is nothing to ask


MANY = "MATCH (a:Goal) MATCH (a:Target) RETURN a"


def test_a_variable_with_several_labels_asks_for_the_combinations_each_validation(
    falkordb_client, kg, requests
):
    validator = SyntaxValidator(falkordb_client, snapshot=kg)

    validator.validate(MANY, "kg")
    first = len(requests)
    validator.validate(MANY, "kg")

    assert first == 2  # the EXPLAIN and the label combinations
    assert (
        len(requests) == first * 2
    )  # and again, not kept: the graph may have changed since


def test_the_combinations_are_asked_once_in_a_validation_however_many_variables_conflict(
    falkordb_client, kg, requests
):
    query = (
        "MATCH (a:Goal) MATCH (a:Target) RETURN a UNION "
        "MATCH (b:Indicator) MATCH (b:Goal) RETURN b"
    )

    SyntaxValidator(falkordb_client, snapshot=kg).validate(query, "kg")

    assert len(requests) == 2  # the EXPLAIN, and one question about the combinations


def test_a_validator_that_lives_on_sees_a_combination_created_after_it_was_made(
    falkordb_client,
):
    graph = falkordb_client.select_graph("multilabels_fresh")
    graph.query("CREATE (:A {x: 1}), (:B {x: 2})")
    try:
        validator = SyntaxValidator(falkordb_client)
        query = "MATCH (n:A) MATCH (n:B) RETURN n"
        before = validator.validate(query, "multilabels_fresh")

        graph.query(
            "CREATE (:A:B {x: 3})"
        )  # a node with both labels: the two are not a conflict any more
        after = validator.validate(query, "multilabels_fresh")
    finally:
        graph.delete()

    assert (
        before[0] is False
        and before[1][0]["code"] == "cypher_guardrail.Error.ConflictingVariables"
    )
    assert after == (True, [])


def test_without_the_multilabel_check_the_combinations_are_never_asked(
    falkordb_client, kg, requests
):
    validator = SyntaxValidator(
        falkordb_client, check_multilabeled_nodes=False, snapshot=kg
    )

    valid, metadata = validator.validate(MANY, "kg")

    assert (
        valid is False
        and metadata[0]["code"] == "cypher_guardrail.Error.ConflictingVariables"
    )
    assert (
        len(requests) == 1
    )  # the EXPLAIN: a variable with two labels is a conflict whatever the graph has
