#!/usr/bin/env python3
"""Rebuild the ``discount-service`` demonstration repository.

Why this exists
---------------
The auditor audits a *merge request*: it needs two real revisions of a real git
repository, where the source branch introduces a defect that the target branch
does not have. That history is a generated artefact, not committed source, so a
fresh clone has no ``.git`` here and the demo cannot run. This script recreates
that history deterministically from the committed working-tree files.

It is deliberately boring and reproducible:

* Fixed author, committer and timestamps, so the resulting commit SHAs are
  identical on every machine. Evaluation cases and documentation can therefore
  refer to real SHAs instead of "whatever the last run happened to produce".
* The defect is expressed as an explicit, exact-match substitution. If the
  target text is not found exactly once the script aborts rather than silently
  producing a repository that does not demonstrate anything.
* It never touches anything outside this directory.

Resulting history
-----------------
``main``                      correct implementation, full regression suite (checked out)
``feature/refactor-discount`` same tree with ONE planted defect

The working tree is deliberately left CLEAN on ``main``, matching the committed
fixture files. The auditor never reads the working tree: it materialises each
revision from git objects (``git archive``/``git show``) into an isolated
workspace. Keeping the tree clean means bootstrapping the demo cannot leave
phantom modifications in the parent repository.

Usage
-----
    python3 examples/discount-service/make_demo_repo.py            # build in place
    python3 examples/discount-service/make_demo_repo.py --force    # replace existing .git
    python3 examples/discount-service/make_demo_repo.py --dest /tmp/x  # build elsewhere
    python3 examples/discount-service/make_demo_repo.py --check     # verify, do not write
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent

MAIN_BRANCH = "main"
FEATURE_BRANCH = "feature/refactor-discount"

# Fixed identity and timestamps -> identical SHAs on every run, everywhere.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Demo Author",
    "GIT_AUTHOR_EMAIL": "demo@example.invalid",
    "GIT_COMMITTER_NAME": "Demo Author",
    "GIT_COMMITTER_EMAIL": "demo@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-15T09:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-15T09:00:00+00:00",
}
FEATURE_ENV = {
    **GIT_ENV,
    "GIT_AUTHOR_DATE": "2026-01-20T14:30:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-20T14:30:00+00:00",
}

MAIN_COMMIT_MESSAGE = "feat(pricing): correct discount contract with regression suite"
FEATURE_COMMIT_MESSAGE = "refactor(pricing): simplify discount calculation"


@dataclass(frozen=True)
class Defect:
    """One planted defect, expressed as an exact single-occurrence substitution."""

    path: str
    find: str
    replace: str
    summary: str


# The single defect the whole demonstration hinges on. `apply_discount` is
# documented to return the FINAL price payable, but the refactor returns the
# discount AMOUNT instead. Every pre-existing test still passes on the feature
# branch, which is exactly why an evidence-based audit is needed: a green suite
# does not demonstrate that the change is correct.
DEFECTS = (
    Defect(
        path="src/discount_service/pricing.py",
        find="    return price * (1 - discount_rate)\n",
        replace=(
            "    # simplified: discount is applied directly to the price\n"
            "    return price * discount_rate\n"
        ),
        summary="apply_discount returns the adjustment instead of the net result",
    ),
)

# Files that make up the committed fixture. Anything not listed is ignored, so
# build artefacts (__pycache__, .pytest_cache) can never leak into the demo repo.
TRACKED_FILES = (
    "README.md",
    "pyproject.toml",
    "src/discount_service/__init__.py",
    "src/discount_service/pricing.py",
    "tests/test_pricing_existing.py",
)


class FixtureError(RuntimeError):
    """Raised when the fixture cannot be built correctly. Never guessed around."""


def _run(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    full_env = {**os.environ, **(env or {})}
    # Never let a developer's global git config change the demo's identity or
    # signing behaviour, which would make the SHAs machine-dependent.
    full_env["GIT_CONFIG_GLOBAL"] = os.devnull
    full_env["GIT_CONFIG_SYSTEM"] = os.devnull
    full_env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(
        args, cwd=str(cwd), env=full_env, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise FixtureError(
            f"{' '.join(args)} failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    return proc.stdout.strip()


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return _run(["git", *args], repo, env)


def _apply_defects(dest: Path) -> None:
    for defect in DEFECTS:
        target = dest / defect.path
        if not target.is_file():
            raise FixtureError(f"defect target missing: {target}")
        original = target.read_text(encoding="utf-8")
        occurrences = original.count(defect.find)
        if occurrences != 1:
            raise FixtureError(
                f"expected the defect anchor in {defect.path} exactly once, found "
                f"{occurrences} time(s). The committed fixture has drifted from "
                "this script; fix one or the other rather than relaxing the check."
            )
        target.write_text(original.replace(defect.find, defect.replace, 1), encoding="utf-8")


def _preflight() -> None:
    """Validate the committed fixture before anything is created or deleted.

    A failed run must never leave a half-built repository behind, so every
    precondition is checked up front: all tracked files present, every defect
    anchor present exactly once, and no defect already applied.
    """
    missing = [rel for rel in TRACKED_FILES if not (HERE / rel).is_file()]
    if missing:
        raise FixtureError(
            "committed fixture file(s) missing: " + ", ".join(missing)
        )
    for defect in DEFECTS:
        original = (HERE / defect.path).read_text(encoding="utf-8")
        occurrences = original.count(defect.find)
        if occurrences != 1:
            already = original.count(defect.replace)
            hint = (
                " The defect appears to be ALREADY APPLIED to the working tree. "
                "The committed fixture must hold the CORRECT implementation; this "
                "script plants the defect. Restore the file from version control "
                "and re-run."
                if already
                else " The committed fixture has drifted from this script; fix one "
                "or the other rather than relaxing this check."
            )
            raise FixtureError(
                f"expected the defect anchor in {defect.path} exactly once, found "
                f"{occurrences} time(s).{hint}"
            )


def _copy_fixture(dest: Path) -> None:
    for rel in TRACKED_FILES:
        src = HERE / rel
        if not src.is_file():
            raise FixtureError(f"committed fixture file missing: {src}")
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)


def build(dest: Path, *, force: bool = False) -> dict[str, str]:
    """Create the two-branch demonstration repository at ``dest``."""
    _preflight()
    dest = dest.resolve()
    git_dir = dest / ".git"
    if git_dir.exists():
        if not force:
            raise FixtureError(
                f"{git_dir} already exists. Re-run with --force to rebuild it."
            )
        shutil.rmtree(git_dir)
    if dest == HERE:
        # Rebuilding in place: stage a pristine copy of the tracked files first so
        # the working tree cannot carry a previous run's defect into `main`.
        pristine = dest.parent / f".{dest.name}.pristine"
        if pristine.exists():
            shutil.rmtree(pristine)
        pristine.mkdir(parents=True)
        _copy_fixture(pristine)
        _run(["git", "init", "-q", "-b", MAIN_BRANCH, str(dest)], dest.parent, GIT_ENV)
        for rel in TRACKED_FILES:
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pristine / rel, out)
        shutil.rmtree(pristine)
    else:
        dest.mkdir(parents=True, exist_ok=True)
        _copy_fixture(dest)
        _run(["git", "init", "-q", "-b", MAIN_BRANCH, "."], dest, GIT_ENV)

    _git(dest, "add", "--", *TRACKED_FILES, env=GIT_ENV)
    _git(dest, "-c", "commit.gpgsign=false", "commit", "-q", "-m", MAIN_COMMIT_MESSAGE, env=GIT_ENV)
    main_sha = _git(dest, "rev-parse", "HEAD", env=GIT_ENV)

    _git(dest, "checkout", "-q", "-b", FEATURE_BRANCH, env=GIT_ENV)
    _apply_defects(dest)
    _git(dest, "add", "--", *TRACKED_FILES, env=FEATURE_ENV)
    _git(
        dest, "-c", "commit.gpgsign=false", "commit", "-q", "-m", FEATURE_COMMIT_MESSAGE,
        env=FEATURE_ENV,
    )
    feature_sha = _git(dest, "rev-parse", "HEAD", env=FEATURE_ENV)

    # Leave the working tree clean on `main`. The revision under audit is read
    # from git objects, so the checked-out branch is irrelevant to the audit --
    # and a clean tree keeps the parent repository free of phantom diffs.
    _git(dest, "checkout", "-q", MAIN_BRANCH, env=GIT_ENV)
    return {"main": main_sha, "feature": feature_sha}


def check(dest: Path) -> list[str]:
    """Verify an existing repository really demonstrates the defect. Read-only."""
    problems: list[str] = []
    dest = dest.resolve()
    if not (dest / ".git").is_dir():
        return [f"{dest} is not a git repository - run make_demo_repo.py"]
    try:
        branches = set(_git(dest, "branch", "--format=%(refname:short)").splitlines())
        for required in (MAIN_BRANCH, FEATURE_BRANCH):
            if required not in branches:
                problems.append(f"missing branch '{required}'")
        if problems:
            return problems
        main_src = _git(dest, "show", f"{MAIN_BRANCH}:src/discount_service/pricing.py")
        feat_src = _git(dest, "show", f"{FEATURE_BRANCH}:src/discount_service/pricing.py")
        if "price * (1 - discount_rate)" not in main_src:
            problems.append(f"'{MAIN_BRANCH}' does not contain the correct implementation")
        if "return price * discount_rate" not in feat_src:
            problems.append(f"'{FEATURE_BRANCH}' does not contain the planted defect")
        if main_src == feat_src:
            problems.append("both revisions are identical - nothing to audit")
        head = _git(dest, "rev-parse", "--abbrev-ref", "HEAD")
        if head != MAIN_BRANCH:
            problems.append(f"HEAD is on '{head}', expected a clean '{MAIN_BRANCH}' checkout")
        dirty = _git(dest, "status", "--porcelain", "--", *TRACKED_FILES)
        if dirty:
            problems.append(
                "tracked fixture files are modified in the working tree: "
                + ", ".join(line.split()[-1] for line in dirty.splitlines())
            )
    except FixtureError as exc:
        problems.append(str(exc))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, default=HERE,
                        help="where to build the repository (default: this directory)")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing .git directory")
    parser.add_argument("--check", action="store_true",
                        help="verify an existing repository instead of building one")
    args = parser.parse_args(argv)

    try:
        if args.check:
            problems = check(args.dest)
            if problems:
                print("DEMO FIXTURE CHECK FAILED")
                for p in problems:
                    print(f"  - {p}")
                return 1
            print(f"DEMO FIXTURE OK: {args.dest}")
            print(f"  {MAIN_BRANCH} = {_git(args.dest, 'rev-parse', '--short', MAIN_BRANCH)}")
            print(f"  {FEATURE_BRANCH} = {_git(args.dest, 'rev-parse', '--short', FEATURE_BRANCH)}")
            return 0

        shas = build(args.dest, force=args.force)
    except FixtureError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Built demonstration repository at {args.dest}")
    print(f"  {MAIN_BRANCH:<24} {shas['main']}  (correct)")
    print(f"  {FEATURE_BRANCH:<24} {shas['feature']}  (defective)")
    for defect in DEFECTS:
        print(f"  planted defect: {defect.path} - {defect.summary}")
    print(f"  working tree left clean on '{MAIN_BRANCH}'")
    print("\nThe pre-existing regression suite still passes on the defective branch.")
    print("That is the point: a green suite does not demonstrate the change is correct.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
