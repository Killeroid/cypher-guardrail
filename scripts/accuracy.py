"""Measure the validators against the labelled corpus in ``tests/accuracy/``.

Prints, for each issue code, how many labelled issues were found (TP), missed (FN) and reported without a label (FP),
and the precision and recall; then the cases the library gets wrong. Needs FalkorDBLite (Python >= 3.12): the corpus
runs on an embedded database that the script starts and removes.

Run with: ``uv run python scripts/accuracy.py``. The exit status is 1 when a case disagrees with its labels, or when a
case marked ``known_gap`` agrees (the mark is then out of date).
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "accuracy"))

import harness


def ratio(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator:6.1%}" if denominator else "     -"


def main() -> int:
    try:
        from redislite.falkordb_client import FalkorDB
    except ImportError:
        print("FalkorDBLite (Python >= 3.12) is needed: `uv sync` installs it.")
        return 2

    cases = harness.load_cases()
    with tempfile.TemporaryDirectory() as folder:
        client = FalkorDB(str(Path(folder) / "accuracy.db"))
        harness.seed(client)
        results = [(case, harness.run_case(client, case)) for case in cases]
        client.close()

    failures, stale = [], []
    for case, outcome in results:
        found = harness.disagreements(case, outcome)
        if found and not case.known_gap:
            failures.append((case, found))
        elif not found and case.known_gap:
            stale.append(case)

    totals = harness.tally(results)
    print(f"{len(cases)} cases, {sum(1 for c in cases if c.known_gap)} known gaps\n")
    print(f"{'code':50} {'TP':>4} {'FP':>4} {'FN':>4}  precision  recall")
    # the known gaps are part of the measure: the labels say what is true, and the library misses them
    for code in sorted(totals):
        tp, fp, fn = (totals[code][key] for key in ("tp", "fp", "fn"))
        print(
            f"{code:50} {tp:4} {fp:4} {fn:4}  {ratio(tp, tp + fp):>9}  {ratio(tp, tp + fn):>6}"
        )
    all_tp, all_fp, all_fn = (
        sum(t[key] for t in totals.values()) for key in ("tp", "fp", "fn")
    )
    print(
        f"{'all':50} {all_tp:4} {all_fp:4} {all_fn:4}  {ratio(all_tp, all_tp + all_fp):>9}  {ratio(all_tp, all_tp + all_fn):>6}"
    )

    clean = [(c, o) for c, o in results if not c.issues and c.kg_valid]
    flagged = [c for c, o in clean if harness.disagreements(c, o)]
    print(f"\nvalid queries flagged: {len(flagged)} of {len(clean)}")

    if failures:
        print(f"\n{len(failures)} case(s) disagree with their labels:")
        for case, found in failures:
            print(f"  {case.id}: {case.query}")
            for line in found:
                print(f"      {line}")
    if stale:
        print("\ncases marked known_gap that now agree (take the mark out):")
        for case in stale:
            print(f"  {case.id}: {case.query}")
    return 1 if failures or stale else 0


if __name__ == "__main__":
    sys.exit(main())
