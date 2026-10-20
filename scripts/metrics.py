"""Measure the size of the code that the refactoring plan sets budgets for, and fail when it grows past a budget.

Measures, over ``src/cypher_guardrail``: the highest cyclomatic complexity of a function (ruff's C901), the longest
function in lines, and the number of methods with a name-mangled ``__name``. The budgets are in ``pyproject.toml``
under ``[tool.metrics]``; they only go down. With ``--check`` the exit status is 1 when a measure is over its budget, and
a measure that is under it is reported, so the budget can be lowered.

Run with: ``uv run python scripts/metrics.py [--check]``.
"""

import argparse
import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "cypher_guardrail"


def max_complexity() -> tuple[int, str]:
    """The highest complexity of a function, and where it is."""
    result = subprocess.run(
        [
            "ruff",
            "check",
            "--select=C901",
            "--config=lint.mccabe.max-complexity=0",
            "--output-format=json",
            "--no-cache",
            str(SOURCE),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    worst = (0, "")
    for finding in json.loads(result.stdout):
        score = int(finding["message"].split("(")[1].split(" ")[0])
        where = f"{Path(finding['filename']).name}:{finding['location']['row']}"
        worst = max(worst, (score, where))
    return worst


def functions() -> list[tuple[int, str, str]]:
    """``(lines, where, name)`` of each function and method."""
    found = []
    for path in sorted(SOURCE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                lines = (node.end_lineno or node.lineno) - node.lineno + 1
                found.append((lines, f"{path.name}:{node.lineno}", node.name))
    return found


def mangled_methods() -> int:
    """The methods whose name starts with two underscores and does not end with two."""
    return sum(
        1
        for _, _, name in functions()
        if name.startswith("__") and not name.endswith("__")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="fail when over budget")
    args = parser.parse_args()

    budget = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["metrics"]
    complexity, complexity_at = max_complexity()
    longest, longest_at, _ = max(functions())
    measured = {
        "max_complexity": (complexity, f"at {complexity_at}"),
        "max_function_lines": (longest, f"at {longest_at}"),
        "mangled_methods": (mangled_methods(), ""),
    }
    over = False
    for key, (value, where) in measured.items():
        limit = budget[key]
        note = "ok"
        if value > limit:
            note, over = "OVER THE BUDGET", True
        elif value < limit:
            note = f"under the budget: lower it to {value}"
        print(f"{key:20} {value:4} (budget {limit:4}) {note} {where}".rstrip())
    return 1 if args.check and over else 0


if __name__ == "__main__":
    sys.exit(main())
