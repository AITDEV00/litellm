#!/usr/bin/env python3
"""Post-merge content-reversion audit for the OICM fork.

The Step 5 deletion audit catches files a merge removed. It cannot catch a
file the merge *kept* while reverting its content to upstream's version,
which is how whole-side conflict resolution loses custom fixes without any
git-visible event. This gate compares, for every file our branch had edited
since the merge base, our blob against the merge result's blob and reports:

- Class A: files upstream never touched since the base whose result content
  is exactly upstream's/base's. Git's auto-merge provably keeps our side for
  these, so any hit means an explicit checkout of the upstream side happened
  and a custom edit was reverted. There is no legitimate Class A hit: taking
  "theirs" for a file whose upstream content equals the base is a plain
  revert of our work and belongs in a visible commit, not a merge resolution.
  Exit status is 1 while any Class A hit is unhandled.

- Class B: files both sides edited whose result content is exactly
  upstream's AND which lost at least one of our lines. Whole-side resolution
  of a genuinely conflicted file can be correct (superseded custom work),
  so these are a mandatory review list, not an automatic failure: every
  entry must be classified in the merge record as restored, superseded
  (with the superseding symbol), or moved by an upstream rename.

Paths are the audit unit, so an upstream rename followed by a takeover is
invisible here (v1.106's tests/test_litellm -> tests/unit move). Run the
audit from both path spellings when a restructure landed between the tags.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Final

_SCAN_ROOTS: Final = ("litellm/", "tests/")


def _git(repo: Path, *args: str) -> str:
    result: Final = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout


def _blob_or_none(repo: Path, rev: str, path: str) -> str | None:
    result: Final = subprocess.run(
        ["git", "rev-parse", "--quiet", "--verify", f"{rev}:{path}"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _tracked_paths(repo: Path, rev: str) -> frozenset[str]:
    listing: Final = _git(repo, "ls-tree", "-r", "--name-only", rev).splitlines()
    return frozenset(p for p in listing if p.startswith(_SCAN_ROOTS))


def _lost_line_count(repo: Path, ours: str, result: str, path: str) -> int:
    diff: Final = _git(repo, "diff", f"{ours}..{result}", "--", path)
    removed: Final = tuple(line for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    return len(removed)


def _resolve_rev(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", ref).strip()


def audit(repo: Path, base_ref: str, ours_ref: str, theirs_ref: str, result_ref: str) -> tuple[int, int]:
    base: Final = _resolve_rev(repo, base_ref)
    ours: Final = _resolve_rev(repo, ours_ref)
    theirs: Final = _resolve_rev(repo, theirs_ref)
    result: Final = _resolve_rev(repo, result_ref)

    candidates: Final = _tracked_paths(repo, ours) & _tracked_paths(repo, result)

    class_a: Final[list[str]] = []
    class_b: Final[list[tuple[int, str]]] = []
    for path in sorted(candidates):
        base_blob: Final = _blob_or_none(repo, base, path)
        ours_blob: Final = _blob_or_none(repo, ours, path)
        theirs_blob: Final = _blob_or_none(repo, theirs, path)
        result_blob: Final = _blob_or_none(repo, result, path)
        if base_blob is None or ours_blob is None or result_blob is None:
            continue
        if ours_blob == base_blob:
            continue
        if theirs_blob == base_blob:
            if result_blob == theirs_blob and result_blob != ours_blob:
                class_a.append(path)
            continue
        if result_blob == theirs_blob and _lost_line_count(repo, ours, result, path) > 0:
            class_b.append((_lost_line_count(repo, ours, result, path), path))

    if class_a:
        print("CLASS A: non-conflicted files reverted to upstream content (git would have kept ours):")
        for path in class_a:
            print(f"  {path}")
    else:
        print("CLASS A: none (no silent revert of non-conflicted files).")

    if class_b:
        print("CLASS B: both-sides-edited files resolved wholesale to upstream with our lines lost (classify each):")
        for lost, path in sorted(class_b, reverse=True):
            print(f"  {lost:5d} lines lost  {path}")
    else:
        print("CLASS B: none (no wholesale upstream takeover with custom lines lost).")

    print(
        f"\nsummary: {len(class_a)} class A, {len(class_b)} class B. "
        "Class A is a hard failure; class B needs per-file classification in the merge record."
    )
    return len(class_a), len(class_b)


def main() -> int:
    parser: Final = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="merge-base ref between the custom branch and the upstream tag")
    parser.add_argument("--ours", required=True, help="pre-merge custom tip (merge commit's first parent)")
    parser.add_argument("--theirs", required=True, help="upstream tag merged in (merge commit's second parent)")
    parser.add_argument("--result", required=True, help="post-merge ref to audit (the merge commit, or HEAD later)")
    parser.add_argument("--repo", default=".", type=Path, help="repository root")
    args: Final = parser.parse_args()

    class_a_count, _ = audit(args.repo, args.base, args.ours, args.theirs, args.result)
    if class_a_count:
        print("\nFAIL: restore the class A files or revert them in a visible commit before pushing the merge.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
