"""Pydantic request/response schemas.

Every request body is validated, every identifier constrained.  Responses never
echo secret material: configuration is served through ``redacted()`` and tokens
are write-only.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
PUBLIC_ID_RE = re.compile(r"^[A-Za-z]{1,4}-\d+-\d{1,4}$")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
class AuditCreateRequest(BaseModel):
    """Create an audit session. Exactly one of the target selectors is needed."""

    project_id: int | None = Field(default=None, ge=1, description="Internal project id")
    gitlab_project_id: int | None = Field(default=None, ge=1, description="GitLab project id")
    merge_request_iid: int | None = Field(default=None, ge=1)
    source_branch: str = Field(default="", max_length=255)
    target_branch: str = Field(default="", max_length=255)
    source_commit_sha: str = Field(default="", max_length=64)
    target_commit_sha: str = Field(default="", max_length=64)
    mode: Literal["local_demo", "live_gitlab"] = "local_demo"
    auto_start: bool = Field(
        default=True,
        description="Queue the audit immediately. Audits run asynchronously.",
    )

    @field_validator("source_commit_sha", "target_commit_sha")
    @classmethod
    def _validate_sha(cls, v: str) -> str:
        if v and not SHA_RE.match(v):
            raise ValueError("commit SHA must be 7-64 lowercase hex characters")
        return v

    @field_validator("source_branch", "target_branch")
    @classmethod
    def _validate_branch(cls, v: str) -> str:
        if v and re.search(r"[\s~^:?*\[\\]", v):
            raise ValueError("branch name contains invalid characters")
        return v.strip()


class DecisionRequest(BaseModel):
    decided_by: str = Field(default="api-user", max_length=255)
    note: str = Field(default="", max_length=2000)

    @field_validator("decided_by")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        return v or "api-user"


class GenerateTestRequest(BaseModel):
    """Regenerate/execute the reproduction test for one finding."""

    execute: bool = Field(default=True, description="Also run the test in isolation")


class GeneratePatchRequest(BaseModel):
    propose: bool = Field(default=True)


class SettingsUpdateRequest(BaseModel):
    """Non-secret runtime preferences only. Credentials stay server-side."""

    sandbox_timeout_seconds: int | None = Field(default=None, ge=5, le=3600)
    sandbox_memory_mb: int | None = Field(default=None, ge=128, le=16384)
    sandbox_cpu_seconds: int | None = Field(default=None, ge=5, le=3600)
    max_findings_per_audit: int | None = Field(default=None, ge=1, le=200)
    test_framework: Literal["pytest"] | None = None
    auto_generate_tests: bool | None = None
    sandbox_mode: Literal["auto", "container", "linux_namespace", "process"] | None = None
    sandbox_keep_workspaces: bool | None = None


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #
class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    app: str
    version: str
    environment: str
    database: str
    database_dialect: str
    isolation: dict[str, Any]
    gitlab: dict[str, Any]
    duo: dict[str, Any]
    # Verified by probing the repository, not inferred from a config flag.
    demo: dict[str, Any] = Field(default_factory=dict)
    demo_mode_available: bool


class ProjectResponse(ORMModel):
    id: int
    gitlab_project_id: int | None = None
    name: str
    namespace: str
    path_with_namespace: str
    default_branch: str
    web_url: str
    local_path: str | None = None
    origin: str
    created_at: Any


class MergeRequestResponse(BaseModel):
    iid: int | None
    title: str
    state: str
    source_branch: str
    target_branch: str
    sha: str
    diff_head_sha: str
    base_sha: str
    web_url: str
    author: str
    pipeline_status: str
    draft: bool
    has_conflicts: bool


class EvidenceResponse(BaseModel):
    kind: str
    description: str
    file_path: str = ""
    line_start: int | None = None
    line_end: int | None = None
    content: str = ""
    note: str = ""


class ConfidenceResponse(BaseModel):
    level: str = ""
    signal_agreement_score: float = 0.0
    basis: list[str] = Field(default_factory=list)
    calibrated_on_labelled_corpus: bool = False
    interpretation: str = ""
    note: str = ""
    post_investigation: dict[str, Any] = Field(default_factory=dict)


class TestRunResponse(ORMModel):
    id: int
    public_id: str
    finding_id: int | None = None
    patch_id: int | None = None
    code_revision: str
    revision_label: str
    test_type: str
    execution_status: str
    exit_code: int | None = None
    tests_total: int
    tests_passed: int
    tests_failed: int
    tests_skipped: int
    tests_errored: int
    intended_tests_executed: bool
    intended_assertion_failed: bool | None = None
    assertion_signature: str
    isolation_level: str
    network_disabled: bool
    duration_ms: int
    test_file_name: str
    patch_applied: bool
    sanitized_output: str
    failures: list[Any] = Field(default_factory=list)
    # The exact source that was executed, plus the declared intent, so a reviewer
    # can confirm the run really tested what the finding claims. Persisting this
    # is what makes "reproduced" auditable rather than asserted.
    test_source: str | None = None
    expectations: list[Any] = Field(default_factory=list)
    collected_ids: list[str] = Field(default_factory=list)
    executed_ids: list[str] = Field(default_factory=list)
    intended_ids: list[str] = Field(default_factory=list)
    workspace_ref: str | None = None
    started_at: Any = None
    completed_at: Any = None


class FindingSummary(ORMModel):
    id: int
    public_id: str
    category: str
    severity: str
    title: str
    file_path: str
    line_start: int | None = None
    line_end: int | None = None
    symbol: str
    verification_status: str
    repro_outcome: str | None = None
    rule_id: str
    engine: str


class FindingResponse(FindingSummary):
    audit_id: int
    description: str
    evidence: list[Any] = Field(default_factory=list)
    expected_behavior: str
    suspected_behavior: str
    confidence: dict[str, Any] = Field(default_factory=dict)
    recommended_action: str
    facts: list[Any] = Field(default_factory=list)
    hypotheses: list[Any] = Field(default_factory=list)
    missing_information: list[Any] = Field(default_factory=list)
    investigation: dict[str, Any] = Field(default_factory=dict)
    repro_test_file: str
    repro_expectations: list[Any] = Field(default_factory=list)
    repro_test_rationale: str
    created_at: Any
    test_runs: list[TestRunResponse] = Field(default_factory=list)
    patches: list["PatchResponse"] = Field(default_factory=list)


class PatchResponse(ORMModel):
    id: int
    public_id: str
    audit_id: int
    finding_id: int | None = None
    base_commit_sha: str
    file_path: str
    original_code: str
    proposed_code: str
    patch_text: str
    explanation: str
    side_effects: list[Any] = Field(default_factory=list)
    related_tests: list[Any] = Field(default_factory=list)
    approval_status: str
    decided_by: str
    decision_note: str
    decided_at: Any = None
    engine: str
    verification_outcome: str
    verification_detail: dict[str, Any] = Field(default_factory=dict)
    verified_at: Any = None
    created_at: Any


class AgentEventResponse(ORMModel):
    id: int
    seq: int
    agent_role: str
    event_type: str
    status: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    engine: str
    created_at: Any


class AuditResponse(ORMModel):
    id: int
    project_id: int
    merge_request_iid: int | None = None
    merge_request_title: str
    source_branch: str
    target_branch: str
    source_commit_sha: str
    target_commit_sha: str
    status: str
    stage: str
    progress_pct: int
    mode: str
    engine: str
    duo_session_id: int | None = None
    duo_web_url: str
    duo_availability: dict[str, Any] = Field(default_factory=dict)
    started_at: Any = None
    completed_at: Any = None
    error_detail: str
    created_at: Any
    findings_count: int = 0
    patches_count: int = 0
    test_runs_count: int = 0
    outcome: str = ""


class AuditDetailResponse(AuditResponse):
    project: ProjectResponse | None = None
    findings: list[FindingSummary] = Field(default_factory=list)
    patches: list[PatchResponse] = Field(default_factory=list)
    test_runs: list[TestRunResponse] = Field(default_factory=list)
    recent_events: list[AgentEventResponse] = Field(default_factory=list)
    report: dict[str, Any] | None = None


class ReportResponse(BaseModel):
    report_id: int
    public_id: str
    audit_id: int
    outcome: str
    outcome_explanation: str
    summary: str
    created_at: Any
    report: dict[str, Any]


class TestRunTotals(BaseModel):
    """Aggregate execution counters shown on the dashboard.

    These are real counts read from persisted TestRun rows. Nothing here is
    estimated, and no accuracy-style percentage is derived from them.
    """

    runs: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    tests_total: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    tests_skipped: int = 0
    tests_errored: int = 0


class DashboardResponse(BaseModel):
    """Every number here is computed from persisted rows -- never hardcoded."""

    totals: dict[str, int]
    audits_by_status: dict[str, int]
    findings_by_severity: dict[str, int]
    findings_by_verification_status: dict[str, int]
    verified_fixes: int
    patches_pending_approval: int
    findings_requiring_attention: int
    test_runs: TestRunTotals
    isolation_levels_used: dict[str, int]
    recent_audits: list[AuditResponse]
    recent_events: list[AgentEventResponse]
    recent_ci_results: list[dict[str, Any]]
    engine_usage: dict[str, int]


class ErrorResponse(BaseModel):
    error: str
    detail: str
    code: str = ""
    status_code: int


class AckResponse(BaseModel):
    ok: bool = True
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class VerificationResponse(BaseModel):
    patch_id: int
    outcome: str
    summary: str
    reasons: list[str]
    remaining_concerns: list[str]
    intended_tests_executed: bool
    regressions: list[Any]
    runs: list[Any]
    scope_statement: str = ""


class DuoAvailabilityResponse(BaseModel):
    available: bool
    reason: str
    missing_prerequisite: str
    checks: list[dict[str, Any]]
    gitlab_version: str
    flow_definition: str
    flow_config_path: str
    flow_yaml_present: bool
    flow_yaml_path: str
    engine_used_when_unavailable: str


FindingResponse.model_rebuild()
