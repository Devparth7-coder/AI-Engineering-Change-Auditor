#!/usr/bin/env python3
"""Validate the Duo Agent Platform flow definition against the documented schema.

CI runs this in the *agent workflow validation* stage.  It fails loudly when the
flow YAML is malformed, when a component references a prompt that does not exist,
when the human approval gate is wired incorrectly, or when a component has been
granted a GitLab write tool that this project forbids.

It does NOT claim the flow is registered or executable -- that requires a GitLab
instance with Duo Agent Platform enabled, which is checked separately by
``GET /api/duo/availability``.

Usage:
    python3 agents/validate_flow.py [--flow PATH] [--agent-config PATH] [--strict]

Exit codes: 0 = valid, 1 = errors found, 2 = file missing/unparsable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    print("ERROR: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FLOW = REPO_ROOT / "agents" / "flows" / "change-audit" / "flow.yaml"
DEFAULT_AGENT_CONFIG = REPO_ROOT / ".gitlab" / "duo" / "agent-config.yml"

VALID_COMPONENT_TYPES = {
    "AgentComponent",
    "OneOffComponent",
    "DeterministicStepComponent",
    "HumanInputComponent",
    "SupervisorComponent",
    "EndComponent",
    "AbortComponent",
}

# Tool names from https://docs.gitlab.com/user/duo_agent_platform/agents/tools/
KNOWN_TOOLS = {
    "add_merge_request_reviewers", "add_new_task", "build_review_merge_request_context",
    "ci_linter", "confirm_vulnerability", "create_branch", "create_commit", "create_epic",
    "create_file_with_contents", "create_issue", "create_issue_note", "create_merge_request",
    "create_merge_request_diff_note", "create_merge_request_note", "create_plan",
    "create_vulnerability_issue", "create_work_item", "create_work_item_note",
    "dismiss_vulnerability", "edit_file", "extract_lines_from_text", "find_files",
    "get_commit", "get_commit_comments", "get_commit_diff", "get_current_user",
    "get_downstream_pipelines", "get_epic", "get_epic_note", "get_failing_bridge_jobs",
    "get_issue", "get_issue_note", "get_job_logs", "get_merge_request",
    "get_pipeline_errors", "get_pipeline_failing_jobs", "get_plan", "get_project",
    "get_repository_file", "get_repository_files", "get_security_finding_details",
    "get_session_context", "get_vulnerability_details", "get_wiki_page", "get_work_item",
    "get_work_item_notes", "get_work_item_statuses", "gitlab_api_get", "gitlab_blob_search",
    "gitlab_commit_search", "grep", "list_dir", "list_merge_request_diffs",
    "list_repository_tree", "mkdir", "notify_me_when", "read_file", "read_files",
    "run_command", "update_merge_request",
}

# This project forbids agents from mutating GitLab state. See docs/security.md.
FORBIDDEN_TOOLS = {
    "create_commit",
    "create_branch",
    "create_merge_request",
    "update_merge_request",
    "add_merge_request_reviewers",
    "create_issue",
    "create_work_item",
    "confirm_vulnerability",
    "dismiss_vulnerability",
}

HUMAN_GATE_REQUIRED_EVENTS = {"on_user_input_prompt", "on_user_response"}
HUMAN_GATE_CONDITIONS = {"approve", "modify", "reject"}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.checks: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def ok(self, msg: str) -> None:
        self.checks.append(msg)

    def render(self) -> str:
        lines: list[str] = []
        for c in self.checks:
            lines.append(f"  \033[92mOK\033[0m      {c}")
        for w in self.warnings:
            lines.append(f"  \033[93mWARN\033[0m    {w}")
        for e in self.errors:
            lines.append(f"  \033[91mERROR\033[0m   {e}")
        return "\n".join(lines)


def _toolset_of(component: dict[str, Any]) -> list[str]:
    """Normalise a toolset, which may contain strings or single-key mappings."""
    out: list[str] = []
    for entry in component.get("toolset") or []:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict):
            out.extend(str(k) for k in entry)
    return out


def validate_flow(flow: dict[str, Any], rep: Report) -> None:
    if flow.get("version") != "v1":
        rep.error(f"version must be 'v1' (flow registry v1); found {flow.get('version')!r}")
    else:
        rep.ok("version is v1")

    env = flow.get("environment")
    if env != "ambient":
        rep.error(
            f"environment must be 'ambient' for custom flows; found {env!r}. "
            "'chat' and 'chat-partial' are restricted fields."
        )
    else:
        rep.ok("environment is ambient")

    coding_env = flow.get("coding_environment", "full")
    if coding_env not in ("full", "none"):
        rep.error(f"coding_environment must be 'full' or 'none'; found {coding_env!r}")
    else:
        rep.ok(f"coding_environment is '{coding_env}'")

    components = flow.get("components") or []
    if not isinstance(components, list) or not components:
        rep.error("components must be a non-empty list")
        return

    names: list[str] = []
    prompts = {p.get("prompt_id"): p for p in (flow.get("prompts") or []) if isinstance(p, dict)}
    rep.ok(f"{len(components)} component(s), {len(prompts)} prompt(s) declared")

    for idx, comp in enumerate(components):
        if not isinstance(comp, dict):
            rep.error(f"component #{idx} is not a mapping")
            continue
        name = comp.get("name")
        ctype = comp.get("type")
        if not name:
            rep.error(f"component #{idx} has no name")
            continue
        if name in names:
            rep.error(f"duplicate component name '{name}'")
        names.append(name)
        if ctype not in VALID_COMPONENT_TYPES:
            rep.error(f"component '{name}' has unsupported type {ctype!r}")
            continue

        if ctype == "AgentComponent":
            pid = comp.get("prompt_id")
            if not pid:
                rep.error(f"AgentComponent '{name}' is missing prompt_id")
            elif pid not in prompts:
                rep.error(
                    f"AgentComponent '{name}' references prompt_id '{pid}' which is not "
                    "defined in the flow's prompts section"
                )
            else:
                rep.ok(f"'{name}' -> prompt '{pid}' resolves")
            if comp.get("prompt_version") and pid in prompts:
                rep.warn(
                    f"'{name}' sets both prompt_version and a local prompt; prompt_version "
                    "is for catalogued prompts and will be ignored"
                )
        elif ctype == "DeterministicStepComponent":
            if not comp.get("tool_name"):
                rep.error(f"DeterministicStepComponent '{name}' is missing tool_name")
            else:
                rep.ok(f"'{name}' runs deterministic tool '{comp['tool_name']}'")
        elif ctype == "HumanInputComponent":
            itype = comp.get("interaction_type")
            if itype not in ("approval", "input"):
                rep.error(
                    f"HumanInputComponent '{name}' must set interaction_type explicitly "
                    f"('approval' or 'input'); found {itype!r}"
                )
            events = set(comp.get("ui_log_events") or [])
            missing = HUMAN_GATE_REQUIRED_EVENTS - events
            if missing:
                rep.error(
                    f"HumanInputComponent '{name}' is missing required ui_log_events "
                    f"{sorted(missing)}; without both the gate is invisible in the session UI"
                )
            else:
                rep.ok(f"'{name}' declares both required human-gate ui_log_events")
            srt = comp.get("sends_response_to")
            if not srt:
                rep.error(f"HumanInputComponent '{name}' is missing sends_response_to")
            elif srt not in names:
                rep.error(
                    f"HumanInputComponent '{name}' sends_response_to '{srt}', which has not "
                    "run before the gate fires (it must point at an EARLIER component)"
                )
            else:
                rep.ok(f"'{name}' sends its response to already-run component '{srt}'")

        tools = _toolset_of(comp)
        unknown = [t for t in tools if t not in KNOWN_TOOLS]
        if unknown:
            rep.error(
                f"component '{name}' requests unknown tool(s) {sorted(unknown)}; "
                "tool names must be snake_case names from the documented registry"
            )
        forbidden = [t for t in tools if t in FORBIDDEN_TOOLS]
        if forbidden:
            rep.error(
                f"component '{name}' requests forbidden GitLab write tool(s) {sorted(forbidden)}. "
                "This project never allows an agent to commit, branch, push, merge or "
                "otherwise mutate GitLab state."
            )
        if tools and not forbidden:
            rep.ok(f"'{name}' toolset is within the least-privilege policy ({len(tools)} tools)")

    # ---------------- routers ----------------
    routers = flow.get("routers")
    if not isinstance(routers, list):
        rep.error("routers must be a list")
        routers = []
    terminal = {"end", "abort"}
    targets: dict[str, list[dict[str, Any]]] = {}
    for r in routers:
        if not isinstance(r, dict):
            rep.error("each router entry must be a mapping")
            continue
        src, dst = r.get("from"), r.get("to")
        if src not in names:
            rep.error(f"router 'from' references unknown component {src!r}")
        if dst not in names and dst not in terminal:
            rep.error(f"router 'to' references unknown target {dst!r} (must be a component, 'end' or 'abort')")
        targets.setdefault(src, []).append(r)

    flow_section = flow.get("flow") or {}
    entry = flow_section.get("entry_point")
    if not entry:
        rep.error("flow.entry_point is required")
    elif entry not in names:
        rep.error(f"flow.entry_point '{entry}' is not a declared component")
    else:
        rep.ok(f"entry_point '{entry}' resolves")

    # Reachability from the entry point.
    if entry in names:
        seen: set[str] = set()
        stack = [entry]
        while stack:
            cur = stack.pop()
            if cur in seen or cur in terminal:
                continue
            seen.add(cur)
            for r in targets.get(cur, []):
                nxt = r.get("to")
                if nxt in names:
                    stack.append(nxt)
        unreachable = [n for n in names if n not in seen]
        if unreachable:
            rep.error(f"components unreachable from entry_point: {sorted(unreachable)}")
        else:
            rep.ok(f"all {len(names)} components are reachable from the entry point")
        if not any(r.get("to") in terminal for r in routers):
            rep.error("no router terminates the flow at 'end' or 'abort'")
        else:
            rep.ok("flow terminates")

    # Human gate routing must handle approve/modify/reject + default_route.
    for comp in components:
        if not isinstance(comp, dict) or comp.get("type") != "HumanInputComponent":
            continue
        name = comp.get("name")
        entries = targets.get(name, [])
        conditions = {e.get("condition") for e in entries if e.get("condition")}
        missing = HUMAN_GATE_CONDITIONS - conditions
        if missing:
            rep.error(
                f"router from human gate '{name}' does not handle {sorted(missing)} explicitly"
            )
        if not any(e.get("default_route") for e in entries):
            rep.error(f"router from human gate '{name}' has no default_route entry")
        if any(e.get("condition") and not e.get("to") for e in entries):
            rep.error(f"conditional router entries from '{name}' must still specify a 'to' target")
        if not missing and any(e.get("default_route") for e in entries):
            rep.ok(f"human gate '{name}' routes approve/modify/reject plus a default_route")

    # ---------------- prompts ----------------
    for pid, prompt in prompts.items():
        if not pid:
            rep.error("a prompt is missing prompt_id")
            continue
        tmpl = prompt.get("prompt_template") or {}
        if not tmpl.get("system"):
            rep.error(f"prompt '{pid}' has no prompt_template.system")
        if not tmpl.get("user"):
            rep.error(f"prompt '{pid}' has no prompt_template.user")
        if tmpl.get("system") and tmpl.get("user"):
            rep.ok(f"prompt '{pid}' defines system and user templates")
        params = prompt.get("params") or {}
        if "timeout" not in params:
            rep.warn(f"prompt '{pid}' sets no params.timeout; the platform default will apply")

    # ---------------- flow inputs ----------------
    inputs = flow_section.get("inputs") or []
    categories = [i.get("category") for i in inputs if isinstance(i, dict)]
    for comp in components:
        if not isinstance(comp, dict):
            continue
        for item in comp.get("inputs") or []:
            frm = item.get("from") if isinstance(item, dict) else item
            if isinstance(frm, str) and frm.startswith("context:inputs."):
                category = frm.split(".", 2)[1] if frm.count(".") >= 2 else ""
                if category and category not in categories:
                    rep.error(
                        f"component '{comp.get('name')}' reads context:inputs.{category}.* "
                        f"but flow.inputs declares no '{category}' category"
                    )
                elif not isinstance(item, dict) or not item.get("optional"):
                    rep.warn(
                        f"component '{comp.get('name')}' reads context:inputs.{category} "
                        "without optional: true; the flow errors when the value is absent"
                    )
    if categories:
        rep.ok(f"flow.inputs declares categories: {categories}")


def validate_agent_config(path: Path, rep: Report) -> None:
    if not path.is_file():
        rep.error(f"missing flow execution config: {path}")
        return
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        rep.error(f"{path} is not valid YAML: {exc}")
        return
    for key in ("image", "setup_script"):
        if key not in data:
            rep.warn(f"{path.name} does not set '{key}'")
        else:
            rep.ok(f"agent-config sets '{key}'")
    allowed = {"image", "setup_script", "cache", "network_policy"}
    unknown = set(data) - allowed
    if unknown:
        rep.warn(f"{path.name} has keys not in the documented set: {sorted(unknown)}")
    netpol = data.get("network_policy")
    if isinstance(netpol, dict):
        if "denied_domains" not in netpol and not netpol.get("include_recommended_allowed") is False:
            rep.warn("network_policy sets no denied_domains; egress is broader than necessary")
        else:
            rep.ok("network_policy restricts egress")


def validate_backend_alignment(flow: dict[str, Any], rep: Report) -> None:
    """Cross-check flow component names against the backend's role mapping."""
    backend = REPO_ROOT / "backend" / "app" / "services" / "duo.py"
    if not backend.is_file():
        rep.warn("backend/app/services/duo.py not found; cannot cross-check role mapping")
        return
    text = backend.read_text(encoding="utf-8")
    names = [c.get("name") for c in (flow.get("components") or []) if isinstance(c, dict)]
    unmapped = [n for n in names if n and f'"{n}"' not in text]
    if unmapped:
        rep.warn(
            f"flow component(s) {sorted(unmapped)} are not mapped in COMPONENT_TO_ROLE "
            "in backend/app/services/duo.py; their events will be attributed to "
            "'orchestrator'"
        )
    else:
        rep.ok("every flow component is mapped to an audit agent role in the backend")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--flow", type=Path, default=DEFAULT_FLOW)
    parser.add_argument("--agent-config", type=Path, default=DEFAULT_AGENT_CONFIG)
    parser.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    rep = Report()
    if not args.flow.is_file():
        print(f"ERROR: flow definition not found at {args.flow}", file=sys.stderr)
        print(
            "The agent workflow cannot be validated. This is a hard failure: CI must "
            "not report successful agent validation when the flow definition is absent.",
            file=sys.stderr,
        )
        return 2
    try:
        flow = yaml.safe_load(args.flow.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        print(f"ERROR: {args.flow} is not valid YAML: {exc}", file=sys.stderr)
        return 2
    if not isinstance(flow, dict):
        print(f"ERROR: {args.flow} did not parse to a mapping", file=sys.stderr)
        return 2

    print(f"Validating flow definition: {args.flow}")
    validate_flow(flow, rep)
    print(f"Validating execution config:  {args.agent_config}")
    validate_agent_config(args.agent_config, rep)
    validate_backend_alignment(flow, rep)

    if args.strict and rep.warnings:
        rep.errors.extend(f"(strict) {w}" for w in rep.warnings)
        rep.warnings = []

    if not args.quiet:
        print(rep.render())
    print(
        f"\n{len(rep.checks)} check(s) passed, {len(rep.warnings)} warning(s), "
        f"{len(rep.errors)} error(s)"
    )
    if rep.errors:
        print("\nFLOW VALIDATION FAILED", file=sys.stderr)
        return 1
    print("FLOW VALIDATION PASSED")
    print(
        "\nNOTE: passing validation means the YAML conforms to the documented Flow "
        "Registry v1 schema and this project's least-privilege policy. It does NOT "
        "mean the flow is registered or executable -- that requires a GitLab "
        "instance with Duo Agent Platform enabled. Check GET /api/duo/availability."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
