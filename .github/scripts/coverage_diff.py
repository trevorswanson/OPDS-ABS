"""Build a before/after coverage summary comment for a pull request.

Reads two `coverage json --branch` reports (PR base and PR head) plus the
list of changed opds_abs/*.py files, and writes a markdown table of each
changed file's statement/line and branch coverage delta, plus the overall
delta, to coverage_comment.md.

Statement and line coverage are the same metric in coverage.py - Python
has no separate concept of "line coverage" distinct from "statement
coverage" the way some other languages do - so they're reported as one
row. Function coverage isn't a coverage.py metric at all.
"""
import json
import sys

METRICS = [
    ("Statement/Line", "percent_statements_covered"),
    ("Branch", "percent_branches_covered"),
]


def load(path):
    """Load a coverage.py JSON report, or None if it doesn't exist."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def fmt_pct(value):
    """Format a coverage percentage, or an em dash when unavailable."""
    return f"{value:.1f}%" if value is not None else "—"


def fmt_cell(before, after):
    """Format a before -> after (delta) cell for one metric."""
    if before is None and after is None:
        return "—"
    if before is None:
        return f"{fmt_pct(after)} (new)"
    if after is None:
        return f"{fmt_pct(before)} (removed)"
    delta = after - before
    sign = "+" if delta >= 0 else ""
    return f"{fmt_pct(before)} → {fmt_pct(after)} ({sign}{delta:.1f}%)"


def summary_pct(cov, filename, key):
    """Look up one metric for one file in a coverage.py JSON report."""
    if cov is None:
        return None
    entry = cov.get("files", {}).get(filename)
    return entry["summary"].get(key) if entry else None


def totals_pct(cov, key):
    """Look up one metric from a coverage.py JSON report's totals."""
    return cov["totals"].get(key) if cov else None


def main():
    """Write the coverage comment body from head/base JSON reports."""
    changed = [line for line in sys.argv[1].splitlines() if line]
    head = load("coverage_head.json")
    base = load("coverage_base.json")

    lines = ["### Coverage report", "", "**Changed files**", ""]
    if changed:
        header = ["File"] + [label for label, _ in METRICS]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
        for filename in changed:
            cells = [f"`{filename}`"]
            for _, key in METRICS:
                before = summary_pct(base, filename, key)
                after = summary_pct(head, filename, key)
                cells.append(fmt_cell(before, after))
            lines.append("| " + " | ".join(cells) + " |")
    else:
        lines.append("No opds_abs/*.py files changed in this PR.")

    lines.append("")
    lines.append("**Overall**")
    lines.append("")
    for label, key in METRICS:
        before_total = totals_pct(base, key)
        after_total = totals_pct(head, key)
        lines.append(f"- {label}: {fmt_cell(before_total, after_total)}")

    with open("coverage_comment.md", "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
