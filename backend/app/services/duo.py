"""GitLab Duo Agent Platform integration (Flow Registry v1 / Flows API).

This module talks to the *real* platform.  It contains no mock and no fallback
LLM call: if the platform is not reachable, it says so precisely and the
orchestrator records that fact against the audit.

Integration path (see ``docs/duo-integration.md`` for the full walkthrough):

1. The audit flow is authored as Flow Registry v1 YAML at
   ``agents/flows/change-audit/flow.yaml`` and registered with GitLab
   (project **AI > Catalog**, or the group AI Catalog).  Writing the YAML alone
   does NOT register an agent -- registration is a deliberate GitLab-side step.
2. Flow *execution* settings (image, setup script, network policy) live at
   ``.gitlab/duo/agent-config.yml`` and are read from the default branch.
3. The backend triggers the flow with
   ``POST /api/v4/ai/duo_workflows/workflows`` (``start_workflow=true``,
   ``environment=ambient``) and polls the session for events.
4. Human approval is handled by a ``HumanInputComponent`` inside the flow, and
   resumed with ``POST /api/v4/ai/duo_workflows/workflows/:id/resume``.

Requirements observed on the instance side:
  * GitLab Premium or Ultimate (Duo Agent Platform tier),
  * GitLab Duo turned on for the top-level group/instance,
  * GitLab >= 18.8 for the platform, >= 19.2 for GA custom flows,
    >= 19.5 for the GA Flows API,
  * a project in a **group** namespace with Developer/Maintainer/Owner role,
  * a PAT with ``api`` scope (and ``ai_workflows`` where OAuth is used).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.models import AgentRole, EngineKind
from app.security.sanitize import sanitize_value

logger = logging.getLogger(__name__)

# Minimum GitLab versions for each capability, from the platform docs.
MIN_VERSION_PLATFORM = (18, 8)
MIN_VERSION_CUSTOM_FLOWS_GA = (19, 2)
MIN_VERSION_FLOWS_API_GA = (19, 5)


class DuoUnavailable(RuntimeError):
    """The Duo Agent Platform could not be used. Carries the exact reason."""

    def __init__(self, reason: str, *, prerequisite: str = "", checks: list[dict[str, Any]] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.prerequisite = prerequisite
        self.checks = checks or []


@dataclass
class DuoCheck:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class DuoAvailability:
    available: bool
    reason: str
    prerequisite: str
    checks: list[DuoCheck] = field(default_factory=list)
    gitlab_version: str = ""
    flow_definition: str = ""
    flow_config_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "missing_prerequisite": self.prerequisite,
            "checks": [c.to_dict() for c in self.checks],
            "gitlab_version": self.gitlab_version,
            "flow_definition": self.flow_definition,
            "flow_config_path": self.flow_config_path,
            "engine_used_when_unavailable": EngineKind.LOCAL_DETERMINISTIC.value,
        }


def _parse_version(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in text.split("-")[0].split(".")[:3]:
        digits = "".join(c for c in chunk if c.isdigit())
        if digits:
            parts.append(int(digits))
    return tuple(parts)


@dataclass
class DuoFlowEvent:
    """A normalised event from a Duo session."""

    seq: int
    event_type: str
    agent_role: AgentRole
    status: str
    message: str
    payload: dict[str, Any]
    occurred_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_type": self.event_type,
            "agent_role": self.agent_role.value,
            "status": self.status,
            "message": self.message,
            "payload": self.payload,
            "occurred_at": self.occurred_at,
        }


# Map Duo flow component names (from our flow.yaml) onto audit agent roles.
COMPONENT_TO_ROLE = {
    "fetch_change_context": AgentRole.ORCHESTRATOR,
    "change_investigator": AgentRole.CHANGE_INVESTIGATOR,
    "repair_designer": AgentRole.REPAIR_DESIGNER,
    "human_patch_approval": AgentRole.ORCHESTRATOR,
    "verification_agent": AgentRole.VERIFICATION_AGENT,
    "publish_audit_summary": AgentRole.REPORTING_SERVICE,
}


class DuoAgentPlatformClient:
    """Thin, honest client over the GitLab Duo Flows API."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.base_url = self.settings.gitlab_url.rstrip("/")
        self.token = self.settings.gitlab_token
        self.timeout = self.settings.gitlab_timeout_seconds

    # ------------------------------------------------------------------ http
    def _headers(self) -> dict[str, str]:
        return {
            "PRIVATE-TOKEN": self.token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ai-engineering-change-auditor/1.0",
        }

    def _call(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        url = f"{self.base_url}/api/{self.settings.gitlab_api_version}{path}"
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                resp = client.request(
                    method, url, headers=self._headers(), json=payload, params=params
                )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise DuoUnavailable(
                f"could not reach GitLab at {self.base_url}: {type(exc).__name__}",
                prerequisite="A reachable GitLab instance with Duo Agent Platform enabled",
            ) from exc
        body: Any = None
        if resp.content:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text[:500]
        return resp.status_code, body

    # ----------------------------------------------------------------- probe
    def probe(self) -> DuoAvailability:
        """Determine whether the Duo Agent Platform can actually be used."""
        checks: list[DuoCheck] = []
        flow_def = self.settings.duo_flow_definition
        cfg_path = self.settings.duo_flow_config_path

        if not self.settings.is_live_gitlab:
            checks.append(
                DuoCheck(
                    "gitlab_configured",
                    False,
                    "GITLAB_URL and/or GITLAB_TOKEN are not set in this environment",
                )
            )
            return DuoAvailability(
                available=False,
                reason=(
                    "GitLab credentials are not configured in this environment, so the "
                    "Duo Agent Platform cannot be invoked. The audit ran on the local "
                    "deterministic engine and is labelled as such."
                ),
                prerequisite=(
                    "Set GITLAB_URL and GITLAB_TOKEN (PAT with `api` scope) for a "
                    "Premium/Ultimate instance with GitLab Duo and the Agent Platform "
                    "turned on, then set DUO_ENABLED=true."
                ),
                checks=checks,
                flow_definition=flow_def,
                flow_config_path=cfg_path,
            )

        if not self.settings.duo_enabled:
            checks.append(
                DuoCheck("duo_enabled", False, "DUO_ENABLED is false in the configuration")
            )
            return DuoAvailability(
                available=False,
                reason="DUO_ENABLED is false; the Duo Agent Platform integration is switched off.",
                prerequisite="Set DUO_ENABLED=true once the flow is registered in GitLab.",
                checks=checks,
                gitlab_version="",
                flow_definition=flow_def,
                flow_config_path=cfg_path,
            )
        checks.append(DuoCheck("duo_enabled", True, "DUO_ENABLED=true"))

        status, body = self._call("GET", "/version")
        if status != 200:
            checks.append(
                DuoCheck("instance_reachable", False, f"GET /version returned HTTP {status}")
            )
            return DuoAvailability(
                available=False,
                reason=f"Could not read the GitLab instance version (HTTP {status}).",
                prerequisite=(
                    "A PAT with `api` scope that can reach this instance. "
                    f"Response: {str(sanitize_value(body))[:200]}"
                ),
                checks=checks,
                flow_definition=flow_def,
                flow_config_path=cfg_path,
            )
        version = str((body or {}).get("version", ""))
        checks.append(DuoCheck("instance_reachable", True, f"GitLab {version}"))

        parsed = _parse_version(version)
        if parsed and parsed < MIN_VERSION_PLATFORM:
            checks.append(
                DuoCheck(
                    "version_supports_agent_platform",
                    False,
                    f"GitLab {version} predates the Agent Platform (needs >= "
                    f"{'.'.join(map(str, MIN_VERSION_PLATFORM))})",
                )
            )
            return DuoAvailability(
                available=False,
                reason=f"GitLab {version} does not support the Duo Agent Platform.",
                prerequisite=f"GitLab >= {'.'.join(map(str, MIN_VERSION_PLATFORM))} (>= "
                f"{'.'.join(map(str, MIN_VERSION_FLOWS_API_GA))} recommended for the GA Flows API).",
                checks=checks,
                gitlab_version=version,
                flow_definition=flow_def,
                flow_config_path=cfg_path,
            )
        checks.append(
            DuoCheck(
                "version_supports_agent_platform",
                True,
                f"GitLab {version} >= {'.'.join(map(str, MIN_VERSION_PLATFORM))}",
            )
        )
        if parsed and parsed < MIN_VERSION_FLOWS_API_GA:
            checks.append(
                DuoCheck(
                    "flows_api_ga",
                    False,
                    f"GitLab {version}: the Flows API is GA only from "
                    f"{'.'.join(map(str, MIN_VERSION_FLOWS_API_GA))}; calls may 404 or behave differently",
                )
            )
        else:
            checks.append(DuoCheck("flows_api_ga", True, "Flows API is GA on this version"))

        # Probe the flows endpoint. 401/403 => entitlement/permission problem,
        # 404 => route missing (version or tier), 200 => usable.
        probe_status, probe_body = self._call("GET", "/ai/duo_workflows/agent_privileges")
        if probe_status == 200:
            checks.append(DuoCheck("flows_api_reachable", True, "GET /ai/duo_workflows/agent_privileges -> 200"))
        elif probe_status in (401, 403):
            checks.append(
                DuoCheck("flows_api_reachable", False, f"HTTP {probe_status} on the Flows API")
            )
            return DuoAvailability(
                available=False,
                reason=f"The Flows API rejected this token (HTTP {probe_status}).",
                prerequisite=(
                    "GitLab Premium/Ultimate with GitLab Duo and the Agent Platform turned on "
                    "for the top-level group, a project in a GROUP namespace, and "
                    "Developer/Maintainer/Owner role for the token owner."
                ),
                checks=checks,
                gitlab_version=version,
                flow_definition=flow_def,
                flow_config_path=cfg_path,
            )
        else:
            # Try the workflow-collection route before giving up: some versions
            # expose the resource but not the privileges sub-route.
            alt_status, _ = self._call("GET", "/ai/duo_workflows/workflows")
            if alt_status in (200, 400, 405):
                checks.append(
                    DuoCheck(
                        "flows_api_reachable",
                        True,
                        f"agent_privileges -> {probe_status}, workflows -> {alt_status} (route present)",
                    )
                )
            else:
                checks.append(
                    DuoCheck(
                        "flows_api_reachable",
                        False,
                        f"agent_privileges -> {probe_status}, workflows -> {alt_status}",
                    )
                )
                return DuoAvailability(
                    available=False,
                    reason=(
                        f"The Duo Flows API is not exposed by this instance "
                        f"(HTTP {probe_status}/{alt_status})."
                    ),
                    prerequisite=(
                        "Confirm GitLab Duo Agent Platform is enabled and the instance "
                        "version/tier exposes /api/v4/ai/duo_workflows."
                    ),
                    checks=checks,
                    gitlab_version=version,
                    flow_definition=flow_def,
                    flow_config_path=cfg_path,
                )

        return DuoAvailability(
            available=True,
            reason="Duo Agent Platform Flows API is reachable and authorised.",
            prerequisite="",
            checks=checks,
            gitlab_version=version,
            flow_definition=flow_def,
            flow_config_path=cfg_path,
        )

    # --------------------------------------------------------------- trigger
    def trigger_flow(
        self,
        *,
        project_id: int | str,
        goal: str,
        merge_request_iid: int | None = None,
        additional_context: list[dict[str, str]] | None = None,
        start: bool = True,
        client_reference: str = "",
        callback_hook_id: int | None = None,
    ) -> dict[str, Any]:
        """Create and start an audit flow session.

        ``workflow_definition`` is used when the flow is registered under a
        definition reference; ``ai_catalog_item_consumer_id`` takes precedence
        when configured, per the documented attribute rules.
        """
        availability = self.probe()
        if not availability.available:
            raise DuoUnavailable(
                availability.reason,
                prerequisite=availability.prerequisite,
                checks=[c.to_dict() for c in availability.checks],
            )

        body: dict[str, Any] = {
            "project_id": str(project_id),
            "goal": goal,
            "environment": self.settings.duo_environment,
            "start_workflow": start,
            "allow_agent_to_request_user": False,
            "source": "ai_engineering_change_auditor",
        }
        if merge_request_iid is not None:
            body["merge_request_id"] = merge_request_iid
        if self.settings.duo_ai_catalog_item_consumer_id:
            body["ai_catalog_item_consumer_id"] = self.settings.duo_ai_catalog_item_consumer_id
        else:
            body["workflow_definition"] = self.settings.duo_flow_definition
        if self.settings.duo_privilege_ids:
            body["agent_privileges"] = self.settings.duo_privilege_ids
        if self.settings.duo_pre_approved_ids:
            body["pre_approved_agent_privileges"] = self.settings.duo_pre_approved_ids
        if additional_context:
            body["additional_context"] = additional_context
        if client_reference:
            body["client_reference"] = client_reference[:255]
        if callback_hook_id:
            body["callback_hook_id"] = callback_hook_id

        status, resp = self._call("POST", "/ai/duo_workflows/workflows", payload=body)
        if status not in (200, 201):
            raise DuoUnavailable(
                f"Failed to start the Duo flow (HTTP {status}).",
                prerequisite=(
                    "The flow must be registered in the AI Catalog (or referenced by "
                    "workflow_definition), and when "
                    "`enforce_composite_identity_for_api_started_workflows` is enabled the flow "
                    "needs a composite identity service account. "
                    f"Response: {str(sanitize_value(resp))[:300]}"
                ),
            )
        data = resp if isinstance(resp, dict) else {"raw": sanitize_value(resp)}
        logger.info("duo flow started: id=%s status=%s", data.get("id"), data.get("status"))
        return {"request_body_keys": sorted(body), "response": sanitize_value(data)}

    # ----------------------------------------------------------------- poll
    def get_session(self, workflow_id: int | str) -> dict[str, Any]:
        status, resp = self._call("GET", f"/ai/duo_workflows/workflows/{workflow_id}")
        if status != 200:
            raise DuoUnavailable(
                f"Could not read Duo session {workflow_id} (HTTP {status}).",
                prerequisite="A session id returned by a successful trigger call.",
            )
        return sanitize_value(resp if isinstance(resp, dict) else {"raw": resp})

    def get_events(self, workflow_id: int | str) -> list[dict[str, Any]]:
        status, resp = self._call("GET", f"/ai/duo_workflows/workflows/{workflow_id}/events")
        if status != 200:
            logger.warning("could not read duo events for %s (HTTP %s)", workflow_id, status)
            return []
        events = resp if isinstance(resp, list) else (resp or {}).get("events", [])
        return [sanitize_value(e) for e in events if isinstance(e, dict)]

    def poll_until_complete(
        self,
        workflow_id: int | str,
        *,
        on_event: Any = None,
        timeout_seconds: float | None = None,
        interval_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Poll a session until it reaches a terminal state.

        Terminal states per the documented ``status`` values: ``finished``,
        ``failed``, ``stopped``.  ``input_required``,
        ``plan_approval_required`` and ``tool_call_approval_required`` are
        *pauses* -- polling stops and the caller must decide, because an
        approval gate must never be auto-satisfied.
        """
        timeout = timeout_seconds or self.settings.duo_poll_timeout_seconds
        interval = interval_seconds or self.settings.duo_poll_interval_seconds
        terminal = {"finished", "failed", "stopped"}
        paused = {"input_required", "plan_approval_required", "tool_call_approval_required"}
        deadline = time.monotonic() + timeout
        seen = 0
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.get_session(workflow_id)
            state = str(last.get("status", ""))
            events = self.get_events(workflow_id)
            for ev in events[seen:]:
                seen += 1
                if on_event:
                    on_event(normalise_event(ev, seq=seen))
            if state in terminal or state in paused:
                return {"status": state, "session": last, "events_seen": seen, "paused": state in paused}
            time.sleep(interval)
        return {
            "status": "timeout",
            "session": last,
            "events_seen": seen,
            "paused": False,
            "note": f"polling stopped after {timeout}s without a terminal state",
        }

    # ------------------------------------------------------------- approval
    def send_input(
        self,
        workflow_id: int | str,
        *,
        action: str,
        message: str = "",
    ) -> dict[str, Any]:
        """Resume a paused session (approve / reject / modify).

        Uses the documented resume route.  It is marked experimental on some
        GitLab versions, so the outcome is recorded verbatim rather than assumed.
        """
        if action not in {"approve", "reject", "modify"}:
            raise ValueError(f"unsupported resume action: {action!r}")
        payload = {"user_inputs": [{"action": action, "message": message}]} if message else {"action": action}
        status, resp = self._call(
            "POST", f"/ai/duo_workflows/workflows/{workflow_id}/resume", payload=payload
        )
        return {
            "http_status": status,
            "action": action,
            "accepted": status in (200, 201, 202),
            "response": sanitize_value(resp),
            "note": (
                "the resume route is marked experimental on some GitLab versions"
                if status >= 400
                else ""
            ),
        }


def normalise_event(raw: dict[str, Any], *, seq: int = 0) -> DuoFlowEvent:
    """Map a raw Duo session event onto an audit AgentEvent."""
    component = str(raw.get("component_name") or raw.get("component") or raw.get("agent") or "")
    etype = str(raw.get("event_type") or raw.get("type") or "duo_event")
    role = COMPONENT_TO_ROLE.get(component, AgentRole.ORCHESTRATOR)
    message = str(
        raw.get("message")
        or raw.get("content")
        or raw.get("final_answer")
        or raw.get("summary")
        or ""
    )
    status = "info"
    low = f"{etype} {message}".lower()
    if "fail" in low or "error" in low or "abort" in low:
        status = "error"
    elif "success" in low or "final_answer" in low or "complet" in low:
        status = "success"
    payload = {k: sanitize_value(v) for k, v in raw.items() if k not in {"message", "content"}}
    return DuoFlowEvent(
        seq=seq,
        event_type=etype,
        agent_role=role,
        status=status,
        message=message[:2000],
        payload=payload,
        occurred_at=str(raw.get("created_at") or raw.get("timestamp") or ""),
    )


def build_audit_goal(
    *,
    project_path: str,
    merge_request_iid: int | None,
    source_sha: str,
    target_sha: str,
    changed_files: list[str],
) -> str:
    """Compose the flow goal. Repository content is never interpolated raw."""
    files = ", ".join(changed_files[:20]) or "(none)"
    return (
        "Run the engineering change audit flow on this merge request. "
        f"Project: {project_path}. "
        + (f"Merge request IID: {merge_request_iid}. " if merge_request_iid else "")
        + f"Source commit: {source_sha}. Target commit: {target_sha}. "
        f"Changed files: {files}. "
        "Produce structured JSON findings, each with file path, line range, evidence "
        "citations, expected behaviour and suspected behaviour. Mark anything you "
        "cannot support with repository evidence as inconclusive. Do not claim a "
        "defect is fixed without executed test results."
    )


def build_additional_context(audit_public_id: str, extra: dict[str, Any] | None = None) -> list[dict[str, str]]:
    payload = {"audit_id": audit_public_id, **(extra or {})}
    return [{"Category": "audit_context", "Content": json.dumps(payload, default=str)}]


__all__ = [
    "COMPONENT_TO_ROLE",
    "DuoAgentPlatformClient",
    "DuoAvailability",
    "DuoCheck",
    "DuoFlowEvent",
    "DuoUnavailable",
    "build_additional_context",
    "build_audit_goal",
    "normalise_event",
]
