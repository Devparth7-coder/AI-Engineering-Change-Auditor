"""SQLAlchemy models and the controlled vocabularies used across the system.

The vocabulary is the heart of the product's honesty guarantee: every enum
below maps to an explicit, human-readable outcome label that appears in the UI
and the final report.  There is deliberately no "AI quality score" anywhere.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, JSONVariant


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Controlled vocabularies
# --------------------------------------------------------------------------- #
class AuditStatus(str, enum.Enum):
    """Lifecycle of an audit session."""

    CREATED = "created"
    QUEUED = "queued"
    FETCHING_CONTEXT = "fetching_context"
    ANALYZING = "analyzing"
    INVESTIGATING = "investigating"
    GENERATING_TESTS = "generating_tests"
    AWAITING_APPROVAL = "awaiting_approval"
    VERIFYING = "verifying"
    REPORTING = "reporting"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_AUDIT_STATUSES = {
    AuditStatus.COMPLETED,
    AuditStatus.COMPLETED_WITH_ERRORS,
    AuditStatus.FAILED,
    AuditStatus.CANCELLED,
}


class FindingCategory(str, enum.Enum):
    LOGIC_ERROR = "logic_error"
    BOUNDARY_CONDITION = "boundary_condition"
    INPUT_VALIDATION = "input_validation"
    ERROR_HANDLING = "error_handling"
    REGRESSION = "regression"
    INCORRECT_ASSUMPTION = "incorrect_assumption"
    MISSING_TESTS = "missing_tests"
    RESOURCE_LEAK = "resource_leak"
    CONCURRENCY = "concurrency"


class Severity(str, enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class VerificationStatus(str, enum.Enum):
    """How much evidence stands behind a finding *right now*.

    The ordering matters: the UI and reports must never present a
    ``HYPOTHESIS`` as though it were a ``FIX_VERIFIED``.
    """

    HYPOTHESIS = "hypothesis"                     # AI/rule generated, no execution yet
    EVIDENCED = "evidenced"                        # backed by repository evidence only
    REPRODUCED = "reproduced"                      # defect reproduced by test execution
    NOT_REPRODUCED = "not_reproduced"              # ran, but did not reproduce
    INCONCLUSIVE = "inconclusive"                  # insufficient evidence either way
    EXECUTION_ERROR = "execution_error"            # could not run the test at all
    FIX_PROPOSED = "fix_proposed"                  # patch generated, unapproved
    FIX_APPROVED = "fix_approved"                  # human approved, not yet verified
    FIX_VERIFIED = "fix_verified"                  # verified by ACTUAL test execution
    FIX_VERIFICATION_FAILED = "fix_verification_failed"
    REGRESSION_DETECTED = "regression_detected"


class ReproOutcome(str, enum.Enum):
    """Classification of a reproduction-test execution against the ORIGINAL revision."""

    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    INCONCLUSIVE = "inconclusive"
    EXECUTION_ERROR = "execution_error"


class VerificationOutcome(str, enum.Enum):
    """Result of applying an approved patch and re-executing tests."""

    VERIFIED_FOR_TESTED_BEHAVIOR = "verified_for_tested_behavior"
    VERIFICATION_FAILED = "verification_failed"
    REGRESSION_DETECTED = "regression_detected"
    INCONCLUSIVE = "inconclusive"
    EXECUTION_ERROR = "execution_error"
    NOT_ATTEMPTED = "not_attempted"


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class TestType(str, enum.Enum):
    REPRODUCTION = "reproduction"
    REGRESSION = "regression"
    POST_FIX_REPRODUCTION = "post_fix_reproduction"
    POST_FIX_REGRESSION = "post_fix_regression"
    PROBE = "probe"


class TestRunStatus(str, enum.Enum):
    """Raw execution status. Never conflated with 'the fix works'."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"
    NOT_COLLECTED = "not_collected"
    SKIPPED = "skipped"
    EXECUTION_ERROR = "execution_error"


class IsolationLevel(str, enum.Enum):
    """Honest description of how untrusted code was contained."""

    CONTAINER = "container"                       # docker
    LINUX_NAMESPACE = "linux_namespace"           # unshare user+net+mount ns
    PROCESS_RESTRICTED = "process_restricted"     # rlimits + scrubbed env only
    NONE = "none"


class AgentRole(str, enum.Enum):
    CHANGE_INVESTIGATOR = "change_investigator"
    REPAIR_DESIGNER = "repair_designer"
    VERIFICATION_AGENT = "verification_agent"
    ORCHESTRATOR = "orchestrator"
    REPORTING_SERVICE = "reporting_service"


