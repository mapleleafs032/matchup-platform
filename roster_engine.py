"""
Fails (exit 1) if any commit range modifies or deletes existing lines in APPEND_ONLY_PATHS.
Used by .github/workflows/ci.yml and can be run locally: python -m pipeline.ci_immutability origin/main HEAD
Header-only changes (first line) are allowed when a CSV gains new columns.
"""
from __future__ import annotations
import subprocess
import sys

import config


def main(base: str, head: str) -> int:
    paths = config.APPEND_ONLY_PATHS
    out = subprocess.run(["git", "diff", "--numstat", base, head, "--", *paths], capture_output=True, text=True, check=True).stdout
    violations = []
    for line in out.strip().splitlines():
        added, deleted, path = line.split("\t")
        if deleted == "-":  # binary; snapshots dir holds JSON so shouldn't happen
            violations.append(f"{path}: binary change in append-only path")
            continue
        if int(deleted) > 0:
            # allow header rewrite: exactly one deleted line that is the file's first line
            diff = subprocess.run(["git", "diff", "-U0", base, head, "--", path], capture_output=True, text=True).stdout
            removed = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
            if len(removed) == 1 and "@@ -1," in diff:
                continue
            violations.append(f"{path}: {deleted} line(s) removed/modified")
    if violations:
        print("IMMUTABILITY VIOLATION:\n  " + "\n  ".join(violations))
        return 1
    print("append-only paths clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "HEAD~1", sys.argv[2] if len(sys.argv) > 2 else "HEAD"))
