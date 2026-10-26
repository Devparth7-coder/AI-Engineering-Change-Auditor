# Architecture

The AI Engineering Change Auditor is a **modular monolith**: one FastAPI
backend that owns all persistence, execution and provenance, and one Next.js
dashboard that only talks to that backend. There are no microservices, no
message queues and no hidden side channels — the brief's eight-step workflow
maps onto stages inside a single orchestrator so that provenance stays in one
transactional database.

```
                        browser (Next.js dashboard, polls /api/*)
                                    │  HTTP, same origin
                                    ▼
                     Next.js server  ── rewrites /api/:path* ──
                                                               ▼
        ┌────────────────────────── FastAPI backend ──────────────────────────┐
        │  api/            thin HTTP layer: validation, authz, rate limit     │
        │    system projects audits findings patches  (15 mandated routes)   │
        │  services/                                                          │
        │    orchestrator   8-stage audit state machine (worker pool)         │
        │    repo_source    local git | GitLab REST (never in the browser)    │
        │    gitlab_client  REST v4 wrapper, retries, allow-list              │
        │    duo            Flow Registry v1 client + availability probe      │
        │    analysis/      deterministic AST engine: contracts + 7 rules     │
        │    investigate    independent corroboration/refutation of evidence  │
        │    testgen        contract-derived reproduction tests               │
        │    pytest_runner  junit parsing, intended-assertion classification  │
        │    patching       minimal patch drafts, containment checks          │
        │    verification   4-run execution matrix, scope statements          │
        │    reporting      explained outcomes, limitations, provenance       │
        │    sandbox        container | linux_namespace | process executors   │
        │    store          persistence helpers, event append                 │
        │  db/models        SQLAlchemy 2.x, JSONB on Postgres / JSON fallback  │
        └─────────────────────────────────────────────────────────────────────┘
                                    │
                    isolated workspaces (temp dirs, no network,
                    rlimits, masked paths; docker when available)
```

## 1. The eight-step workflow as orchestrator stages

`services/orchestrator.py` runs each audit through these stages on a thread
pool (`AUDIT_WORKER_CONCURRENCY`), never inside an HTTP request:

1. **connect** – resolve project + merge request (GitLab REST, or the labelled
   local demo fixture), pin `source_commit_sha` / `target_commit_sha`.
2. **analyse** – deterministic AST engine over the two revisions.
3. **investigate** – every evidence item is re-checked independently
   (`source_lines`, `docstring_quote`, `diff_mentions`, `analysis`, `repo_scan`);
   corroboration and refutation counts are stored, confidence is never invented.
4. **generate tests** – reproduction tests derived from the declared contract;
   findings without a mechanical test stay `inconclusive`, never "reproduced".
5. **reproduce** – execute the generated test against the *original* revision in
   an isolated workspace; only an intended-assertion failure counts.
6. **propose fix** – minimal patch draft carrying full original *and* proposed
   file content plus side-effect analysis; nothing is applied anywhere yet.
7. **human approval gate** – `POST /api/patches/{id}/approve|reject` with a
   recorded decision; verification before approval answers HTTP 409.
8. **verify by execution** – four pytest runs (baseline reproduction, baseline
   regression, post-fix reproduction, post-fix regression), then
   `verified_for_tested_behavior` *with a scope statement*, or an explicit
   failure/inconclusive outcome.
9. **report & persist** – explained outcome label, counts, remaining risks, the
   report's own limitations, and the full event trail. Survives restarts.

Every stage appends typed rows to `agent_events` (the provenance trail the UI
streams). Long audits are polled (`GET /api/audits/{id}` +
`GET /api/audits/{id}/events?after_seq=`); no request ever blocks on analysis.

## 2. Evidence model — the distinction the product exists for

```
AI hypothesis        FindingDraft from a rule: facts | hypotheses | missing info
evidence-backed      investigator corroborates each evidence item independently
reproduced defect    generated test executed on the ORIGINAL revision,
                     intended assertion failed (exit code + junit, not vibes)
proposed fix         PatchDraft: minimal, justified by the contract, analysed
verified fix         four executed pytest runs + human approval + scope statement
inconclusive         anything else — and it is labelled as such, loudly
```

`models.Finding.verification_status` and `repro_outcome` are separate columns
precisely so a generated patch can never be reported as a fix.

## 3. Persistence

Seven entities with provenance: `projects`, `audit_sessions`, `findings`,
`patch_proposals`, `test_runs`, `agent_events`, `audit_reports`. Migrations are
Alembic; column types use a `JSONVariant` that compiles to **JSONB on
PostgreSQL** and JSON on the SQLite development fallback. Model chain-of-thought
is never stored: rules emit structured facts/evidence, and the report stores
outcomes, counts, risks and limitations — not free-form reasoning transcripts.

## 4. Frontend

Next.js (App Router) + TypeScript + Tailwind, eight screens:
Dashboard, Project Explorer, Audit Workspace, Findings Explorer, Patch Review,
Verification Results, Audit History, Settings. The browser only ever calls the
Next server; `next.config.ts` rewrites `/api/:path*` to `BACKEND_URL`, so
credentials live server-side only. Every screen polls real endpoints
(`usePoll`), renders loading/empty/error states, and every button performs a
real mutating request behind a confirmation dialog. The production build is
what the demo video records.

## 5. GitLab Duo Agent Platform integration (mandatory)

* `agents/flows/change-audit/flow.yaml` — Flow Registry **v1**: read-only reader
  component, investigator components, a `HumanInputComponent` approval gate with
  approve/modify/reject routes, deterministic steps, end/abort components;
  `environment: ambient`; least-privilege `agent_privileges` (no
  `read_write_gitlab`).
* `.gitlab/duo/agent-config.yml` — execution image, setup script, cache and
  `network_policy` for the coding environment.
* `services/duo.py` — real Flows API client: `POST /ai/duo_workflows/workflows`
  with `start_workflow=true`, status polling across the documented enum
  (`created…tool_call_approval_required`), resume/input handling, and an
  **availability probe** (`GET /api/duo/availability`) that reports the missing
  prerequisite verbatim when credentials or platform support are absent.
* Honesty contract: with no credentials the audit runs on `LOCAL_ENGINE`
  (`local_deterministic`) and says so in the session panel, banner, history and
  report. Duo results are never simulated, and no external LLM is ever labelled
  as Duo.
* `agents/validate_flow.py` validates the YAML against the documented schema and
  this project's policy; CI fails (exit 2) when the configuration is absent and
  (exit 1) when it is malformed — a skipped validation can never look green.

## 6. Execution isolation

`services/sandbox.py` probes capabilities at startup and picks the strongest
available tier: `container` (Docker, used automatically when present) →
`linux_namespace` (`unshare -rnm`, read-only binds, tmpfs masks) → `process`
(rlimits + scrubbed env). The chosen tier is reported on `/api/health` and
recorded on every `test_runs` row. **A git worktree alone is not a security
boundary**, and neither is the namespace tier: see `docs/security.md`.

## 7. Repository layout

```
frontend/   Next.js dashboard (App Router, Tailwind, typed API client)
backend/    FastAPI app, services, analysis rules, migrations, unit tests
agents/     Flow Registry v1 YAML, validator, agent README
examples/   discount-service fixture (known defect, deterministic builder)
evaluation/ corpus + run_benchmark.py + results/
docs/       architecture, security, evaluation, demo script
tests/      mandatory end-to-end acceptance test (14 stages, 91 checks)
.gitlab/    duo/agent-config.yml
.gitlab-ci.yml, Dockerfile, docker-compose.yml, AGENTS.md, LICENSE, .env.example
```
