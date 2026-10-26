"""Audit orchestration service.

Runs the end-to-end workflow asynchronously (an audit never depends on an HTTP
request staying open) and writes every stage transition to ``agent_events``, so
a reopened audit can be replayed from persisted records alone.

Stage order:

``fetch_context`` -> ``duo`` -> ``analyze`` -> ``investigate``
-> ``generate_tests`` -> ``propose_patches`` -> ``report``

Verification is **not** part of the automatic run: it requires an explicit human
approval first (``approve_patch``), and is then executed by ``verify_patch``.
Nothing in this module can push, merge or deploy.
"""

from __future__ import annotations

import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import gitrepo, investigate as investigate_mod, patching, sandbox, store, testgen
from .analysis.engine import ChangeBundle, analyze
from .duo import (
    DuoAgentPlatformClient,
    DuoUnavailable,
    build_additional_context,
    build_audit_goal,
)
from .gitlab_client import GitLabClient, GitLabError, GitLabNotConfigured
from .pytest_runner import classify_reproduction, run_pytest, summarise_output
from .repo_source import GitRevisionSource, StaticRevisionSource, is_test_path
from .reporting import build_report
from .verification import verify_patch as run_verification
from app.config import Settings, get_settings
from app.db import session_scope
from app.models import (
    AgentRole,
    ApprovalStatus,
    AuditSession,
    AuditStatus,
    EngineKind,
    EventType,
    Finding,
    IsolationLevel,
    PatchProposal,
    Project,
    ReproOutcome,
    TestRun,
    TestRunStatus,
    TestType,
    VerificationOutcome,
    VerificationStatus,
    utcnow,
)
from app.security.sanitize import detect_injection, sanitize_value

logger = logging.getLogger(__name__)

MAX_LIVE_FILES = 400
_executor: ThreadPoolExecutor | None = None
_running: set[int] = set()
_lock = threading.Lock()


@dataclass
class AuditContext:
    bundle: ChangeBundle
    source: Any
    mode: str
    project_path: str
    changed_paths: list[str] = field(default_factory=list)
    injection_flags: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    duo_result: dict[str, Any] = field(default_factory=dict)


def get_executor(settings: Settings | None = None) -> ThreadPoolExecutor:
    global _executor
    settings = settings or get_settings()
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=max(1, settings.audit_worker_concurrency),
            thread_name_prefix="audit-worker",
        )
    return _executor


# --------------------------------------------------------------------------- #
# Context building
# --------------------------------------------------------------------------- #
def _scan_injection(sources: dict[str, str]) -> dict[str, list[str]]:
    """Flag repository content that looks like a prompt-injection attempt."""
    flags: dict[str, list[str]] = {}
    for path, content in sources.items():
        hits = detect_injection(content)
        if hits:
            flags[path] = hits
    return flags


def build_local_demo_context(
    session: Session, audit: AuditSession, project: Project, settings: Settings
) -> AuditContext:
    repo = Path(project.local_path or settings.demo_repo_path)
    if not repo.is_dir():
        raise GitLabError(f"demonstration repository not found at {repo}")
    source_ref = audit.source_branch or settings.demo_source_branch
    target_ref = audit.target_branch or settings.demo_target_branch
    source_sha = gitrepo.resolve_sha(repo, source_ref)
    target_sha = gitrepo.resolve_sha(repo, target_ref)

    changed = gitrepo.changed_files(repo, target_sha, source_sha)
    old_sources = {p: gitrepo.file_at(repo, target_sha, p) for p in changed}
    new_sources = {p: gitrepo.file_at(repo, source_sha, p) for p in changed}
    all_new = GitRevisionSource(repo).all_python_files(source_sha)
    test_sources = {p: s for p, s in all_new.items() if is_test_path(p)}
    raw_diff = gitrepo.diff_between(repo, target_sha, source_sha)

    audit.source_commit_sha = source_sha
    audit.target_commit_sha = target_sha
    audit.merge_request_title = audit.merge_request_title or (
        f"[demo] {source_ref} -> {target_ref}"
    )
    session.commit()

    bundle = ChangeBundle(
        source_commit_sha=source_sha,
        target_commit_sha=target_sha,
        source_branch=source_ref,
        target_branch=target_ref,
        old_sources=old_sources,
        new_sources=new_sources,
        raw_diff=raw_diff,
        test_sources=test_sources,
        repo_root=str(repo),
        mode="local_demo",
    )
    return AuditContext(
        bundle=bundle,
        source=GitRevisionSource(repo),
        mode="local_demo",
        project_path=project.path_with_namespace or project.name,
        changed_paths=changed,
        injection_flags=_scan_injection(all_new),
        notes=[
            "DEMONSTRATION MODE: the change was read from the bundled example "
            "repository over local git. No GitLab API and no Duo Agent Platform "
            "call was involved in producing this context."
        ],
    )


