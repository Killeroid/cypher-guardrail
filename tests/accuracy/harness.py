"""The labelled accuracy corpus: loading, running and scoring it.

The corpus says, for each query, which issues a person who knows Cypher and the graph says it has. The tests
(``tests/test_accuracy.py``) fail on any disagreement, and ``scripts/accuracy.py`` prints the precision and recall of
each issue code. A false positive (a valid query that is flagged) is the failure that matters most for generated
queries.

A case is a JSON object:

``id``         a unique name, ``<file>.<group>.<n>``.
``graph``      a graph of ``graphs.json``.
``query``      the Cypher query.
``validators`` which of ``"syntax"``, ``"schema"`` and ``"properties"`` run (the default of the file).
``options``    ``read_only``, ``check_multilabeled_nodes``, ``parameters`` and ``check_performance`` (syntax), ``check_types`` and ``strict``
               (properties).
``issues``     the issues the validators must report, and no others: ``{"code": ..., <fields>}``. ``code`` is short
               (``Warning.UnknownLabel`` for ``cypher_guardrail.Warning.UnknownLabel``; a FalkorDB code is written in
               full; ``FalkorDB.*`` is any error of the database). Only the fields written are compared.
``valid``      (optional) what ``SyntaxValidator`` returns.
``score``      (optional) the score, when one of ``schema`` and ``properties`` runs.
``kg_valid``   (optional) the KG Valid Query of the README, when all three run.
``near_miss``  the codes this query is a valid look-alike of (it must not report them).
``known_gap``  (optional) why the library disagrees with the truth written in the case. The case is an expected
               failure: when the library gets it right, the test fails, so the label is taken out.
``mode_gaps``  (optional) the same for one mode only: ``{"offline": "why"}``. The schema and properties validators also
               run offline (from a snapshot of the graph) and hybrid (a snapshot and the database); the labels are the
               same, minus what only ``SyntaxValidator`` reports, which needs the database. A snapshot never invents an
               issue, but cannot see some (a path of two or more hops is only a "maybe"), which is what a mode gap says.
"""

import json
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from cypher_guardrail import (
    PropertiesValidator,
    SchemaValidator,
    Snapshot,
    SyntaxValidator,
)
from cypher_guardrail import issues as _issues_module

DIRECTORY = Path(__file__).parent
CASE_FILES = (
    "syntax",
    "schema",
    "properties",
    "valid",
    "udf",
    "performance",
    "aggregates",
)
ANY_DATABASE_ERROR = "FalkorDB.*"
MODES = ("online", "offline", "hybrid")
# What only SyntaxValidator reports: it needs the database, so it is not part of the other modes
SYNTAX_CODES = {
    "Error.ArithmeticError",
    "Error.ConflictingVariables",
    "Error.WriteQuery",
    "Warning.ParameterNotProvided",
    "Warning.WriteQuery",
}
# Issue codes that are not reported to the user: the code of an error the library raises inside
INTERNAL_CODES = {"cypher_guardrail.Error.NotReadOnly"}


@dataclass
class Case:
    id: str
    graph: str
    query: str
    validators: list[str]
    options: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    valid: bool | None = None
    score: float | None = None
    has_score: bool = False
    kg_valid: bool | None = None
    near_miss: list[str] = field(default_factory=list)
    known_gap: str | None = None
    mode_gaps: dict[str, str] = field(default_factory=dict)


def load_graphs() -> dict[str, dict[str, Any]]:
    return json.loads((DIRECTORY / "graphs.json").read_text())


def _case_of(raw: dict[str, Any]) -> Case:
    return Case(
        id=raw["id"],
        graph=raw.get("graph", "movies"),
        query=raw["query"],
        validators=raw["validators"],
        options=raw.get("options", {}),
        issues=raw.get("issues", []),
        valid=raw.get("valid"),
        score=raw.get("score"),
        has_score="score" in raw,
        kg_valid=raw.get("kg_valid"),
        near_miss=raw.get("near_miss", []),
        known_gap=raw.get("known_gap"),
        mode_gaps=raw.get("mode_gaps", {}),
    )


def load_cases() -> list[Case]:
    return [
        _case_of(raw)
        for name in CASE_FILES
        for raw in json.loads((DIRECTORY / f"{name}.json").read_text())
    ]


def seed(client, graphs=None) -> None:
    """Create the graphs of the corpus on ``client`` (an embedded FalkorDB)."""
    for name, spec in (graphs or load_graphs()).items():
        graph = client.select_graph(name)
        for statement in spec["seed"] + spec.get("indexes", []):
            graph.query(statement)
        # UDFs belong to the instance, not to the graph: the spec of a graph only says where they are written
        for library, code in spec.get("udfs", {}).items():
            client.execute_command("GRAPH.UDF", "LOAD", "REPLACE", library, code)


def unseed(client, graphs=None) -> None:
    """Remove what ``seed`` created: the graphs and the UDF libraries."""
    for name, spec in (graphs or load_graphs()).items():
        client.select_graph(name).delete()
        for library in spec.get("udfs", {}):
            client.execute_command("GRAPH.UDF", "DELETE", library)


def snapshots_of(client, graphs=None) -> dict[str, Snapshot]:
    """A snapshot of each graph of the corpus (with the types of the values), for the offline and hybrid modes."""
    return {
        name: Snapshot.capture(client, name, types=True)
        for name in (graphs or load_graphs())
    }


def _is_syntax_code(code: str) -> bool:
    return code.startswith("FalkorDB.") or code in SYNTAX_CODES


def applies_to(case: Case, mode: str) -> bool:
    """Whether ``mode`` has anything to say about ``case``: the other modes run no ``SyntaxValidator``."""
    return mode == "online" or bool(set(case.validators) - {"syntax"})


