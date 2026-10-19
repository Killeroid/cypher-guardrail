"""Measure the validators against the labelled corpus in ``tests/accuracy/``.

Prints, for each issue code, how many labelled issues were found (TP), missed (FN) and reported without a label (FP),
and the precision and recall; then the same totals for each mode (online, offline from a snapshot, and hybrid), and
the cases the library gets wrong. Needs FalkorDBLite (Python >= 3.12): the corpus runs on an embedded database that
the script starts and removes.

Run with: ``uv run python scripts/accuracy.py``. The exit status is 1 when a case disagrees with its labels, or when a
case marked ``known_gap`` (or a ``mode_gaps`` entry) agrees: the mark is then out of date.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "accuracy"))

import harness


def ratio(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator:6.1%}" if denominator else "     -"


def totals_of(results) -> tuple[int, int, int]:
    counts = harness.tally(results).values()
    return (
        sum(c["tp"] for c in counts),
        sum(c["fp"] for c in counts),
        sum(c["fn"] for c in counts),
    )


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
        snapshots = harness.snapshots_of(client)
        results = {
            mode: [
                (
                    harness.for_mode(case, mode),
                    harness.run_case(client, case, mode, snapshots),
                    case,
                )
                for case in cases
                if harness.applies_to(case, mode)
            ]
            for mode in harness.MODES
        }
        client.close()

    failures, stale = [], []
    for mode, rows in results.items():
        for case, outcome, original in rows:
            found = harness.disagreements(case, outcome)
            gap = original.known_gap or original.mode_gaps.get(mode)
            if found and not gap:
                failures.append((mode, original, found))
            elif not found and gap:
                stale.append((mode, original))

    print(f"{len(cases)} cases, {sum(1 for c in cases if c.known_gap)} known gaps\n")
    online = [(case, outcome) for case, outcome, _ in results["online"]]
    totals = harness.tally(online)
    print(f"{'code':50} {'TP':>4} {'FP':>4} {'FN':>4}  precision  recall")
    # the known gaps are part of the measure: the labels say what is true, and the library misses them
    for code in sorted(totals):
        tp, fp, fn = (totals[code][key] for key in ("tp", "fp", "fn"))
        print(
            f"{code:50} {tp:4} {fp:4} {fn:4}  {ratio(tp, tp + fp):>9}  {ratio(tp, tp + fn):>6}"
        )

    print(f"\n{'mode':50} {'TP':>4} {'FP':>4} {'FN':>4}  precision  recall")
    for mode, rows in results.items():
        tp, fp, fn = totals_of([(case, outcome) for case, outcome, _ in rows])
        label = f"{mode} ({len(rows)} cases)"
        print(
            f"{label:50} {tp:4} {fp:4} {fn:4}  {ratio(tp, tp + fp):>9}  {ratio(tp, tp + fn):>6}"
        )

    clean = [(c, o) for c, o, _ in results["online"] if not c.issues and c.kg_valid]
    flagged = [c for c, o in clean if harness.disagreements(c, o)]
    print(f"\nvalid queries flagged: {len(flagged)} of {len(clean)}")

    if failures:
        print(f"\n{len(failures)} case(s) disagree with their labels:")
        for mode, case, found in failures:
            print(f"  [{mode}] {case.id}: {case.query}")
            for line in found:
                print(f"      {line}")
    if stale:
        print("\ncases marked as a gap that now agree (take the mark out):")
        for mode, case in stale:
            print(f"  [{mode}] {case.id}: {case.query}")
    return 1 if failures or stale else 0


if __name__ == "__main__":
    sys.exit(main())