def build_live_context(
    session: Session,
    audit: AuditSession,
    project: Project,
    settings: Settings,
    client: GitLabClient,
) -> AuditContext:
    if audit.merge_request_iid is None:
        raise GitLabError("a merge request IID is required for live audits")
    pid = project.gitlab_project_id
    if pid is None:
        raise GitLabError("project has no GitLab project id")
    client.assert_project_allowed(pid)

    mr = client.get_merge_request(pid, audit.merge_request_iid)
    changes = client.get_merge_request_changes(pid, audit.merge_request_iid)
    source_sha = mr.get("diff_head_sha") or mr.get("sha") or ""
    refs = changes.get("diff_refs") or {}
    target_sha = refs.get("base_sha") or ""
    if not source_sha:
        raise GitLabError("merge request reports no head commit SHA")
    if not target_sha:
        target_sha = gitrepo_sha_or_branch(client, pid, mr.get("target_branch") or project.default_branch)

    audit.source_branch = mr.get("source_branch", audit.source_branch)
    audit.target_branch = mr.get("target_branch", audit.target_branch)
    audit.source_commit_sha = source_sha
    audit.target_commit_sha = target_sha
    audit.merge_request_title = mr.get("title", "")
    session.commit()

    file_changes = [c for c in changes.get("changes", []) if not c.get("deleted_file")]
    paths = [c.get("new_path") for c in file_changes if c.get("new_path")]
    raw_diff = "\n".join(
        _hunk_to_diff(c) for c in file_changes if c.get("diff")
    )
    old_sources = {p: client.file_raw(pid, p, target_sha) for p in paths}
    new_sources = {p: client.file_raw(pid, p, source_sha) for p in paths}

    # Fetch enough of the tree to actually execute tests in isolation.
    tree = client.list_tree(pid, source_sha, recursive=True)
    py_paths = [t["path"] for t in tree if str(t.get("path", "")).endswith(".py")]
    truncated = len(py_paths) > MAX_LIVE_FILES
    py_paths = py_paths[:MAX_LIVE_FILES]
    files_at_source: dict[str, str] = {}
    for p in py_paths:
        files_at_source[p] = client.file_raw(pid, p, source_sha)
    test_sources = {p: s for p, s in files_at_source.items() if is_test_path(p)}

    bundle = ChangeBundle(
        source_commit_sha=source_sha,
        target_commit_sha=target_sha,
        source_branch=audit.source_branch,
        target_branch=audit.target_branch,
        old_sources=old_sources,
        new_sources=new_sources,
        raw_diff=raw_diff,
        test_sources=test_sources,
        repo_root="",
        mode="live_gitlab",
    )
    notes = [
        f"LIVE MODE: context fetched from GitLab project {pid} MR !{audit.merge_request_iid}.",
        f"Pinned revisions: source {source_sha[:12]}, target {target_sha[:12]}.",
    ]
    if truncated:
        notes.append(
            f"Repository snapshot truncated to {MAX_LIVE_FILES} Python files; "
            "test execution may be incomplete for very large repositories."
        )
    return AuditContext(
        bundle=bundle,
        source=StaticRevisionSource({source_sha: files_at_source}),
        mode="live_gitlab",
        project_path=project.path_with_namespace or project.name,
        changed_paths=paths,
        injection_flags=_scan_injection(files_at_source),
        notes=notes,
    )


def gitrepo_sha_or_branch(client: GitLabClient, pid: int, ref: str) -> str:  # noqa: ANN201
    try:
        data = client._request("GET", f"/projects/{pid}/repository/commits/{ref}")  # noqa: SLF001
        return str((data or {}).get("id", ref))
    except GitLabError:
        return ref