class EngineKind(str, enum.Enum):
    """Which engine actually produced a result. Surfaced everywhere in the UI."""

    GITLAB_DUO_FLOW = "gitlab_duo_flow"
    LOCAL_DETERMINISTIC = "local_deterministic"
    GITLAB_REST = "gitlab_rest"
    LOCAL_DEMO = "local_demo"


class EventType(str, enum.Enum):
    AUDIT_CREATED = "audit_created"
    AUDIT_STARTED = "audit_started"
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    STAGE_FAILED = "stage_failed"
    CONTEXT_FETCHED = "context_fetched"
    FINDING_CREATED = "finding_created"
    FINDING_UPDATED = "finding_updated"
    TEST_GENERATED = "test_generated"
    TEST_EXECUTED = "test_executed"
    PATCH_PROPOSED = "patch_proposed"
    PATCH_APPROVED = "patch_approved"
    PATCH_REJECTED = "patch_rejected"
    PATCH_APPLIED = "patch_applied"
    VERIFICATION_COMPLETED = "verification_completed"
    REGRESSION_RUN_COMPLETED = "regression_run_completed"
    DUO_FLOW_TRIGGERED = "duo_flow_triggered"
    DUO_FLOW_EVENT = "duo_flow_event"
    DUO_FLOW_COMPLETED = "duo_flow_completed"
    DUO_UNAVAILABLE = "duo_unavailable"
    REPORT_GENERATED = "report_generated"
    AUDIT_COMPLETED = "audit_completed"
    AUDIT_FAILED = "audit_failed"
    SECURITY_NOTICE = "security_notice"


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gitlab_project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    path_with_namespace: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    web_url: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    local_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="local_demo")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    audits: Mapped[list[AuditSession]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_projects_origin_name", "origin", "name"),)


class AuditSession(Base):
    __tablename__ = "audit_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    merge_request_iid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    merge_request_title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    source_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    target_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    source_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    target_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[AuditStatus] = mapped_column(
        SAEnum(AuditStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=AuditStatus.CREATED,
        index=True,
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="local_demo")
    engine: Mapped[EngineKind] = mapped_column(
        SAEnum(EngineKind, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=EngineKind.LOCAL_DETERMINISTIC,
    )
    duo_session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duo_web_url: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    duo_availability: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    stage: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="audits")
    findings: Mapped[list[Finding]] = relationship(
        back_populates="audit", cascade="all, delete-orphan", order_by="Finding.id"
    )
    patches: Mapped[list[PatchProposal]] = relationship(
        back_populates="audit", cascade="all, delete-orphan", order_by="PatchProposal.id"
    )
    test_runs: Mapped[list[TestRun]] = relationship(
        back_populates="audit", cascade="all, delete-orphan", order_by="TestRun.id"
    )
    events: Mapped[list[AgentEvent]] = relationship(
        back_populates="audit", cascade="all, delete-orphan", order_by="AgentEvent.id"
    )
    reports: Mapped[list[AuditReport]] = relationship(
        back_populates="audit", cascade="all, delete-orphan", order_by="AuditReport.id"
    )


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("audit_sessions.id"), nullable=False, index=True)
    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    category: Mapped[FindingCategory] = mapped_column(
        SAEnum(FindingCategory, values_callable=lambda e: [m.value for m in e]), nullable=False
    )
    severity: Mapped[Severity] = mapped_column(
        SAEnum(Severity, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="", index=True)
    line_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    evidence: Mapped[list[Any]] = mapped_column(JSONVariant, default=list)
    expected_behavior: Mapped[str] = mapped_column(Text, nullable=False, default="")
    suspected_behavior: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        SAEnum(VerificationStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=VerificationStatus.HYPOTHESIS,
        index=True,
    )
    repro_outcome: Mapped[ReproOutcome | None] = mapped_column(
        SAEnum(ReproOutcome, values_callable=lambda e: [m.value for m in e]), nullable=True
    )
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False, default="")
    engine: Mapped[EngineKind] = mapped_column(
        SAEnum(EngineKind, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=EngineKind.LOCAL_DETERMINISTIC,
    )
    rule_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    facts: Mapped[list[Any]] = mapped_column(JSONVariant, default=list)
    hypotheses: Mapped[list[Any]] = mapped_column(JSONVariant, default=list)
    missing_information: Mapped[list[Any]] = mapped_column(JSONVariant, default=list)
    # Output of the investigator stage: what was corroborated, what was refuted.
    investigation: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    repro_test_file: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    repro_expectations: Mapped[list[Any]] = mapped_column(JSONVariant, default=list)
    repro_test_rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    audit: Mapped[AuditSession] = relationship(back_populates="findings")
    patches: Mapped[list[PatchProposal]] = relationship(back_populates="finding")
    test_runs: Mapped[list[TestRun]] = relationship(back_populates="finding")


class PatchProposal(Base):
    __tablename__ = "patch_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("audit_sessions.id"), nullable=False, index=True)
    finding_id: Mapped[int | None] = mapped_column(
        ForeignKey("findings.id"), nullable=True, index=True
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    base_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    patch_artifact_ref: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    patch_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    original_code: Mapped[str] = mapped_column(Text, nullable=False, default="")
    proposed_code: Mapped[str] = mapped_column(Text, nullable=False, default="")
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    side_effects: Mapped[list[Any]] = mapped_column(JSONVariant, default=list)
    related_tests: Mapped[list[Any]] = mapped_column(JSONVariant, default=list)
    approval_status: Mapped[ApprovalStatus] = mapped_column(
        SAEnum(ApprovalStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ApprovalStatus.PENDING,
        index=True,
    )
    decided_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    decision_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    engine: Mapped[EngineKind] = mapped_column(
        SAEnum(EngineKind, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=EngineKind.LOCAL_DETERMINISTIC,
    )
    verification_outcome: Mapped[VerificationOutcome] = mapped_column(
        SAEnum(VerificationOutcome, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=VerificationOutcome.NOT_ATTEMPTED,
    )
    verification_detail: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    audit: Mapped[AuditSession] = relationship(back_populates="patches")
    finding: Mapped[Finding | None] = relationship(back_populates="patches")


class TestRun(Base):
    __tablename__ = "test_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("audit_sessions.id"), nullable=False, index=True)
    finding_id: Mapped[int | None] = mapped_column(
        ForeignKey("findings.id"), nullable=True, index=True
    )
    patch_id: Mapped[int | None] = mapped_column(ForeignKey("patch_proposals.id"), nullable=True)
    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    code_revision: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    revision_label: Mapped[str] = mapped_column(String(32), nullable=False, default="original")
    test_type: Mapped[TestType] = mapped_column(
        SAEnum(TestType, values_callable=lambda e: [m.value for m in e]), nullable=False
    )
    execution_status: Mapped[TestRunStatus] = mapped_column(
        SAEnum(TestRunStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        index=True,
    )
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tests_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tests_passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tests_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tests_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tests_errored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    collected_ids: Mapped[list[Any]] = mapped_column(JSONVariant, default=list)
    executed_ids: Mapped[list[Any]] = mapped_column(JSONVariant, default=list)
    intended_ids: Mapped[list[Any]] = mapped_column(JSONVariant, default=list)
    intended_tests_executed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failures: Mapped[list[Any]] = mapped_column(JSONVariant, default=list)
    assertion_signature: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    intended_assertion_failed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sanitized_output: Mapped[str] = mapped_column(Text, nullable=False, default="")
    test_file_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    test_source: Mapped[str] = mapped_column(Text, nullable=False, default="")
    expectations: Mapped[list[Any]] = mapped_column(JSONVariant, default=list)
    isolation_level: Mapped[IsolationLevel] = mapped_column(
        SAEnum(IsolationLevel, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=IsolationLevel.PROCESS_RESTRICTED,
    )
    network_disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    workspace_ref: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    patch_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    audit: Mapped[AuditSession] = relationship(back_populates="test_runs")
    finding: Mapped[Finding | None] = relationship(back_populates="test_runs")


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("audit_sessions.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    agent_role: Mapped[AgentRole] = mapped_column(
        SAEnum(AgentRole, values_callable=lambda e: [m.value for m in e]), nullable=False
    )
    event_type: Mapped[EventType] = mapped_column(
        SAEnum(EventType, values_callable=lambda e: [m.value for m in e]), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    engine: Mapped[str] = mapped_column(String(32), nullable=False, default="local_deterministic")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    audit: Mapped[AuditSession] = relationship(back_populates="events")

    __table_args__ = (Index("ix_agent_events_audit_seq", "audit_id", "seq"),)


class AuditReport(Base):
    __tablename__ = "audit_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("audit_sessions.id"), nullable=False, index=True)
    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    outcome: Mapped[str] = mapped_column(String(64), nullable=False, default="inconclusive")
    report: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    audit: Mapped[AuditSession] = relationship(back_populates="reports")
