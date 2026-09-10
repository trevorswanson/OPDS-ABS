"""Fail CI if statement or branch coverage drops below the configured floor.

coverage.py's own `fail_under` only gates a single number: pure statement
coverage when branch tracking is off, or a blended statement+branch number
once it's on. Neither is "statement >= floor AND branch >= floor"
independently, which is what we actually want, so this checks both
metrics from a `coverage json` report explicitly.
"""
import json
import sys

FLOOR = 90.0


def main():
    """Check statement and branch coverage against FLOOR, exiting 1 if either fails."""
    with open("coverage.json") as f:
        totals = json.load(f)["totals"]

    metrics = [
        ("Statement", totals.get("percent_statements_covered")),
        ("Branch", totals.get("percent_branches_covered")),
    ]

    failed = False
    for label, pct in metrics:
        if pct is None:
            print(f"{label} coverage: unavailable")
            continue
        ok = pct >= FLOOR
        failed = failed or not ok
        print(f"{label} coverage: {pct:.2f}% (floor {FLOOR:.0f}%) - {'OK' if ok else 'FAIL'}")

    if failed:
        print(f"\nCoverage floor not met (every metric must be >= {FLOOR:.0f}%).")
        sys.exit(1)


if __name__ == "__main__":
    main()