def _hunk_to_diff(change: dict[str, Any]) -> str:
    header = (
        f"diff --git a/{change.get('old_path')} b/{change.get('new_path')}\n"
        f"--- a/{change.get('old_path')}\n"
        f"+++ b/{change.get('new_path')}\n"
    )
    return header + (change.get("diff") or "")


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
class AuditOrchestrator:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------ run
    def run_audit(self, audit_id: int) -> None:
        """Execute the full automatic pipeline for one audit."""
        with _lock:
            if audit_id in _running:
                logger.info("audit %s already running; ignoring duplicate start", audit_id)
                return
            _running.add(audit_id)
        try:
            self._run_audit_inner(audit_id)
        except Exception as exc:  # noqa: BLE001 - never let a worker die silently
            logger.exception("audit %s failed", audit_id)
            self._mark_failed(audit_id, exc)
        finally:
            with _lock:
                _running.discard(audit_id)

    def _mark_failed(self, audit_id: int, exc: Exception) -> None:
        with session_scope() as session:
            audit = store.get_audit(session, audit_id)
            if audit is None:
                return
            detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}"
            audit.error_detail = detail[:4000]
            store.set_stage(session, audit, AuditStatus.FAILED)
            store.record_event(
                session,
                audit_id,
                AgentRole.ORCHESTRATOR,
                EventType.AUDIT_FAILED,
                f"Audit failed: {type(exc).__name__}: {exc}",
                {"exception": type(exc).__name__},
                status="error",
            )

    def _run_audit_inner(self, audit_id: int) -> None:  # noqa: C901
        settings = self.settings
        with session_scope() as session:
            audit = store.get_audit(session, audit_id)
            if audit is None:
                raise ValueError(f"audit {audit_id} does not exist")
            if audit.status in (AuditStatus.COMPLETED, AuditStatus.FAILED):
                logger.info("audit %s already terminal (%s)", audit_id, audit.status.value)
                return
            project = store.get_project(session, audit.project_id)
            if project is None:
                raise ValueError(f"project {audit.project_id} does not exist")
            audit.started_at = audit.started_at or utcnow()
            store.set_stage(session, audit, AuditStatus.FETCHING_CONTEXT)
            store.record_event(
                session, audit_id, AgentRole.ORCHESTRATOR, EventType.AUDIT_STARTED,
                f"Audit started in mode '{audit.mode}'.",
                {
                    "mode": audit.mode,
                    "project": project.path_with_namespace,
                    "merge_request_iid": audit.merge_request_iid,
                    "sandbox": sandbox.probe_isolation(settings),
                },
            )

        # ---------------- stage 1: context ----------------
        with session_scope() as session:
            audit = store.get_audit(session, audit_id)
            project = store.get_project(session, audit.project_id)
            ctx = self._stage_fetch_context(session, audit, project, settings)
            store.record_event(
                session, audit_id, AgentRole.ORCHESTRATOR, EventType.CONTEXT_FETCHED,
                f"Change context fetched: {len(ctx.changed_paths)} changed file(s), "
                f"source {audit.source_commit_sha[:12]}, target {audit.target_commit_sha[:12]}.",
                {
                    "mode": ctx.mode,
                    "changed_paths": ctx.changed_paths,
                    "source_commit_sha": audit.source_commit_sha,
                    "target_commit_sha": audit.target_commit_sha,
                    "notes": ctx.notes,
                    "injection_flags": ctx.injection_flags,
                },
            )
            if ctx.injection_flags:
                store.record_event(
                    session, audit_id, AgentRole.ORCHESTRATOR, EventType.SECURITY_NOTICE,
                    "Repository content matched prompt-injection markers. Content handed "
                    "to agents is fenced and labelled untrusted; directives inside "
                    "repository text are never followed.",
                    {"files": ctx.injection_flags},
                    status="warning",
                )

        # ---------------- stage 2: Duo Agent Platform ----------------
        with session_scope() as session:
            audit = store.get_audit(session, audit_id)
            self._stage_duo(session, audit, ctx, settings)

        # ---------------- stage 3: analyze ----------------
        with session_scope() as session:
            audit = store.get_audit(session, audit_id)
            self._stage_analyze(session, audit, ctx, settings)

        # ---------------- stage 4: investigate ----------------
        with session_scope() as session:
            audit = store.get_audit(session, audit_id)
            self._stage_investigate(session, audit, ctx)

        # ---------------- stage 5: reproduction tests ----------------
        with session_scope() as session:
            audit = store.get_audit(session, audit_id)
            self._stage_generate_tests(session, audit, ctx, settings)

        # ---------------- stage 6: patches ----------------
        with session_scope() as session:
            audit = store.get_audit(session, audit_id)
            self._stage_propose_patches(session, audit, ctx)

        # ---------------- stage 7: report ----------------
        with session_scope() as session:
            audit = store.get_audit(session, audit_id)
            self._stage_report(session, audit, ctx)

    # ------------------------------------------------------------ stages
    def _stage_fetch_context(
        self, session: Session, audit: AuditSession, project: Project, settings: Settings
    ) -> AuditContext:
        if audit.mode == "live_gitlab":
            try:
                client = GitLabClient.from_settings(settings)
            except GitLabNotConfigured as exc:
                raise GitLabError(
                    "Audit was created in live mode but GitLab is not configured: "
                    f"{exc}"
                ) from exc
            return build_live_context(session, audit, project, settings, client)
        return build_local_demo_context(session, audit, project, settings)

    def _stage_duo(
        self, session: Session, audit: AuditSession, ctx: AuditContext, settings: Settings
    ) -> None:
        store.set_stage(session, audit, AuditStatus.INVESTIGATING, stage="duo_integration")
        client = DuoAgentPlatformClient(settings)
        availability = client.probe()
        audit.duo_availability = availability.to_dict()
        session.commit()

        if not availability.available:
            audit.engine = EngineKind.LOCAL_DETERMINISTIC
            session.commit()
            store.record_event(
                session, audit.id, AgentRole.ORCHESTRATOR, EventType.DUO_UNAVAILABLE,
                "GitLab Duo Agent Platform was NOT used for this audit. "
                + availability.reason,
                availability.to_dict(),
                status="warning",
                engine=EngineKind.LOCAL_DETERMINISTIC,
            )
            return

        goal = build_audit_goal(
            project_path=ctx.project_path,
            merge_request_iid=audit.merge_request_iid,
            source_sha=audit.source_commit_sha,
            target_sha=audit.target_commit_sha,
            changed_files=ctx.changed_paths,
        )
        try:
            started = client.trigger_flow(
                project_id=audit.project.gitlab_project_id or ctx.project_path,
                goal=goal,
                merge_request_iid=audit.merge_request_iid,
                additional_context=build_additional_context(
                    f"audit-{audit.id}",
                    {
                        "source_commit_sha": audit.source_commit_sha,
                        "target_commit_sha": audit.target_commit_sha,
                        "changed_files": ctx.changed_paths,
                    },
                ),
                client_reference=f"eca-audit-{audit.id}",
            )
        except DuoUnavailable as exc:
            audit.engine = EngineKind.LOCAL_DETERMINISTIC
            session.commit()
            store.record_event(
                session, audit.id, AgentRole.ORCHESTRATOR, EventType.DUO_UNAVAILABLE,
                "The Duo Agent Platform was reachable but the flow could not be started: "
                f"{exc.reason} Missing prerequisite: {exc.prerequisite}",
                {"checks": exc.checks, "reason": exc.reason, "prerequisite": exc.prerequisite},
                status="error",
            )
            return

        response = started.get("response") or {}
        workflow_id = response.get("id")
        audit.duo_session_id = workflow_id
        audit.duo_web_url = str(response.get("web_url") or "")
        audit.engine = EngineKind.GITLAB_DUO_FLOW
        session.commit()
        store.record_event(
            session, audit.id, AgentRole.ORCHESTRATOR, EventType.DUO_FLOW_TRIGGERED,
            f"Duo flow started (session {workflow_id}, status {response.get('status')}).",
            started,
            status="success",
            engine=EngineKind.GITLAB_DUO_FLOW,
        )
        if workflow_id is None:
            ctx.duo_result = {"started": started, "polled": False}
            return

        def _on_event(ev):  # noqa: ANN001, ANN202
            with session_scope() as inner:
                store.record_event(
                    inner, audit.id, ev.agent_role, EventType.DUO_FLOW_EVENT, ev.message,
                    ev.payload, status=ev.status, engine=EngineKind.GITLAB_DUO_FLOW,
                )

        try:
            polled = client.poll_until_complete(workflow_id, on_event=_on_event)
        except DuoUnavailable as exc:
            polled = {"status": "unavailable", "reason": exc.reason}
        ctx.duo_result = {"started": started, "polled": polled}
        with session_scope() as inner:
            store.record_event(
                inner, audit.id, AgentRole.ORCHESTRATOR, EventType.DUO_FLOW_COMPLETED,
                f"Duo flow finished with status '{polled.get('status')}'.",
                sanitize_value(polled),
                status="success" if polled.get("status") == "finished" else "warning",
                engine=EngineKind.GITLAB_DUO_FLOW,
            )

    def _stage_analyze(
        self, session: Session, audit: AuditSession, ctx: AuditContext, settings: Settings
    ) -> None:
        store.set_stage(session, audit, AuditStatus.ANALYZING)
        result = analyze(ctx.bundle, max_findings=settings.max_findings_per_audit)
        store.record_event(
            session, audit.id, AgentRole.CHANGE_INVESTIGATOR, EventType.STAGE_COMPLETED,
            f"Change analysis complete: {len(result.findings)} candidate finding(s) from "
            f"{len(result.rules_run)} rule(s) over {result.files_analyzed} file(s).",
            result.to_dict(),
            engine=audit.engine,
        )
        if result.rule_errors:
            store.record_event(
                session, audit.id, AgentRole.CHANGE_INVESTIGATOR, EventType.STAGE_FAILED,
                f"{len(result.rule_errors)} rule(s) raised errors and were skipped.",
                {"rule_errors": result.rule_errors},
                status="error",
                engine=audit.engine,
            )
        for draft in result.findings:
            public_id = store.next_public_id(session, Finding, "F", audit.id)
            conf = dict(draft.confidence)
            finding = Finding(
                audit_id=audit.id,
                public_id=public_id,
                category=draft.category,
                severity=draft.severity,
                title=draft.title,
                description=draft.description,
                file_path=draft.file_path,
                line_start=draft.line_start,
                line_end=draft.line_end,
                symbol=draft.symbol,
                evidence=[e.to_dict() for e in draft.evidence],
                expected_behavior=draft.expected_behavior,
                suspected_behavior=draft.suspected_behavior,
                confidence=conf,
                verification_status=VerificationStatus.HYPOTHESIS,
                recommended_action=draft.recommended_action,
                engine=draft.engine,
                rule_id=draft.rule_id,
                facts=draft.facts,
                hypotheses=draft.hypotheses,
                missing_information=draft.missing_information,
            )
            session.add(finding)
            session.flush()
            store.record_event(
                session, audit.id, AgentRole.CHANGE_INVESTIGATOR, EventType.FINDING_CREATED,
                f"{public_id} [{draft.severity.value}] {draft.title}",
                {
                    "finding_id": finding.id,
                    "public_id": public_id,
                    "rule_id": draft.rule_id,
                    "category": draft.category.value,
                    "severity": draft.severity.value,
                    "verification_status": VerificationStatus.HYPOTHESIS.value,
                    "location": f"{draft.file_path}:{draft.line_start}-{draft.line_end}",
                    "evidence_count": len(draft.evidence),
                    "testable": draft.testable,
                },
                engine=draft.engine,
            )
            # Keep the draft on the row so later stages need not recompute it.
            _DRAFTS[(audit.id, finding.id)] = draft
        session.commit()

    def _stage_investigate(self, session: Session, audit: AuditSession, ctx: AuditContext) -> None:
        store.set_stage(session, audit, AuditStatus.INVESTIGATING)
        findings = session.scalars(select(Finding).where(Finding.audit_id == audit.id)).all()
        promoted = demoted = 0
        for finding in findings:
            draft = _DRAFTS.get((audit.id, finding.id))
            if draft is None:
                continue
            res = investigate_mod.investigate(
                draft,
                new_sources=ctx.bundle.new_sources,
                old_sources=ctx.bundle.old_sources,
                raw_diff=ctx.bundle.raw_diff,
            )
            finding.investigation = res.to_dict()
            finding.facts = res.facts
            finding.hypotheses = res.hypotheses
            finding.missing_information = res.missing_information
            conf = dict(finding.confidence or {})
            base = float(conf.get("signal_agreement_score", 0))
            conf["signal_agreement_score"] = round(
                max(0.0, min(1.0, base + res.confidence_adjustment)), 3
            )
            conf["post_investigation"] = {
                "adjustment": res.confidence_adjustment,
                "corroboration_ratio": round(res.corroboration_ratio, 3),
                "corroborated": len(res.corroborated),
                "refuted": len(res.refuted),
            }
            score = conf["signal_agreement_score"]
            conf["level"] = "high" if score >= 0.75 else "medium" if score >= 0.5 else "low"
            finding.confidence = conf
            previous = finding.verification_status
            finding.verification_status = res.status
            if res.status == VerificationStatus.EVIDENCED:
                promoted += 1
            elif res.status == VerificationStatus.INCONCLUSIVE and previous != VerificationStatus.INCONCLUSIVE:
                demoted += 1
            store.record_event(
                session, audit.id, AgentRole.CHANGE_INVESTIGATOR, EventType.FINDING_UPDATED,
                f"{finding.public_id}: evidence corroboration -> {res.status.value} "
                f"({len(res.corroborated)} corroborated, {len(res.refuted)} refuted).",
                {
                    "finding_id": finding.id,
                    "previous_status": previous.value,
                    "status": res.status.value,
                    "corroboration_ratio": round(res.corroboration_ratio, 3),
                    "notes": res.notes,
                },
                status="info" if res.status != VerificationStatus.INCONCLUSIVE else "warning",
                engine=audit.engine,
            )
        session.commit()
        store.record_event(
            session, audit.id, AgentRole.CHANGE_INVESTIGATOR, EventType.STAGE_COMPLETED,
            f"Investigation complete: {promoted} finding(s) corroborated by repository "
            f"evidence, {demoted} demoted to inconclusive.",
            {"promoted": promoted, "demoted": demoted},
            engine=audit.engine,
        )

    def _stage_generate_tests(
        self, session: Session, audit: AuditSession, ctx: AuditContext, settings: Settings
    ) -> None:
        store.set_stage(session, audit, AuditStatus.GENERATING_TESTS)
        findings = session.scalars(select(Finding).where(Finding.audit_id == audit.id)).all()
        reproduced = inconclusive = not_reproduced = errored = 0
        for finding in findings:
            draft = _DRAFTS.get((audit.id, finding.id))
            if draft is None:
                continue
            if finding.verification_status == VerificationStatus.INCONCLUSIVE:
                store.record_event(
                    session, audit.id, AgentRole.REPAIR_DESIGNER, EventType.FINDING_UPDATED,
                    f"{finding.public_id}: no reproduction test generated because the "
                    "finding is inconclusive.",
                    {"finding_id": finding.id, "reason": "inconclusive"},
                    status="warning",
                    engine=audit.engine,
                )
                inconclusive += 1
                continue
            generated = testgen.generate(
                draft, finding.public_id, repo_files=list(ctx.bundle.new_sources)
            )
            if not generated.testable:
                finding.verification_status = VerificationStatus.INCONCLUSIVE
                finding.repro_outcome = ReproOutcome.INCONCLUSIVE
                store.record_event(
                    session, audit.id, AgentRole.REPAIR_DESIGNER, EventType.TEST_GENERATED,
                    f"{finding.public_id}: reproduction test NOT generated. "
                    f"{generated.refusal_reason}",
                    {"finding_id": finding.id, "refusal_reason": generated.refusal_reason},
                    status="warning",
                    engine=audit.engine,
                )
                session.commit()
                inconclusive += 1
                continue

            finding.repro_test_file = generated.file_name
            finding.repro_expectations = [e.to_dict() for e in generated.expectations]
            finding.repro_test_rationale = generated.rationale
            store.record_event(
                session, audit.id, AgentRole.REPAIR_DESIGNER, EventType.TEST_GENERATED,
                f"{finding.public_id}: generated {generated.file_name} with "
                f"{len(generated.expectations)} contract-derived assertion(s).",
                {
                    "finding_id": finding.id,
                    "test_file": generated.file_name,
                    "intended_node_ids": generated.intended_node_ids,
                    "expectations": [e.to_dict() for e in generated.expectations],
                    "rationale": generated.rationale,
                },
                engine=audit.engine,
            )
            session.commit()

            self._execute_reproduction(session, audit, ctx, finding, generated, settings)
            # The outcome is recorded on the FINDING (TestRun has no repro_outcome
            # column); reading it from the run would raise AttributeError.
            outcome = finding.repro_outcome
            if outcome == ReproOutcome.REPRODUCED:
                reproduced += 1
            elif outcome == ReproOutcome.NOT_REPRODUCED:
                not_reproduced += 1
            elif outcome == ReproOutcome.EXECUTION_ERROR:
                errored += 1
            else:
                inconclusive += 1

        session.commit()
        store.record_event(
            session, audit.id, AgentRole.REPAIR_DESIGNER, EventType.STAGE_COMPLETED,
            f"Reproduction stage complete: {reproduced} reproduced, "
            f"{not_reproduced} not reproduced, {inconclusive} inconclusive, "
            f"{errored} execution error.",
            {
                "reproduced": reproduced,
                "not_reproduced": not_reproduced,
                "inconclusive": inconclusive,
                "execution_error": errored,
            },
            engine=audit.engine,
        )

    def _execute_reproduction(
        self,
        session: Session,
        audit: AuditSession,
        ctx: AuditContext,
        finding: Finding,
        generated: Any,  # noqa: ANN401
        settings: Settings,
    ) -> Any:  # noqa: ANN401
        """Run the generated test against the ORIGINAL revision, in isolation."""
        workspace = sandbox.make_workspace(settings, prefix=f"repro-{finding.public_id}")
        try:
            repo_dir = workspace / "repo"
            repo_dir.mkdir(parents=True, exist_ok=True)
            ctx.source.export(audit.source_commit_sha, repo_dir)
            tests_dir = workspace / "audit_tests"
            tests_dir.mkdir(exist_ok=True)
            (tests_dir / "conftest.py").write_text(generated.conftest_content, encoding="utf-8")
            (tests_dir / generated.file_name).write_text(generated.content, encoding="utf-8")

            pythonpath = [str(p) for p in (repo_dir, repo_dir / "src") if p.is_dir()]
            report = run_pytest(
                workspace,
                [f"audit_tests/{generated.file_name}"],
                settings=settings,
                label=f"repro-{finding.public_id}",
                pythonpath=pythonpath,
            )
            outcome, reason, match = classify_reproduction(report, generated.intended_node_ids)
        finally:
            sandbox.cleanup_workspace(workspace, settings)

        status_map = {
            ReproOutcome.REPRODUCED: VerificationStatus.REPRODUCED,
            ReproOutcome.NOT_REPRODUCED: VerificationStatus.NOT_REPRODUCED,
            ReproOutcome.INCONCLUSIVE: VerificationStatus.INCONCLUSIVE,
            ReproOutcome.EXECUTION_ERROR: VerificationStatus.EXECUTION_ERROR,
        }
        finding.repro_outcome = outcome
        finding.verification_status = status_map[outcome]

        run = TestRun(
            audit_id=audit.id,
            finding_id=finding.id,
            public_id=store.next_public_id(session, TestRun, "T", audit.id),
            code_revision=audit.source_commit_sha,
            revision_label="original",
            test_type=TestType.REPRODUCTION,
            execution_status=report.status,
            exit_code=report.exit_code,
            tests_total=report.total,
            tests_passed=report.passed,
            tests_failed=report.failed,
            tests_skipped=report.skipped,
            tests_errored=report.errors,
            collected_ids=[c.node_id for c in report.cases],
            executed_ids=[c.node_id for c in report.cases if c.outcome != "skipped"],
            intended_ids=generated.intended_node_ids,
            intended_tests_executed=bool(match.get("all_intended_executed")),
            failures=[c.to_dict() for c in report.cases if c.outcome in ("failed", "error")],
            assertion_signature=", ".join(match.get("intended_assertion_failures", []))[:255],
            intended_assertion_failed=bool(match.get("intended_assertion_failures")),
            sanitized_output=summarise_output(report),
            test_file_name=generated.file_name,
            test_source=generated.content,
            expectations=[e.to_dict() for e in generated.expectations],
            isolation_level=IsolationLevel(report.isolation_level),
            network_disabled=report.network_disabled,
            duration_ms=report.duration_ms,
            workspace_ref="(cleaned up)" if not settings.sandbox_keep_workspaces else str(workspace),
            patch_applied=False,
            started_at=utcnow(),
            completed_at=utcnow(),
        )
        session.add(run)
        session.flush()
        store.record_event(
            session, audit.id, AgentRole.VERIFICATION_AGENT, EventType.TEST_EXECUTED,
            f"{finding.public_id}: reproduction test on the ORIGINAL revision -> "
            f"{outcome.value}. {reason}",
            {
                "finding_id": finding.id,
                "test_run_id": run.id,
                "revision": audit.source_commit_sha,
                "revision_label": "original",
                "execution_status": report.status.value,
                "exit_code": report.exit_code,
                "counts": {
                    "total": report.total, "passed": report.passed,
                    "failed": report.failed, "skipped": report.skipped,
                    "errors": report.errors,
                },
                "intended_tests_executed": bool(match.get("all_intended_executed")),
                "intended_assertion_failures": match.get("intended_assertion_failures"),
                "unrelated_failures": match.get("unrelated_failures"),
                "repro_outcome": outcome.value,
                "reason": reason,
                "isolation_level": report.isolation_level,
                "network_disabled": report.network_disabled,
                "duration_ms": report.duration_ms,
            },
            status="success" if outcome == ReproOutcome.REPRODUCED else "warning",
            engine=audit.engine,
        )
        session.commit()
        return run

    def _stage_propose_patches(self, session: Session, audit: AuditSession, ctx: AuditContext) -> None:
        findings = session.scalars(select(Finding).where(Finding.audit_id == audit.id)).all()
        proposed = 0
        for finding in findings:
            if finding.verification_status != VerificationStatus.REPRODUCED:
                continue
            draft = _DRAFTS.get((audit.id, finding.id))
            if draft is None:
                continue
            source_text = ctx.bundle.new_sources.get(finding.file_path, "")
            patch_draft = patching.propose_from_draft(
                draft,
                source_text,
                ctx.bundle.new_sources | {
                    p: s for p, s in _all_sources(ctx).items()
                },
                [f"audit_tests/{finding.repro_test_file}"] if finding.repro_test_file else [],
            )
            if patch_draft is None:
                store.record_event(
                    session, audit.id, AgentRole.REPAIR_DESIGNER, EventType.FINDING_UPDATED,
                    f"{finding.public_id}: no patch proposed. No mechanical correction is "
                    "justified by the declared contract; the auditor will not invent one.",
                    {"finding_id": finding.id, "reason": "no mechanical repair available"},
                    status="warning",
                    engine=audit.engine,
                )
                continue
            public_id = store.next_public_id(session, PatchProposal, "P", audit.id)
            patch = PatchProposal(
                audit_id=audit.id,
                finding_id=finding.id,
                public_id=public_id,
                base_commit_sha=audit.source_commit_sha,
                file_path=patch_draft.file_path,
                patch_text=patch_draft.patch_text,
                original_code=patch_draft.original_code,
                proposed_code=patch_draft.proposed_code,
                explanation=patch_draft.explanation,
                side_effects=patch_draft.side_effects,
                related_tests=patch_draft.related_tests,
                approval_status=ApprovalStatus.PENDING,
                engine=audit.engine,
                verification_outcome=VerificationOutcome.NOT_ATTEMPTED,
            )
            session.add(patch)
            session.flush()
            # Store the full proposed file so verification can apply it exactly.
            _PATCH_CONTENT[(audit.id, patch.id)] = patch_draft.proposed_full_content
            _PATCH_ORIGINAL[(audit.id, patch.id)] = patch_draft.original_full_content
            finding.verification_status = VerificationStatus.FIX_PROPOSED
            proposed += 1
            store.record_event(
                session, audit.id, AgentRole.REPAIR_DESIGNER, EventType.PATCH_PROPOSED,
                f"{public_id} proposed for {finding.public_id} "
                f"(strategy: {patch_draft.strategy}). Awaiting explicit human approval.",
                {
                    "patch_id": patch.id,
                    "finding_id": finding.id,
                    "strategy": patch_draft.strategy,
                    "file_path": patch_draft.file_path,
                    "call_sites": patch_draft.call_sites,
                    "side_effects": patch_draft.side_effects,
                    "patch_text": patch_draft.patch_text,
                    "approval_status": ApprovalStatus.PENDING.value,
                },
                engine=audit.engine,
            )
        session.commit()
        if proposed:
            store.set_stage(session, audit, AuditStatus.AWAITING_APPROVAL)
            store.record_event(
                session, audit.id, AgentRole.ORCHESTRATOR, EventType.STAGE_COMPLETED,
                f"{proposed} patch proposal(s) awaiting human approval. The audit will "
                "not apply, push, merge or deploy anything without it.",
                {"pending_patches": proposed},
                status="warning",
                engine=audit.engine,
            )

    def _stage_report(self, session: Session, audit: AuditSession, ctx: AuditContext) -> None:
        store.set_stage(session, audit, AuditStatus.REPORTING)
        report = build_report(session, audit, ctx.bundle, ctx.notes, ctx.duo_result)
        store.record_event(
            session, audit.id, AgentRole.REPORTING_SERVICE, EventType.REPORT_GENERATED,
            f"Audit report generated: outcome '{report['outcome']}'.",
            {
                "report_id": report["report_id"],
                "outcome": report["outcome"],
                "counts": report["payload"]["counts"],
            },
            engine=audit.engine,
        )
        final = AuditStatus.COMPLETED
        if any(
            f.verification_status in (VerificationStatus.EXECUTION_ERROR,)
            for f in audit.findings
        ) or audit.error_detail:
            final = AuditStatus.COMPLETED_WITH_ERRORS
        store.set_stage(session, audit, final)
        store.record_event(
            session, audit.id, AgentRole.ORCHESTRATOR, EventType.AUDIT_COMPLETED,
            f"Audit {final.value}.",
            {"status": final.value, "engine": audit.engine.value},
            status="success",
            engine=audit.engine,
        )
        session.commit()

    # ------------------------------------------------- human-driven actions
    def approve_patch(
        self, patch_id: int, *, decided_by: str = "", note: str = ""
    ) -> dict[str, Any]:
        with session_scope() as session:
            patch = session.get(PatchProposal, patch_id)
            if patch is None:
                raise KeyError(f"patch {patch_id} not found")
            if patch.approval_status == ApprovalStatus.APPROVED:
                return {"patch_id": patch_id, "approval_status": patch.approval_status.value,
                        "already": True}
            patch.approval_status = ApprovalStatus.APPROVED
            patch.decided_by = decided_by or "api-user"
            patch.decision_note = note
            patch.decided_at = utcnow()
            finding = session.get(Finding, patch.finding_id) if patch.finding_id else None
            if finding is not None and finding.verification_status == VerificationStatus.FIX_PROPOSED:
                finding.verification_status = VerificationStatus.FIX_APPROVED
            store.record_event(
                session, patch.audit_id, AgentRole.ORCHESTRATOR, EventType.PATCH_APPROVED,
                f"{patch.public_id} approved by {patch.decided_by}. Approval authorises "
                "execution inside an isolated audit workspace only; it does not authorise "
                "pushing, merging or deploying.",
                {"patch_id": patch_id, "decided_by": patch.decided_by, "note": note},
                status="success",
            )
            session.commit()
            return {"patch_id": patch_id, "approval_status": patch.approval_status.value,
                    "already": False}

    def reject_patch(
        self, patch_id: int, *, decided_by: str = "", note: str = ""
    ) -> dict[str, Any]:
        with session_scope() as session:
            patch = session.get(PatchProposal, patch_id)
            if patch is None:
                raise KeyError(f"patch {patch_id} not found")
            patch.approval_status = ApprovalStatus.REJECTED
            patch.decided_by = decided_by or "api-user"
            patch.decision_note = note
            patch.decided_at = utcnow()
            finding = session.get(Finding, patch.finding_id) if patch.finding_id else None
            if finding is not None and finding.verification_status in (
                VerificationStatus.FIX_PROPOSED, VerificationStatus.FIX_APPROVED
            ):
                finding.verification_status = VerificationStatus.REPRODUCED
            store.record_event(
                session, patch.audit_id, AgentRole.ORCHESTRATOR, EventType.PATCH_REJECTED,
                f"{patch.public_id} rejected by {patch.decided_by}.",
                {"patch_id": patch_id, "decided_by": patch.decided_by, "note": note},
                status="warning",
            )
            session.commit()
            return {"patch_id": patch_id, "approval_status": patch.approval_status.value}

    def verify_patch(self, patch_id: int) -> dict[str, Any]:
        """Execute verification for an APPROVED patch. Refuses otherwise."""
        settings = self.settings
        with session_scope() as session:
            patch = session.get(PatchProposal, patch_id)
            if patch is None:
                raise KeyError(f"patch {patch_id} not found")
            if patch.approval_status != ApprovalStatus.APPROVED:
                raise PermissionError(
                    f"patch {patch.public_id} is '{patch.approval_status.value}'; "
                    "explicit human approval is required before verification"
                )
            audit = store.get_audit(session, patch.audit_id)
            finding = session.get(Finding, patch.finding_id) if patch.finding_id else None
            if audit is None or finding is None:
                raise KeyError("patch is not linked to an audit and finding")
            project = store.get_project(session, audit.project_id)
            ctx = self._stage_fetch_context(session, audit, project, settings)
            store.set_stage(session, audit, AuditStatus.VERIFYING)
            store.record_event(
                session, audit.id, AgentRole.VERIFICATION_AGENT, EventType.STAGE_STARTED,
                f"Verifying {patch.public_id} against revision "
                f"{audit.source_commit_sha[:12]} in an isolated workspace.",
                {"patch_id": patch_id, "finding_id": finding.id},
                engine=audit.engine,
            )
            session.commit()

            proposed_full = _PATCH_CONTENT.get((audit.id, patch.id))
            original_full = _PATCH_ORIGINAL.get((audit.id, patch.id))
            if not proposed_full:
                draft = _DRAFTS.get((audit.id, finding.id))
                source_text = ctx.bundle.new_sources.get(patch.file_path, "")
                if draft is None or not source_text:
                    raise RuntimeError(
                        "proposed content is not available for this patch; re-run the audit "
                        "stage to regenerate it"
                    )
                regenerated = patching.propose_from_draft(
                    draft, source_text, _all_sources(ctx), []
                )
                if regenerated is None:
                    raise RuntimeError("could not regenerate the proposed patch content")
                proposed_full = regenerated.proposed_full_content
                original_full = regenerated.original_full_content
                _PATCH_CONTENT[(audit.id, patch.id)] = proposed_full
                _PATCH_ORIGINAL[(audit.id, patch.id)] = original_full or source_text

            test_file = finding.repro_test_file
            if not test_file:
                raise RuntimeError(
                    "no reproduction test is recorded for this finding; verification "
                    "requires an executable test"
                )
            generated_files = {
                "conftest.py": testgen.CONFTEST_TEMPLATE,
                test_file: _reconstruct_test_source(session, finding, test_file),
            }
            intended = _intended_ids(finding)
            if not intended:
                raise RuntimeError(
                    "no intended test node ids were persisted for this finding; "
                    "cannot verify that the intended tests executed"
                )
            result = run_verification(
                source=ctx.source,
                original_sha=audit.source_commit_sha,
                file_path=patch.file_path,
                original_full_content=original_full or "",
                proposed_full_content=proposed_full,
                generated_files=generated_files,
                repro_test_target=f"audit_tests/{test_file}",
                intended_node_ids=intended,
                settings=settings,
                symbol=finding.symbol,
            )

            # Persist every run as a TestRow so the evidence outlives the process.
            for rec in result.runs:
                test_type = {
                    "baseline_reproduction": TestType.REPRODUCTION,
                    "baseline_regression": TestType.REGRESSION,
                    "post_fix_reproduction": TestType.POST_FIX_REPRODUCTION,
                    "post_fix_regression": TestType.POST_FIX_REGRESSION,
                }.get(rec.label, TestType.REPRODUCTION)
                r = rec.report
                run = TestRun(
                    audit_id=audit.id,
                    finding_id=finding.id,
                    patch_id=patch.id,
                    public_id=store.next_public_id(session, TestRun, "T", audit.id),
                    code_revision=rec.revision,
                    revision_label=rec.revision_label,
                    test_type=test_type,
                    execution_status=r.status,
                    exit_code=r.exit_code,
                    tests_total=r.total,
                    tests_passed=r.passed,
                    tests_failed=r.failed,
                    tests_skipped=r.skipped,
                    tests_errored=r.errors,
                    collected_ids=[c.node_id for c in r.cases],
                    executed_ids=[c.node_id for c in r.cases if c.outcome != "skipped"],
                    intended_ids=rec.intended_match.get("intended", []),
                    intended_tests_executed=bool(rec.intended_match.get("all_intended_executed")),
                    failures=[c.to_dict() for c in r.cases if c.outcome in ("failed", "error")],
                    assertion_signature=", ".join(
                        rec.intended_match.get("intended_assertion_failures", [])
                    )[:255],
                    intended_assertion_failed=bool(
                        rec.intended_match.get("intended_assertion_failures")
                    ),
                    sanitized_output=summarise_output(r),
                    test_file_name=test_file if "reproduction" in rec.label else "(repository suite)",
                    isolation_level=IsolationLevel(r.isolation_level),
                    network_disabled=r.network_disabled,
                    duration_ms=r.duration_ms,
                    patch_applied=rec.patch_applied,
                    started_at=utcnow(),
                    completed_at=utcnow(),
                )
                session.add(run)
                session.flush()
                store.record_event(
                    session, audit.id, AgentRole.VERIFICATION_AGENT, EventType.TEST_EXECUTED,
                    f"{rec.summary_line}",
                    rec.to_dict(),
                    status="success" if r.status == TestRunStatus.PASSED else "warning",
                    engine=audit.engine,
                )

            outcome_to_status = {
                VerificationOutcome.VERIFIED_FOR_TESTED_BEHAVIOR: VerificationStatus.FIX_VERIFIED,
                VerificationOutcome.VERIFICATION_FAILED: VerificationStatus.FIX_VERIFICATION_FAILED,
                VerificationOutcome.REGRESSION_DETECTED: VerificationStatus.REGRESSION_DETECTED,
                VerificationOutcome.INCONCLUSIVE: VerificationStatus.INCONCLUSIVE,
                VerificationOutcome.EXECUTION_ERROR: VerificationStatus.EXECUTION_ERROR,
                VerificationOutcome.NOT_ATTEMPTED: VerificationStatus.FIX_APPROVED,
            }
            patch.verification_outcome = result.outcome
            patch.verification_detail = result.to_dict()
            patch.verified_at = utcnow()
            finding.verification_status = outcome_to_status[result.outcome]
            if result.outcome == VerificationOutcome.VERIFIED_FOR_TESTED_BEHAVIOR:
                finding.repro_outcome = ReproOutcome.REPRODUCED
            store.record_event(
                session, audit.id, AgentRole.VERIFICATION_AGENT,
                EventType.VERIFICATION_COMPLETED,
                f"{patch.public_id}: verification outcome = {result.outcome.value}. "
                f"{result.summary.splitlines()[1] if len(result.summary.splitlines()) > 1 else ''}",
                result.to_dict(),
                status="success"
                if result.outcome == VerificationOutcome.VERIFIED_FOR_TESTED_BEHAVIOR
                else "warning",
                engine=audit.engine,
            )
            if result.regressions:
                store.record_event(
                    session, audit.id, AgentRole.VERIFICATION_AGENT,
                    EventType.REGRESSION_RUN_COMPLETED,
                    f"{len(result.regressions)} previously passing test(s) broke after the patch.",
                    {"regressions": result.regressions},
                    status="error",
                    engine=audit.engine,
                )
            session.commit()

            self._stage_report(session, audit, ctx)
            session.commit()
            return {
                "patch_id": patch_id,
                "outcome": result.outcome.value,
                "summary": result.summary,
                "reasons": result.reasons,
                "remaining_concerns": result.remaining_concerns,
                "intended_tests_executed": result.intended_tests_executed,
                "regressions": result.regressions,
                "runs": [r.to_dict() for r in result.runs],
                # The engine's own statement of what the verification does NOT
                # prove. Omitting it made every caller silently fall back to an
                # empty string, so the API looked more confident than the engine.
                "scope_statement": result.scope_statement,
            }


