"""User-defined functions (UDFs): the catalog a snapshot keeps of them, how it is captured, saved and noticed.

UDFs belong to the FalkorDB instance, not to a graph, and FalkorDB tells their names only. The engine of the tests
(FalkorDBLite) has them, so the capture runs against it; the answer of a server that has none is simulated.
"""

import dataclasses
from datetime import timedelta

import pytest

from cypher_guardrail import CypherError, LiveSnapshot, Snapshot
from cypher_guardrail._internal.capture_driver import fingerprint
from cypher_guardrail._internal.core import udfs
from cypher_guardrail._internal.core.io import ListUdfs, run_sync
from cypher_guardrail._internal.sources.online import listed_udfs

CODE = (
    "function boost(x) { return x * 2; }\n"
    "function shout(s) { return s.toUpperCase(); }\n"
    "falkor.register('boost', boost);\n"
    "falkor.register('shout', shout);"
)
SCORING = (("scoring", ("boost", "shout")),)


@pytest.fixture
def scoring(falkordb_client):
    """The library ``scoring`` loaded on the instance for the length of a test."""
    falkordb_client.execute_command("GRAPH.UDF", "LOAD", "REPLACE", "scoring", CODE)
    yield "scoring"
    falkordb_client.execute_command("GRAPH.UDF", "DELETE", "scoring")


# ----------------------------------------------------------------------------------------------------
# Reading the reply of GRAPH.UDF LIST


@pytest.mark.parametrize(
    "reply",
    [
        [
            {"library_name": "scoring", "functions": ["shout", "boost"]}
        ],  # a map (RESP3, FalkorDBLite)
        [["library_name", "scoring", "functions", ["shout", "boost"]]],  # flat (RESP2)
        [[b"library_name", b"scoring", b"functions", [b"shout", b"boost"]]],  # bytes
        [
            {"library_name": "scoring", "functions": ["boost"], "library_code": "..."},
            {"library_name": "scoring", "functions": ["shout", "boost"]},
        ],  # a library twice, and its code is ignored
    ],
)
def test_both_protocols_give_the_same_libraries(reply):
    assert udfs.parse_list(reply) == SCORING


def test_the_libraries_and_their_functions_are_sorted():
    reply = [
        {"library_name": "zeta", "functions": ["b", "a"]},
        {"library_name": "alpha", "functions": []},
    ]

    assert udfs.parse_list(reply) == (("alpha", ()), ("zeta", ("a", "b")))


@pytest.mark.parametrize(
    "reply",
    [
        None,
        [],
        "OK",
        7,
        [1, 2],
        [None],
        [{}],
        [{"library_name": "x"}],
        [{"functions": ["f"]}],
        [{"library_name": "x", "functions": "f"}],
        [["library_name"]],
    ],
)
def test_what_is_not_a_library_is_left_out(reply):
    assert udfs.parse_list(reply) == ()


@pytest.mark.parametrize(
    ("message", "unsupported"),
    [
        ("ERR unknown command 'GRAPH.UDF', with args beginning with: 'LIST'", True),
        ("NOPERM this user has no permissions to run the 'graph.udf' command", True),
        ("Connection reset by peer", False),
        ("Invalid graph operation on empty key", False),
    ],
)
def test_a_server_that_has_no_list_of_udfs_is_told_from_one_that_failed(
    message, unsupported
):
    error = CypherError("FalkorDB.DatabaseError.Statement.ExecutionFailed", message)

    assert udfs.is_unsupported(error) is unsupported


# ----------------------------------------------------------------------------------------------------
# Asking for them: the logic, with a transport that answers as a server would


class Server:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def run(self, request):
        self.asked.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def ask(server, wanted=True):
    return run_sync(listed_udfs("g", wanted), server)


def test_the_functions_of_the_instance_are_asked_once():
    server = Server([{"library_name": "scoring", "functions": ["boost", "shout"]}])

    assert ask(server) == SCORING
    assert server.asked == [ListUdfs("g")]


def test_nothing_is_asked_when_they_are_not_wanted():
    server = Server([])

    assert ask(server, wanted=False) is None
    assert server.asked == []


