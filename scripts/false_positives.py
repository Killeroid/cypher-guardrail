"""Look for false positives on real queries: run the gold queries of Neo4j's text2cypher dataset through the validators.

Each row of ``neo4j/text2cypher-2025v1`` (Apache-2.0 according to its card; not stored in this repository) has a gold
query and the schema it was written for. The schema is read as a snapshot, and the schema and properties validators
run on the gold query offline (no database). A query that is flagged is a candidate: compare it with the schema of
its row. Gold queries are sometimes wrong, and when this was first run (the whole test split, 2026-10-20) every
group of findings that was checked was a mistake of the gold query: a relationship written the wrong way round,
a label or a property with the wrong case or the wrong name, a property of another label.

Needs the network for the rows (a page of 100 at a time, kept in the cache folder). It is not part of the tests.

Run with: ``uv run python scripts/false_positives.py [--limit N] [--show N] [--cache DIR]``.

``--check`` compares what is flagged with ``tests/accuracy/false_positives_baseline.json`` (the whole test split) and
exits with 1 when a code has more findings than the baseline, or a query makes a validator fail: a change that makes
the validators flag more of the real queries is seen. ``--write-baseline`` records the baseline (after reading the new
findings: a change that flags what is wrong in the gold queries is no regression).
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from http import HTTPStatus
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "accuracy"))

import text2cypher

from cypher_guardrail import PropertiesValidator, SchemaValidator, Snapshot

BASELINE = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "accuracy"
    / "false_positives_baseline.json"
)
API = (
    "https://datasets-server.huggingface.co/rows?dataset=neo4j%2Ftext2cypher-2025v1"
    "&config=default&split=test&offset={offset}&length=100"
)


def download(url: str, attempts: int = 8) -> bytes:
    """The body of ``url``. The server limits how fast pages can be asked for (HTTP 429): wait, as it says, and ask again."""
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code != HTTPStatus.TOO_MANY_REQUESTS or attempt == attempts - 1:
                raise
            wait = float(error.headers.get("Retry-After") or 2 ** (attempt + 1))
            print(f"  rate limited: waiting {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


def fetch(limit: int, cache: Path) -> list[dict]:
    cache.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for offset in range(0, limit, 100):
        page = cache / f"test_{offset}.json"
        if not page.exists():
            page.write_bytes(download(API.format(offset=offset)))
        rows += [item["row"] for item in json.loads(page.read_text())["rows"]]
    return rows[:limit]


def describe(issue: dict) -> str:
    """What an issue is about, short enough to group by."""
    code = issue["code"].removeprefix("cypher_guardrail.Warning.")
    for field in (
        "original_path",
        "path",
        "name",
        "label",
        "property",
        "relationship_type",
    ):
        if field in issue:
            extra = f" {issue['properties']}" if "properties" in issue else ""
            return f"{code}: {issue[field]}{extra}"
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--limit", type=int, default=4500, help="rows to read (the test split has 4438)"
    )
    parser.add_argument(
        "--show", type=int, default=8, help="groups of findings to show for each code"
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path.home() / ".cache" / "cypher-guardrail" / "text2cypher",
        help="where the pages of rows are kept",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when more is flagged than the baseline",
    )
    parser.add_argument(
        "--write-baseline", action="store_true", help="record what is flagged now"
    )
    args = parser.parse_args()

    rows = fetch(args.limit, args.cache)
    snapshots: dict[str, Snapshot | None] = {}
    validated = skipped = clean = 0
    groups: dict[str, Counter] = defaultdict(Counter)
    crashed = []
    for row in rows:
        schema = row["schema"]
        if schema not in snapshots:
            snapshots[schema] = text2cypher.snapshot_of(schema)
        snapshot = snapshots[schema]
        if snapshot is None:
            skipped += 1
            continue
        validated += 1
        try:
            _, schema_issues = SchemaValidator(snapshot).validate(row["cypher"])
            _, property_issues = PropertiesValidator(snapshot).validate(row["cypher"])
        except Exception as error:  # noqa: BLE001 - a query that breaks a validator is a finding
            crashed.append((row["instance_id"], f"{type(error).__name__}: {error}"))
            continue
        issues = schema_issues + property_issues
        if not issues:
            clean += 1
        for issue in issues:
            code = re.sub(r"^cypher_guardrail\.Warning\.", "", issue["code"])
            groups[code][describe(issue)] += 1

    flagged = validated - clean - len(crashed)
    print(
        f"{len(rows)} rows: {validated} validated, {skipped} skipped (a schema that is not read)"
    )
    print(
        f"{clean} clean, {flagged} flagged ({flagged / max(validated, 1):.1%}), {len(crashed)} made a validator fail\n"
    )
    for code, counter in sorted(
        groups.items(), key=lambda item: -sum(item[1].values())
    ):
        print(f"{code}: {sum(counter.values())} findings, {len(counter)} different")
        for text, count in counter.most_common(args.show):
            print(f"  {count:3}x {text}")
    for instance, error in crashed:
        print(f"CRASH {instance}: {error}")
    found = {
        "validated": validated,
        "flagged": flagged,
        "findings": {code: sum(counter.values()) for code, counter in groups.items()},
    }
    if args.write_baseline:
        BASELINE.write_text(json.dumps(found, indent=1, sort_keys=True) + "\n")
        print(f"recorded {BASELINE}")
    if args.check:
        return worse_than_the_baseline(found, len(crashed))
    return 0


def worse_than_the_baseline(found: dict, crashed: int) -> int:
    """1 when more is flagged than the baseline says (or a validator failed), after saying what."""
    baseline = json.loads(BASELINE.read_text())
    worse = [f"{crashed} quer(ies) made a validator fail"] if crashed else []
    if found["validated"] != baseline["validated"]:
        worse.append(
            f"{found['validated']} rows were validated, the baseline has {baseline['validated']}: "
            "the dataset or the reading of its schemas changed (record the baseline again)"
        )
    for code, count in sorted(found["findings"].items()):
        before = baseline["findings"].get(code, 0)
        if count > before:
            worse.append(f"{code}: {count} findings, the baseline has {before}")
    for line in worse:
        print(f"WORSE: {line}")
    return 1 if worse else 0


if __name__ == "__main__":
    sys.exit(main())
