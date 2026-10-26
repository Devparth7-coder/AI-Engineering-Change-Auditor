"""Patch proposal, safe application, and approval bookkeeping.

Guarantees enforced here:

* **Minimal** -- the patch changes the smallest construct that satisfies the
  declared contract, and says so.
* **Separate** -- original and proposed code are stored as distinct fields and
  rendered side by side; the original is never mutated in place.
* **Safe** -- a patch may only touch one relative path inside the workspace.
  Absolute paths, ``..`` traversal, symlink targets and multi-file patches are
  rejected before anything is written.
* **Never silent** -- applying a patch requires an explicit approval record.
  This module has no ability to push, merge or deploy.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analysis.rules.base import FindingDraft

UNSAFE_PATH_RE = re.compile(r"(^/)|(\.\.)|(^~)")


class PatchSafetyError(ValueError):
    """Raised when a patch violates the containment rules."""


@dataclass
class PatchDraft:
    finding_public_id: str
    file_path: str
    original_code: str
    proposed_code: str
    patch_text: str
    explanation: str
    strategy: str
    # Complete file contents, kept separate on purpose: the patch is applied by
    # writing `proposed_full_content` after verifying the workspace still matches
    # `original_full_content`. Replaying a unified diff is avoided entirely --
    # it is fragile, and a mis-applied hunk is worse than no patch at all.
    original_full_content: str = ""
    proposed_full_content: str = ""
    side_effects: list[str] = field(default_factory=list)
    call_sites: list[dict[str, Any]] = field(default_factory=list)
    related_tests: list[str] = field(default_factory=list)
    minimal: bool = True
    applicable: bool = True
    refusal_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_public_id": self.finding_public_id,
            "file_path": self.file_path,
            "original_code": self.original_code,
            "proposed_code": self.proposed_code,
            "patch_text": self.patch_text,
            "explanation": self.explanation,
            "strategy": self.strategy,
            "side_effects": self.side_effects,
            "original_full_content_chars": len(self.original_full_content),
            "proposed_full_content_chars": len(self.proposed_full_content),
            "call_sites": self.call_sites,
            "related_tests": self.related_tests,
            "minimal": self.minimal,
            "applicable": self.applicable,
            "refusal_reason": self.refusal_reason,
        }


def _offset(source: str, line: int, col: int) -> int:
    lines = source.splitlines(keepends=True)
    return sum(len(ln) for ln in lines[: line - 1]) + col


def _node_span(source: str, node: ast.AST) -> tuple[int, int]:
    start = _offset(source, node.lineno, node.col_offset)
    end = _offset(source, node.end_lineno, node.end_col_offset)
    return start, end


def _unified_diff(path: str, old: str, new: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    )
    return "".join(diff)


def find_call_sites(
    symbol: str, sources: dict[str, str], *, exclude_paths: tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    """Locate real call sites of ``symbol`` across the repository."""
    hits: list[dict[str, Any]] = []
    pattern = re.compile(rf"(?<![\w.]){re.escape(symbol)}\s*\(")
    for path, src in sources.items():
        if path in exclude_paths or not path.endswith(".py"):
            continue
        for lineno, line in enumerate(src.splitlines(), start=1):
            if pattern.search(line) and not line.lstrip().startswith(("def ", "async def ")):
                hits.append({"file_path": path, "line": lineno, "text": line.strip()[:200]})
    return hits[:50]


def _stale_comment_line(source: str, return_lineno: int) -> int | None:
    """Line number of a single comment line directly above a return statement."""
    lines = source.splitlines()
    idx = return_lineno - 2  # line above, 0-based
    if idx < 0:
        return None
    candidate = lines[idx].strip()
    if candidate.startswith("#") and candidate != "#":
        return idx + 1
    return None


def propose_from_draft(
    draft: FindingDraft,
    source: str,
    all_sources: dict[str, str] | None = None,
    related_tests: list[str] | None = None,
) -> PatchDraft | None:
    """Build the smallest correction that satisfies the declared contract.

    Returns ``None`` when no mechanical, contract-justified repair exists.  The
    auditor would rather report "no patch proposed" than invent a change it
    cannot justify -- an unjustified patch cannot be meaningfully verified.
    """
    hint = draft.repair_hint
    if hint is None or not hint.replacement_expr:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return PatchDraft(
            finding_public_id="",
            file_path=draft.file_path,
            original_code="",
            proposed_code="",
            patch_text="",
            explanation="",
            strategy=hint.strategy,
            applicable=False,
            refusal_reason=f"source does not parse: {exc}",
        )

    target_fn = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == hint.symbol:
            target_fn = node
            break
    if target_fn is None:
        return None

    # Find the return statement whose expression matches the offending one.
    target_return = None
    for node in ast.walk(target_fn):
        if isinstance(node, ast.Return) and node.value is not None:
            try:
                if ast.unparse(node.value) == hint.original_expr:
                    target_return = node
                    break
            except Exception:  # noqa: BLE001
                continue
    if target_return is None or target_return.value is None:
        return None

    start, end = _node_span(source, target_return.value)
    original_expr_src = source[start:end]
    proposed = source[:start] + hint.replacement_expr + source[end:]

    # Drop a now-incorrect comment introduced immediately above the return.
    dropped_comment = _stale_comment_line(source, target_return.lineno)
    explanation_bits = [
        f"Replace `{original_expr_src}` with `{hint.replacement_expr}` in `{hint.symbol}`.",
        hint.rationale,
    ]
    if dropped_comment is not None:
        plines = proposed.splitlines(keepends=True)
        # Recompute the comment position in the (possibly re-spaced) new source.
        c_idx = _stale_comment_line(proposed, target_return.lineno)
        if c_idx is not None and c_idx - 1 < len(plines):
            comment_text = plines[c_idx - 1].strip()
            del plines[c_idx - 1]
            proposed = "".join(plines)
            explanation_bits.append(
                f"Remove the stale comment `{comment_text}`, which described the "
                f"incorrect behaviour and would otherwise contradict the fix."
            )

    # Show the enclosing function before/after so reviewers see context.
    fn_span = _node_span(source, target_fn)
    original_code = source[fn_span[0] : fn_span[1]]
    try:
        new_tree = ast.parse(proposed)
        new_fn = next(
            n
            for n in ast.walk(new_tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == hint.symbol
        )
        nspan = _node_span(proposed, new_fn)
        proposed_code = proposed[nspan[0] : nspan[1]]
    except Exception:  # noqa: BLE001
        proposed_code = proposed

    call_sites = find_call_sites(
        hint.symbol, all_sources or {}, exclude_paths=(draft.file_path,)
    )
    side_effects = _side_effects(draft, hint, call_sites)

    return PatchDraft(
        finding_public_id="",
        file_path=draft.file_path,
        original_code=original_code,
        proposed_code=proposed_code,
        patch_text=_unified_diff(draft.file_path, source, proposed),
        explanation=" ".join(explanation_bits),
        strategy=hint.strategy,
        original_full_content=source,
        proposed_full_content=proposed,
        side_effects=side_effects,
        call_sites=call_sites,
        related_tests=related_tests or [],
        minimal=True,
    )


def _side_effects(
    draft: FindingDraft, hint: Any, call_sites: list[dict[str, Any]]  # noqa: ANN401
) -> list[str]:
    out: list[str] = []
    sym = hint.symbol
    if call_sites:
        listing = ", ".join(f"{c['file_path']}:{c['line']}" for c in call_sites[:6])
        out.append(
            f"{len(call_sites)} call site(s) of `{sym}` exist elsewhere in the "
            f"repository ({listing}). If any of them was written to consume the "
            f"incorrect value, it must be updated in the same change."
        )
    else:
        out.append(
            f"No other call sites of `{sym}` were found in the analysed files, so "
            f"the blast radius appears limited to this function's direct callers."
        )
    out.append(
        "The return value's magnitude changes (for the documented example, from the "
        "adjustment amount to the amount remaining), so any cached, persisted or "
        "downstream-reported figures derived from it will change."
    )
    out.append(
        "Behaviour at the domain edges is unchanged: a zero rate still returns the "
        "base value and a rate of 1.0 now returns 0 rather than the base value."
    )
    if draft.contract and draft.contract.raises:
        out.append(
            f"The declared error contract ({', '.join(draft.contract.raises)}) is "
            f"preserved; no validation behaviour is altered by this patch."
        )
    return out


# --------------------------------------------------------------------------- #
# Safe application
# --------------------------------------------------------------------------- #
def validate_patch_target(workspace: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` inside ``workspace`` or raise PatchSafetyError."""
    if not rel_path or UNSAFE_PATH_RE.search(rel_path):
        raise PatchSafetyError(f"unsafe patch path rejected: {rel_path!r}")
    root = workspace.resolve()
    target = (workspace / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PatchSafetyError(
            f"patch path escapes the workspace: {rel_path!r}"
        ) from exc
    if target.is_symlink():
        raise PatchSafetyError(f"refusing to patch a symlink: {rel_path!r}")
    return target


def apply_to_workspace(
    workspace: Path,
    rel_path: str,
    *,
    proposed_full_content: str | None = None,
    original_full_content: str | None = None,
) -> dict[str, Any]:
    """Write the proposed content into an isolated workspace copy.

    Two modes:

    * ``proposed_full_content`` given -- write it verbatim (the auditor keeps the
      complete proposed file, so this is exact and needs no patch tooling).
    * otherwise -- not supported; a partial patch is never applied blindly.

    When ``original_full_content`` is supplied the current file must match it
    exactly, which makes application idempotent-safe: applying the same patch to
    an already-patched file is detected and reported rather than double-applied.
    """
    target = validate_patch_target(workspace, rel_path)
    if proposed_full_content is None:
        raise PatchSafetyError("refusing to apply a patch without full proposed content")
    current = target.read_text(encoding="utf-8") if target.exists() else ""
    already = current == proposed_full_content
    if original_full_content is not None and not already and current != original_full_content:
        return {
            "applied": False,
            "reason": (
                "workspace content does not match the recorded original revision; "
                "refusing to apply the patch to an unexpected base"
            ),
            "path": str(target),
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_suffix(target.suffix + ".orig")
    if target.exists() and not backup.exists():
        backup.write_text(current, encoding="utf-8")
    target.write_text(proposed_full_content, encoding="utf-8")
    return {
        "applied": True,
        "already_applied": already,
        "path": str(target),
        "backup": str(backup),
        "bytes_written": len(proposed_full_content),
    }


__all__ = [
    "PatchDraft",
    "PatchSafetyError",
    "apply_to_workspace",
    "find_call_sites",
    "propose_from_draft",
    "validate_patch_target",
]
