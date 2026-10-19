"""The labelled accuracy corpus (``tests/accuracy/``): the validators must say what a person who knows the graph says.

The golden corpus of ``test_corpus.py`` records what the validators answered, which catches a change but not a mistake.
This one records what the answers should be, written by hand from the Cypher the queries are in and the graphs of
``accuracy/graphs.json``. Valid queries are in it on purpose: a valid query that is flagged is the failure that
matters most for generated queries. ``scripts/accuracy.py`` prints the precision and recall of each issue code.

An issue that comes from FalkorDB itself (a syntax error, an execution failure) is labelled with what the database of
the tests (FalkorDBLite) answers, so those labels belong to its version.
"""

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent / "accuracy"))

import harness

CASES = harness.load_cases()
CASES_BY_ID = {case.id: case for case in CASES}
SCORED = {"schema", "properties"}
MINIMUM = 3  # positive cases, and valid look-alikes, of each code


@pytest.fixture(scope="module")
def accuracy_graphs(falkordb_client):
    """The graphs of the corpus, on the embedded database of the tests."""
    graphs = harness.load_graphs()
    harness.seed(falkordb_client, graphs)
    yield graphs
    for name in graphs:
        falkordb_client.select_graph(name).delete()


def _params():
    return [
        pytest.param(
            case,
            id=case.id,
            marks=[pytest.mark.xfail(strict=True, reason=case.known_gap)]
            if case.known_gap
            else [],
        )
        for case in CASES
    ]


@pytest.mark.parametrize("case", _params())
def test_the_validators_agree_with_the_labels(falkordb_client, accuracy_graphs, case):
    outcome = harness.run_case(falkordb_client, case)

    assert harness.disagreements(case, outcome) == [], case.query


# ----------------------------------------------------------------------------------------------------
# The corpus itself


def test_every_case_is_well_formed():
    graphs = harness.load_graphs()
    known = set(harness.reportable_codes())
    assert len({case.id for case in CASES}) == len(CASES)
    for case in CASES:
        where = f"{case.id}: {case.query}"
        assert case.graph in graphs, where
        assert case.validators and set(case.validators) <= {
            "syntax",
            "schema",
            "properties",
        }, where
        assert set(case.options) <= {
            "read_only",
            "check_multilabeled_nodes",
            "parameters",
            "check_types",
            "strict",
        }, where
        for issue in case.issues:
            code = issue["code"]
            assert (
                code == harness.ANY_DATABASE_ERROR or harness.full_code(code) in known
            ), where
        assert set(map(harness.full_code, case.near_miss)) <= known, where
        if case.has_score:
            assert len(SCORED & set(case.validators)) == 1, where
        if case.kg_valid is not None:
            assert set(case.validators) == {"syntax", "schema", "properties"}, where
        if case.valid is not None:
            assert "syntax" in case.validators, where
        assert case.known_gap is None or case.known_gap.strip(), where


def test_every_code_has_cases_that_report_it_and_valid_look_alikes():
    positive: dict[str, int] = {}
    near_miss: dict[str, int] = {}
    for case in CASES:
        for issue in case.issues:
            code = harness.full_code(issue["code"])
            positive[code] = positive.get(code, 0) + 1
        for code in case.near_miss:
            near_miss[harness.full_code(code)] = (
                near_miss.get(harness.full_code(code), 0) + 1
            )
    for code in harness.reportable_codes():
        assert positive.get(code, 0) >= MINIMUM, f"{code}: cases that report it"
        assert near_miss.get(code, 0) >= MINIMUM, f"{code}: valid look-alikes"


def test_the_valid_queries_are_a_real_share_of_the_corpus():
    clean = [case for case in CASES if not case.issues and case.valid is not False]
    assert len(clean) >= len(CASES) // 3


# ----------------------------------------------------------------------------------------------------
# The harness: a wrong label must be seen (no database needed)

ISSUE = {"code": "cypher_guardrail.Warning.UnknownLabel", "label": "Ghost"}


def _case(validators=("schema",), **fields: Any):
    return harness.Case(
        id="x.1", graph="movies", query="q", validators=list(validators), **fields
    )


@pytest.mark.parametrize(
    ("expected", "found", "agree"),
    [
        ([{"code": "Warning.UnknownLabel", "label": "Ghost"}], [ISSUE], True),
        ([{"code": "Warning.UnknownLabel"}], [ISSUE], True),  # fields not written
        ([], [], True),
        (
            [{"code": "Warning.UnknownLabel", "label": "Spirit"}],
            [ISSUE],
            False,
        ),  # wrong field
        ([{"code": "Warning.UnknownRelationshipType"}], [ISSUE], False),  # wrong code
        ([{"code": "Warning.UnknownLabel"}], [], False),  # an issue not found
        ([], [ISSUE], False),  # an issue nobody labelled: a false positive
        ([{"code": "Warning.UnknownLabel"}], [ISSUE, ISSUE], False),  # one too many
    ],
)
def test_the_harness_sees_a_label_that_is_wrong(expected, found, agree):
    case = _case(issues=expected)

    result = harness.disagreements(case, harness.Outcome(issues=found))

    assert (result == []) is agree


def test_the_harness_checks_the_verdicts_too():
    outcome = harness.Outcome(issues=[], valid=True, scores={"schema": 0.5})

    assert harness.disagreements(_case(has_score=True, score=0.5), outcome) == []
    assert harness.disagreements(_case(has_score=True, score=1.0), outcome) != []
    assert (
        harness.disagreements(_case(validators=["syntax"], valid=False), outcome) != []
    )
    clean = harness.Outcome(
        issues=[], valid=True, scores={"schema": 1.0, "properties": None}
    )
    assert harness.disagreements(_case(kg_valid=True), clean) == []
    assert harness.disagreements(_case(kg_valid=False), clean) != []


def test_a_database_error_can_be_labelled_with_any_of_them():
    error = {
        "code": "FalkorDB.DatabaseError.Statement.ExecutionFailed",
        "description": "x",
    }

    assert (
        harness.disagreements(
            _case(issues=[{"code": "FalkorDB.*"}]), harness.Outcome(issues=[error])
        )
        == []
    )
    assert (
        harness.disagreements(
            _case(issues=[{"code": "FalkorDB.*"}]), harness.Outcome(issues=[ISSUE])
        )
        != []
    )


def test_the_tally_counts_what_was_found_missed_and_made_up():
    wrong_label = _case(issues=[{"code": "Warning.UnknownLabel", "label": "Ghost"}])
    cases_and_outcomes = [
        (wrong_label, harness.Outcome(issues=[ISSUE])),  # found
        (wrong_label, harness.Outcome(issues=[])),  # missed
        (_case(issues=[]), harness.Outcome(issues=[ISSUE])),  # made up
    ]

    counts = harness.tally(cases_and_outcomes)["Warning.UnknownLabel"]

    assert (counts["tp"], counts["fn"], counts["fp"]) == (1, 1, 1)