@pytest.mark.parametrize(
    "message",
    [
        "ERR unknown command 'GRAPH.UDF'",
        "NOPERM this user has no permissions to run the 'graph.udf' command",
    ],
)
def test_a_server_that_cannot_list_them_has_no_catalog_and_no_error(message):
    server = Server(
        CypherError("FalkorDB.DatabaseError.Statement.ExecutionFailed", message)
    )

    assert ask(server) is None


def test_any_other_failure_is_raised():
    server = Server(
        CypherError(
            "FalkorDB.DatabaseError.Statement.ExecutionFailed",
            "Connection reset by peer",
        )
    )

    with pytest.raises(CypherError, match="Connection reset"):
        ask(server)


# ----------------------------------------------------------------------------------------------------
# The catalog in a snapshot


def snap(**fields):
    return Snapshot(labels=("A",), node_label_sets=(("A",),), **fields)


def test_a_snapshot_without_udfs_is_format_1_and_has_no_catalog():
    snapshot = snap()

    assert snapshot.udfs is None
    assert not snapshot.has_udfs
    assert snapshot.format_version == 1
    assert "udfs" not in snapshot.to_dict()
    assert snapshot.functions_of("scoring") == ()
    assert snapshot.function_names == frozenset()


def test_a_snapshot_with_udfs_is_format_2():
    snapshot = snap(udfs={"scoring": ["shout", "boost", "boost"]})

    assert snapshot.udfs == SCORING
    assert snapshot.has_udfs
    assert snapshot.format_version == 2
    assert snapshot.to_dict()["udfs"] == {"scoring": ["boost", "shout"]}
    assert snapshot.functions_of("scoring") == ("boost", "shout")
    assert snapshot.functions_of("nolib") == ()
    assert snapshot.function_names == {"scoring.boost", "scoring.shout"}


def test_no_udfs_is_not_the_same_as_not_knowing():
    none_there = snap(udfs=())

    assert none_there.has_udfs
    assert none_there.udfs == ()
    assert none_there.format_version == 2
    assert none_there != snap()


def test_the_catalog_is_normalised_whatever_the_form_it_is_given_in():
    assert snap(udfs=[("b", ["y", "x"]), ("a", ["z"])]) == snap(
        udfs={"a": ["z"], "b": ["x", "y"]}
    )


@pytest.mark.parametrize("udfs", [{"lib": [1]}, {1: ["f"]}])
def test_the_names_of_the_catalog_are_strings(udfs):
    with pytest.raises((TypeError, ValueError)):
        snap(udfs=udfs)


def test_the_catalog_survives_json_and_a_file(tmp_path):
    snapshot = snap(udfs={"scoring": ["boost", "shout"]})

    assert Snapshot.from_json(snapshot.to_json()) == snapshot
    snapshot.save(tmp_path / "s.json")
    assert Snapshot.load(tmp_path / "s.json") == snapshot


def test_a_file_of_format_1_still_loads_and_has_no_catalog():
    data = snap().to_dict()
    assert data["format_version"] == 1

    loaded = Snapshot.from_dict(data)

    assert loaded.udfs is None
    assert loaded.format_version == 1


def test_a_file_of_format_1_cannot_have_udfs():
    data = {**snap().to_dict(), "udfs": {"scoring": ["boost"]}}

    with pytest.raises(ValueError, match=r"udfs.*format_version 2"):
        Snapshot.from_dict(data)


def test_a_file_of_format_2_without_udfs_loads():
    data = {**snap().to_dict(), "format_version": 2}

    assert Snapshot.from_dict(data).udfs is None


def test_from_schema_takes_the_udfs():
    snapshot = Snapshot.from_schema(
        nodes={"A": []}, relationships=[], udfs={"scoring": ["boost", "shout"]}
    )

    assert snapshot.udfs == SCORING
    assert Snapshot.from_schema(nodes={"A": []}, relationships=[]).udfs is None


