# GitLab Duo Agent Platform integration

This directory contains the **mandatory** Duo Agent Platform integration of the
AI Engineering Change Auditor: a real Flow Registry v1 definition, the execution
config for its coding environment, and an offline validator that CI runs.

```
agents/
  flows/change-audit/flow.yaml   Flow Registry v1 flow definition
  validate_flow.py               offline schema + policy validator (CI job)
  README.md                      this file
.gitlab/duo/agent-config.yml     execution image / setup / network policy
AGENTS.md                        repository instructions flows may consume
```

## 1. What the flow does

`flow.yaml` encodes the audit as components, following the documented
custom-flows schema (`version: 1`, `environment: ambient`):

1. **reader component (read-only)** — builds the review context with read-only
   tools only (`get_merge_request`, `list_merge_request_diffs`,
   `get_commit_diff`, `get_repository_file`, …). Separating read from write is
   deliberate, per GitLab's agent security guidance.
2. **investigator components** — corroborate or refute each candidate finding;
   their outputs are structured, never free-form verdicts.
3. **deterministic step** — runs the reproduction/verification matrix with fixed
   arguments and no LLM in the loop (`DeterministicStepComponent`).
4. **`HumanInputComponent` gate** — execution pauses; a human chooses
   **approve / modify / reject**. The downstream router handles all three plus
   a default route; `ui_log_events` includes both `on_user_input_prompt` and
   `on_user_response`; `sends_response_to` points at an already-run component.
5. **end / abort components** — explicit terminal states; abort on any
   integrity violation (injection flags, workspace drift, missing evidence).

Inputs are declared with `flow.inputs` categories and consumed via
`context:…` references; `AGENTS.md` is attached as an optional
`context:inputs.user_rule` so repository rules travel with the flow.

## 2. Privileges (least privilege, enforced twice)

`agent_privileges: [1, 2, 4]` — read/write files in the flow's own environment,
read-only GitLab, run commands. `read_write_gitlab` (3) is **never** granted:
the flow must not push to default branches, merge, or deploy. Pre-approved
privileges are limited to read-only GitLab (2); everything else requires
approval at runtime (`require_tool_approval: true`). The validator re-checks
this policy on every CI run.

## 3. Validating without GitLab (what CI does)

```bash
python3 agents/validate_flow.py \
    --flow agents/flows/change-audit/flow.yaml \
    --agent-config .gitlab/duo/agent-config.yml     # exit 0 = valid
python3 agents/validate_flow.py --flow /nonexistent.yaml   # exit 2 = missing
python3 agents/validate_flow.py --flow broken.yaml          # exit 1 = invalid
```

The validator checks schema shape (components, prompts, routers, flow graph
reachability, human-input checklist), tool names against the documented
read-only/write/IDE tool lists, privilege policy, and backend alignment (the
statuses and stages the backend orchestrator implements). **An absent or
malformed configuration fails the pipeline; it can never be reported as
success.** Passing validation does *not* mean the flow is registered or
executable — that requires a live instance (next section).

## 4. Going live (prerequisite, stated verbatim by the product)

> Set `GITLAB_URL` and `GITLAB_TOKEN` (PAT with `api` scope) for a
> Premium/Ultimate instance with GitLab Duo and the Agent Platform turned on,
> then set `DUO_ENABLED=true`.

With those set, `backend/app/services/duo.py` starts workflows through
`POST /api/v4/ai/duo_workflows/workflows` (`start_workflow=true`,
`environment=ambient`), polls the documented status enum (`created`, `running`,
`paused`, `input_required`, `plan_approval_required`,
`tool_call_approval_required`, `finished`, `failed`, …), resumes on human
input, and maps flow events onto the same `agent_events` provenance trail the
local engine uses. Note the platform's own constraints: API-started workflows
with `start_workflow=true` may require a composite-identity service account
(flag `enforce_composite_identity_for_api_started_workflows`), and custom flows
require a project in a group namespace with Developer+ role.

Without credentials, `GET /api/duo/availability` reports `available: false`
with the missing prerequisite named, audits run on the clearly-labelled
`local_deterministic` engine, and **no Duo result is ever simulated or
substituted by an external LLM**.

## 5. Relationship to the local engine

The deterministic AST engine (`backend/app/services/analysis/`) is *not* a Duo
stand-in: it is an independently testable analysis core that the product uses
when the platform is unreachable, labelled as such everywhere. When Duo is
available, the flow's deterministic step and the backend share the same
reproduction/verification semantics, so evidence produced by either path has
the same shape and the same honesty guarantees.
