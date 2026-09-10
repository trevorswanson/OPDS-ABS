"""Build a before/after coverage summary comment for a pull request.

Reads two `coverage json` reports (PR base and PR head) plus the list of
changed opds_abs/*.py files, and writes a markdown table of each changed
file's coverage delta plus the overall delta to coverage_comment.md.
"""
import json
import sys


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


def fmt_delta(before, after):
    """Format the before -> after coverage delta for one file or total."""
    if before is None or after is None:
        return "new file" if before is None and after is not None else "n/a"
    delta = after - before
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.1f}%"


def file_pct(cov, filename):
    """Look up a single file's coverage percentage in a JSON report."""
    if cov is None:
        return None
    entry = cov.get("files", {}).get(filename)
    return entry["summary"]["percent_covered"] if entry else None


def main():
    """Write the coverage comment body from head/base JSON reports."""
    changed = [line for line in sys.argv[1].splitlines() if line]
    head = load("coverage_head.json")
    base = load("coverage_base.json")

    lines = ["### Coverage report", "", "**Changed files**", ""]
    if changed:
        lines.append("| File | Before | After | Delta |")
        lines.append("|---|---|---|---|")
        for filename in changed:
            before = file_pct(base, filename)
            after = file_pct(head, filename)
            lines.append(
                f"| `{filename}` | {fmt_pct(before)} | {fmt_pct(after)} | "
                f"{fmt_delta(before, after)} |"
            )
    else:
        lines.append("No opds_abs/*.py files changed in this PR.")

    lines.append("")
    lines.append("**Overall**")
    before_total = base["totals"]["percent_covered"] if base else None
    after_total = head["totals"]["percent_covered"] if head else None
    if before_total is None or after_total is None:
        lines.append(f"{fmt_pct(before_total)} -> {fmt_pct(after_total)}")
    else:
        lines.append(
            f"{fmt_pct(before_total)} -> {fmt_pct(after_total)} "
            f"({fmt_delta(before_total, after_total)})"
        )

    with open("coverage_comment.md", "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