def for_mode(case: Case, mode: str) -> Case:
    """``case`` as the labels read in ``mode``: without what only ``SyntaxValidator`` reports."""
    if mode == "online":
        return case
    return replace(
        case,
        issues=[i for i in case.issues if not _is_syntax_code(i["code"])],
        valid=None,
        kg_valid=None,
    )


def full_code(code: str) -> str:
    return code if code.startswith("FalkorDB.") else f"cypher_guardrail.{code}"


def reportable_codes() -> list[str]:
    """Every code the library can report, from ``cypher_guardrail.issues``."""
    codes = {
        value
        for name, value in vars(_issues_module).items()
        if name.isupper() and isinstance(value, str)
    }
    return sorted(codes - INTERNAL_CODES)


@dataclass
class Outcome:
    issues: list[dict[str, Any]]
    valid: bool | None = None
    scores: dict[str, float | None] = field(default_factory=dict)


def run_case(
    client,
    case: Case,
    mode: str = "online",
    snapshots: dict[str, Snapshot] | None = None,
) -> Outcome:
    """Run the validators of ``case`` on its query, the way a consumer would.

    ``mode`` is ``online`` (the database answers), ``offline`` (a snapshot of the graph answers: there is no
    ``SyntaxValidator``) or ``hybrid`` (the snapshot where it is sure, else the database).
    """
    options = case.options
    outcome = Outcome(issues=[])
    driver: Any
    held: Snapshot | None  # the snapshot that a hybrid validator is given
    if mode == "online":
        driver, held, where = client, None, (case.graph,)
    else:
        if snapshots is None:
            raise ValueError(f"the {mode} mode needs the snapshots of the graphs")
        snapshot = snapshots[case.graph]
        if mode == "offline":
            driver, held, where = snapshot, None, ()
        else:
            driver, held, where = client, snapshot, (case.graph,)
    for name in case.validators:
        if name == "syntax":
            if mode != "online":
                continue
            validator = SyntaxValidator(
                client,
                check_multilabeled_nodes=options.get("check_multilabeled_nodes", True),
                read_only=options.get("read_only", True),
                check_performance=options.get("check_performance", False),
            )
            outcome.valid, found = validator.validate(
                case.query, case.graph, parameters=options.get("parameters")
            )
        elif name == "schema":
            outcome.scores[name], found = SchemaValidator(
                driver, snapshot=held
            ).validate(case.query, *where)
        elif name == "properties":
            validator = PropertiesValidator(
                driver, check_types=options.get("check_types", False), snapshot=held
            )
            outcome.scores[name], found = validator.validate(
                case.query, *where, strict=options.get("strict", False)
            )
        else:
            raise ValueError(f"{case.id}: unknown validator {name!r}")
        outcome.issues.extend(cast("list[dict[str, Any]]", found))
    return outcome


def _matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    code = expected["code"]
    if code == ANY_DATABASE_ERROR:
        if not actual["code"].startswith("FalkorDB."):
            return False
    elif actual["code"] != full_code(code):
        return False
    return all(
        actual.get(key) == value for key, value in expected.items() if key != "code"
    )


def pair_issues(
    expected: list[dict[str, Any]], actual: list[dict[str, Any]]
) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Match each expected issue with an issue found: (matched, expected but missing, found but not expected).

    The more specific expectations (more fields) pick first, so a general one cannot take the issue of a specific one.
    """
    remaining = list(actual)
    matched, missing = [], []
    for want in sorted(expected, key=lambda issue: -len(issue)):
        for index, got in enumerate(remaining):
            if _matches(want, got):
                matched.append((want, remaining.pop(index)))
                break
        else:
            missing.append(want)
    return matched, missing, remaining


def _same_score(expected: float | None, actual: float | None) -> bool:
    if expected is None or actual is None:
        return expected is actual
    return abs(expected - actual) < 1e-9


def disagreements(case: Case, outcome: Outcome) -> list[str]:
    """What the library gets wrong about ``case``, in words. Empty when it agrees with the labels."""
    _, missing, unexpected = pair_issues(case.issues, outcome.issues)
    found = [f"missing {issue}" for issue in missing]
    found += [
        "unexpected "
        + str({key: value for key, value in issue.items() if key != "description"})
        for issue in unexpected
    ]
    if case.valid is not None and outcome.valid != case.valid:
        found.append(f"valid: expected {case.valid}, got {outcome.valid}")
    if case.has_score:
        (score,) = outcome.scores.values()
        if not _same_score(case.score, score):
            found.append(f"score: expected {case.score}, got {score}")
    if case.kg_valid is not None:
        kg_valid = (
            outcome.valid is True
            and outcome.scores["schema"] == 1
            and outcome.scores["properties"] in (1, None)
        )
        if kg_valid != case.kg_valid:
            found.append(f"KG Valid Query: expected {case.kg_valid}, got {kg_valid}")
    return found


def tally(cases_and_outcomes) -> dict[str, Counter]:
    """``{code: Counter(tp, fp, fn)}`` over the cases (a code that no case mentions is absent)."""
    totals: dict[str, Counter] = {}
    for case, outcome in cases_and_outcomes:
        matched, missing, unexpected = pair_issues(case.issues, outcome.issues)
        for want, _ in matched:
            totals.setdefault(want["code"], Counter())["tp"] += 1
        for want in missing:
            totals.setdefault(want["code"], Counter())["fn"] += 1
        for got in unexpected:
            code = got["code"].removeprefix("cypher_guardrail.")
            totals.setdefault(code, Counter())["fp"] += 1
    return totals