def test_the_catalog_is_part_of_what_makes_two_snapshots_equal():
    assert dataclasses.replace(snap(udfs={"a": ["f"]}), udfs=(("a", ("f",)),)) == snap(
        udfs={"a": ["f"]}
    )
    assert snap(udfs={"a": ["f"]}) != snap(udfs={"a": ["g"]})


# ----------------------------------------------------------------------------------------------------
# Captured from a database


def test_the_udfs_of_the_instance_are_captured(falkordb_client, scoring):
    snapshot = Snapshot.capture(falkordb_client, "kg")

    assert snapshot.udfs == SCORING
    assert snapshot.format_version == 2


def test_the_udfs_are_captured_with_the_stock_client_too(
    plain_falkordb_client, scoring
):
    # the stock falkordb client may give the reply in the other protocol
    assert Snapshot.capture(plain_falkordb_client, "kg").udfs == SCORING


def test_a_server_with_no_udfs_gives_an_empty_catalog(falkordb_client):
    assert Snapshot.capture(falkordb_client, "kg").udfs == ()


def test_they_can_be_left_out(falkordb_client, scoring):
    snapshot = Snapshot.capture(falkordb_client, "kg", udfs=False)

    assert snapshot.udfs is None
    assert snapshot.format_version == 1


def test_the_udfs_belong_to_the_instance_not_to_the_graph(falkordb_client, scoring):
    # a graph that does not exist yet is an empty snapshot, and the instance still has its functions
    assert Snapshot.capture(falkordb_client, "no_such_graph").udfs == SCORING


def test_the_async_capture_gives_the_same(falkordb_client, run_async, scoring):
    async def ask(client):
        return await Snapshot.capture_async(client, "kg")

    assert run_async(ask).udfs == SCORING


def test_the_async_capture_can_leave_them_out(run_async, scoring):
    async def ask(client):
        return await Snapshot.capture_async(client, "kg", udfs=False)

    assert run_async(ask).udfs is None


def test_the_code_of_a_function_is_never_kept(falkordb_client, scoring):
    assert "toUpperCase" not in Snapshot.capture(falkordb_client, "kg").to_json()


# ----------------------------------------------------------------------------------------------------
# Noticing a change: the fingerprint of a live snapshot


def test_loading_or_deleting_a_library_changes_the_fingerprint(falkordb_client):
    before = fingerprint(falkordb_client, "kg")
    falkordb_client.execute_command("GRAPH.UDF", "LOAD", "REPLACE", "scoring", CODE)
    try:
        loaded = fingerprint(falkordb_client, "kg")
        assert loaded != before
        assert fingerprint(falkordb_client, "kg") == loaded
    finally:
        falkordb_client.execute_command("GRAPH.UDF", "DELETE", "scoring")

    assert fingerprint(falkordb_client, "kg") == before


def test_a_fingerprint_without_the_udfs_does_not_see_them(falkordb_client):
    before = fingerprint(falkordb_client, "kg", udfs=False)
    falkordb_client.execute_command("GRAPH.UDF", "LOAD", "REPLACE", "scoring", CODE)
    try:
        assert fingerprint(falkordb_client, "kg", udfs=False) == before
    finally:
        falkordb_client.execute_command("GRAPH.UDF", "DELETE", "scoring")


def test_a_live_snapshot_captures_again_when_a_library_is_loaded(falkordb_client):
    live = LiveSnapshot(falkordb_client, "kg", refresh_every=timedelta(hours=1))
    first = live.refresh()
    assert first.udfs == ()
    assert live.refresh() is first  # nothing changed: confirmed, not captured again

    falkordb_client.execute_command("GRAPH.UDF", "LOAD", "REPLACE", "scoring", CODE)
    try:
        second = live.refresh()
        assert second is not first
        assert second.udfs == SCORING
    finally:
        falkordb_client.execute_command("GRAPH.UDF", "DELETE", "scoring")

    assert live.refresh().udfs == ()


def test_a_live_snapshot_without_udfs_does_not_ask_for_them(falkordb_client, scoring):
    live = LiveSnapshot(
        falkordb_client, "kg", refresh_every=timedelta(hours=1), udfs=False
    )

    assert live.refresh().udfs is None
