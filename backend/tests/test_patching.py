"""Patch proposal containment: nothing is applied outside a workspace."""

from __future__ import annotations

import pytest

from app.services.patching import (
    PatchSafetyError,
    apply_to_workspace,
    propose_from_draft,
    validate_patch_target,
)
from test_analysis_engine import DOC, DEFECT, PATH, bundle

from app.services.analysis.engine import analyze


def _arith_draft():
    result = analyze(bundle(DOC, DEFECT))
    return next(d for d in result.findings if d.rule_id == "R-ARITH-001")


def test_proposed_patch_carries_both_full_contents() -> None:
    draft = _arith_draft()
    patch = propose_from_draft(draft, DEFECT, all_sources={PATH: DEFECT})
    assert patch is not None
    assert patch.original_full_content == DEFECT
    assert patch.proposed_full_content != DEFECT
    assert "price * (1 - discount_rate)" in patch.proposed_full_content
    assert patch.minimal is True


def test_patch_dict_does_not_carry_full_sources() -> None:
    draft = _arith_draft()
    patch = propose_from_draft(draft, DEFECT, all_sources={PATH: DEFECT})
    assert patch is not None
    payload = patch.to_dict()
    assert DEFECT not in repr(payload.values())
    assert payload["original_full_content_chars"] == len(DEFECT)


def test_apply_writes_only_into_workspace(tmp_path) -> None:
    draft = _arith_draft()
    patch = propose_from_draft(draft, DEFECT, all_sources={PATH: DEFECT})
    assert patch is not None
    target = tmp_path / PATH
    target.parent.mkdir(parents=True)
    target.write_text(DEFECT, encoding="utf-8")

    info = apply_to_workspace(
        tmp_path,
        PATH,
        proposed_full_content=patch.proposed_full_content,
        original_full_content=patch.original_full_content,
    )
    assert info
    assert "price * (1 - discount_rate)" in target.read_text(encoding="utf-8")


def test_apply_refuses_when_workspace_drifted(tmp_path) -> None:
    draft = _arith_draft()
    patch = propose_from_draft(draft, DEFECT, all_sources={PATH: DEFECT})
    assert patch is not None
    target = tmp_path / PATH
    target.parent.mkdir(parents=True)
    target.write_text(DEFECT + "\n# someone edited the workspace\n", encoding="utf-8")
    info = apply_to_workspace(
        tmp_path,
        PATH,
        proposed_full_content=patch.proposed_full_content,
        original_full_content=patch.original_full_content,
    )
    # Drift is refused and reported, never silently applied on a wrong base.
    assert info["applied"] is False
    assert "does not match" in info["reason"]
    assert target.read_text(encoding="utf-8").endswith("# someone edited the workspace\n")


@pytest.mark.parametrize("rel", ["../escape.py", "/etc/passwd", "a/../../b.py"])
def test_path_escape_is_refused(tmp_path, rel: str) -> None:
    with pytest.raises(PatchSafetyError):
        validate_patch_target(tmp_path, rel)


def test_unjustifiable_draft_gets_no_patch() -> None:
    draft = _arith_draft()
    draft.repair_hint = None
    assert propose_from_draft(draft, DEFECT) is None
