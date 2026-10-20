"""What several test files share: plain helpers and the hand-written snapshot of the lab graph.

(Fixtures are in ``conftest.py``. This module is imported by name, as ``import support``: pytest puts this folder on
``sys.path``.)
"""

from cypher_guardrail import Snapshot
from cypher_guardrail.issues import PARAMETER_NOT_PROVIDED


def codes(metadata):
    """The code of each issue, in order."""
    return [issue["code"] for issue in metadata]


def code_set(issues):
    """The set of the codes of some issues or notifications."""
    return {issue["code"] for issue in issues}


def missing_parameters(metadata):
    """The ``parameters`` of each ParameterNotProvided issue."""
    return [i["parameters"] for i in metadata if i["code"] == PARAMETER_NOT_PROVIDED]


# Kinds of nodes: A, B, C, D; A and Extra together; Src, Mid and Dst; and a node with no label.
# Edges: A-R->B, B-S->C, C-R->A, C-R->C, (A+Extra)-T->B, ()-U->A, Src-R->Mid, Mid-S->Dst
# Src-R->Mid and Mid-S->Dst are two edges at two *different* Mid nodes in the real data this mirrors, which is why a
# path through Mid exists between the kinds but not between real nodes.
LAB = Snapshot.from_dict(
    {
        "format_version": 1,
        "graph": "lab",
        "captured_at": None,
        "labels": ["A", "B", "C", "D", "Dst", "Extra", "Mid", "Src"],
        "relationship_types": ["R", "S", "T", "U"],
        "property_keys": ["n", "p", "q", "w", "z"],
        "indexes": [
            ["A", "NODE", {"p": ["RANGE"]}],
            ["R", "RELATIONSHIP", {"w": ["FULLTEXT"]}],
            ["B", "NODE", {"p": ["FULLTEXT"], "q": ["VECTOR"]}],
        ],
        "node_label_sets": [
            ["A"],
            ["B"],
            ["C"],
            ["A", "Extra"],
            ["D"],
            ["Src"],
            ["Mid"],
            ["Dst"],
            [],
        ],
        "edges": [
            [["A"], "R", ["B"]],
            [["B"], "S", ["C"]],
            [["C"], "R", ["A"]],
            [["C"], "R", ["C"]],
            [["A", "Extra"], "T", ["B"]],
            [[], "U", ["A"]],
            [["Src"], "R", ["Mid"]],
            [["Mid"], "S", ["Dst"]],
        ],
        "node_properties": {
            "A": ["p", "q"],
            "B": ["p"],
            "D": ["z"],
            "Mid": ["n"],
            "Extra": ["p"],
        },
        "relationship_properties": {"R": ["w"], "S": [], "T": [], "U": []},
    }
)