def _all_sources(ctx: AuditContext) -> dict[str, str]:
    src = getattr(ctx.source, "all_python_files", None)
    if callable(src):
        try:
            return src(ctx.bundle.source_commit_sha)
        except Exception:  # noqa: BLE001
            return {}
    return dict(ctx.bundle.new_sources)


def _intended_ids(finding: Finding) -> list[str]:
    """Reconstruct intended node ids from the stored reproduction test run."""
    for run in finding.test_runs:
        if run.test_type == TestType.REPRODUCTION and run.intended_ids:
            return list(run.intended_ids)
    return []


def _reconstruct_test_source(session: Session, finding: Finding, test_file: str) -> str:
    for run in finding.test_runs:
        if run.test_file_name == test_file and run.test_source:
            return run.test_source
    raise RuntimeError(f"generated test source for {test_file} was not persisted")


# In-process caches.  The authoritative copies live in the database; these exist
# so a single audit run does not recompute drafts.  `verify_patch` regenerates
# from persisted data when the cache is cold (e.g. after a restart).
_DRAFTS: dict[tuple[int, int], Any] = {}
_PATCH_CONTENT: dict[tuple[int, int], str] = {}
_PATCH_ORIGINAL: dict[tuple[int, int], str] = {}


def submit_audit(audit_id: int, settings: Settings | None = None) -> bool:
    """Queue an audit for background execution. Idempotent per audit id."""
    with _lock:
        if audit_id in _running:
            return False
        _running.add(audit_id)
    get_executor(settings).submit(_run_and_release, audit_id, settings)
    return True


def _run_and_release(audit_id: int, settings: Settings | None) -> None:
    """Worker entry point.

    ``submit_audit`` has ALREADY registered this audit in ``_running``, so this
    must not call the guarded ``run_audit`` -- that would see its own registration
    and return without doing any work. It calls the unguarded inner pipeline and
    releases the registration itself.
    """
    orch = AuditOrchestrator(settings)
    try:
        orch._run_audit_inner(audit_id)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 - must be recorded, never swallowed
        logger.exception("audit %s failed in worker", audit_id)
        orch._mark_failed(audit_id, exc)  # noqa: SLF001
    finally:
        with _lock:
            _running.discard(audit_id)


def is_running(audit_id: int) -> bool:
    with _lock:
        return audit_id in _running


__all__ = [
    "AuditContext",
    "AuditOrchestrator",
    "is_running",
    "submit_audit",
]
