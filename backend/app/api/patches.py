"""Patch proposal endpoints: review, approve, reject, verify."""

from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import ApprovalStatus, PatchProposal, VerificationOutcome
from app.schemas import DecisionRequest, PatchResponse, VerificationResponse
from app.services import orchestrator, store
from .findings import patch_to_response
from .deps import require_auth, resolve_patch

router = APIRouter(tags=["patches"])

# Serialises verification so two concurrent requests cannot race on the same
# isolated workspace or double-count test runs.
_verify_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(patch_id: int) -> threading.Lock:
    with _locks_guard:
        return _verify_locks.setdefault(patch_id, threading.Lock())


@router.get("/{patch_id}", response_model=PatchResponse, summary="One patch proposal")
def get_patch(
    patch_id: str,
    session: Session = Depends(get_db),
    _: None = Depends(require_auth),
) -> PatchResponse:
    return patch_to_response(resolve_patch(session, patch_id))


@router.post("/{patch_id}/approve", response_model=PatchResponse, summary="Approve a patch proposal")
def approve_patch(
    patch_id: str,
    body: DecisionRequest | None = None,
    session: Session = Depends(get_db),
    _: None = Depends(require_auth),
) -> PatchResponse:
    """Record explicit human approval.

    Approval authorises execution **inside an isolated audit workspace only**.
    It never authorises pushing to a branch, merging a merge request, or
    deploying. This service has no such capability at all.
    """
    patch = resolve_patch(session, patch_id)
    body = body or DecisionRequest()
    if patch.approval_status == ApprovalStatus.REJECTED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{patch.public_id} was rejected and cannot be approved. "
                "Generate a new proposal instead."
            ),
        )
    try:
        orchestrator.AuditOrchestrator(get_settings()).approve_patch(
            patch.id, decided_by=body.decided_by, note=body.note
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    session.refresh(patch)
    return patch_to_response(patch)


@router.post("/{patch_id}/reject", response_model=PatchResponse, summary="Reject a patch proposal")
def reject_patch(
    patch_id: str,
    body: DecisionRequest | None = None,
    session: Session = Depends(get_db),
    _: None = Depends(require_auth),
) -> PatchResponse:
    patch = resolve_patch(session, patch_id)
    body = body or DecisionRequest()
    if patch.approval_status == ApprovalStatus.APPROVED and patch.verification_outcome not in (
        VerificationOutcome.NOT_ATTEMPTED,
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"{patch.public_id} has already been verified "
                f"({patch.verification_outcome.value}); rejecting it now would misrepresent "
                "the recorded history. The rejection is not applied."
            ),
        )
    try:
        orchestrator.AuditOrchestrator(get_settings()).reject_patch(
            patch.id, decided_by=body.decided_by, note=body.note
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    session.refresh(patch)
    return patch_to_response(patch)


@router.post("/{patch_id}/verify", response_model=VerificationResponse, summary="Verify an approved patch")
def verify_patch(
    patch_id: str,
    background: bool = Query(
        default=False,
        description="Run verification asynchronously instead of waiting for the result.",
    ),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: None = Depends(require_auth),
) -> Any:
    """Execute the full verification chain for an APPROVED patch.

    Steps: isolated workspace at the exact original revision -> apply the patch
    -> run the reproduction test -> run the repository regression suite ->
    compare against the pre-patch baseline -> persist every real result.

    Refuses with HTTP 409 unless the patch is approved: a generated patch is
    never treated as a fix without a human decision.
    """
    patch = resolve_patch(session, patch_id)
    if patch.approval_status != ApprovalStatus.APPROVED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Patch {patch.public_id} is '{patch.approval_status.value}'. Explicit human "
                "approval is required before verification: POST /api/patches/{id}/approve."
            ),
        )
    lock = _lock_for(patch.id)
    if not lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=f"Verification for {patch.public_id} is already in progress.",
        )
    lock.release()

    if background:
        background_tasks.add_task(_verify_in_background, patch.id, lock)
        return VerificationResponse(
            patch_id=patch.id,
            outcome=VerificationOutcome.NOT_ATTEMPTED.value,
            summary=(
                "Verification queued in the background. Poll "
                f"GET /api/audits/{patch.audit_id}/events for progress."
            ),
            reasons=[],
            remaining_concerns=[],
            intended_tests_executed=False,
            regressions=[],
            runs=[],
        )

    with lock:
        try:
            result = orchestrator.AuditOrchestrator(settings).verify_patch(patch.id)
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return VerificationResponse(
        patch_id=patch.id,
        outcome=result["outcome"],
        summary=result["summary"],
        reasons=result["reasons"],
        remaining_concerns=result["remaining_concerns"],
        intended_tests_executed=result["intended_tests_executed"],
        regressions=result["regressions"],
        runs=result["runs"],
        # The scope statement is the engine's own disclaimer about what the
        # verification does NOT prove. Dropping it silently (the schema default
        # is "") would let the API look more confident than the engine is.
        scope_statement=result.get("scope_statement", ""),
    )


def _verify_in_background(patch_id: int, lock: threading.Lock) -> None:
    settings = get_settings()
    with lock:
        from app.db import session_scope

        try:
            orchestrator.AuditOrchestrator(settings).verify_patch(patch_id)
        except Exception as exc:  # noqa: BLE001 - must be recorded, not swallowed
            with session_scope() as session:
                patch = session.get(PatchProposal, patch_id)
                if patch is not None:
                    store.record_event(
                        session,
                        patch.audit_id,
                        store.AgentRole.VERIFICATION_AGENT,
                        store.EventType.STAGE_FAILED,
                        f"Background verification failed: {type(exc).__name__}: {exc}",
                        {"patch_id": patch_id, "exception": type(exc).__name__},
                        status="error",
                    )


__all__ = ["router"]
