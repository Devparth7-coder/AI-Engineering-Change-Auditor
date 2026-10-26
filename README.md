# AI Engineering Change Auditor

**Can we trust a proposed code change, and what evidence demonstrates that it
works?**

The auditor investigates a GitLab merge-request diff, turns AI hypotheses into
evidence-backed findings, reproduces defects by *executing* generated tests
against the original revision, proposes minimal patches, refuses to verify
anything until a human approves, and then verifies the fix by executing a
four-run pytest matrix in an isolated workspace. A generated patch is never
reported as a fix.

```
AI hypothesis → evidence-backed finding → reproduced defect
              → proposed fix → human approval → VERIFIED BY EXECUTION
              → inconclusive (and labelled as such, loudly)
```

## Quickstart (no GitLab needed — labelled demo mode)

```bash
# backend (Python 3.12+, git required)
pip install -r backend/requirements.txt
cd backend && PYTHONPATH=. python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# frontend (Node 20+)
cd frontend && npm ci && npm run build && npx next start -H 0.0.0.0 -p 3000
# open http://localhost:3000
```

Startup prints an honest capability line, e.g.
`env=development db=sqlite isolation=linux_namespace gitlab=not configured
(demo mode available) duo=disabled demo=ready`. With no credentials the bundled
fixture repository (`examples/discount-service`, a known injected defect whose
own 16 tests still pass) is audited end-to-end and every screen labels the run
`local_deterministic`.

### Docker / PostgreSQL (production target)

```bash
docker compose up --build     # postgres:16 + backend + frontend
```

Migrations are Alembic; models use JSONB on PostgreSQL and fall back to SQLite
for zero-dependency development.

## GitLab Duo Agent Platform (mandatory integration)

* `agents/flows/change-audit/flow.yaml` — real Flow Registry v1 flow: read-only
  reader, investigators, deterministic verification step, `HumanInputComponent`
  approval gate, end/abort components; `environment: ambient`; least-privilege
  privileges (never `read_write_gitlab`).
* `.gitlab/duo/agent-config.yml` — execution image, setup, network policy.
* `backend/app/services/duo.py` — real Flows API client (start, status polling
  across the documented enum, resume on human input) behind an availability
  probe.
* **Honesty contract:** without a Premium/Ultimate instance and credentials the
  product states the missing prerequisite verbatim (`GET /api/duo/availability`),
  runs the clearly-labelled local deterministic engine, and never simulates or
  substitutes a Duo result. See `agents/README.md`.

## The eight-step workflow

1. connect GitLab project + MR (or labelled local fixture) — SHAs pinned
2. analyse the change (deterministic AST engine, 7 rules, contract-aware)
3. investigate — every evidence item independently corroborated/refuted
4. generate reproduction tests from the declared contract
5. reproduce by execution against the **original** revision, network off
6. propose a minimal patch with side-effect analysis (nothing applied yet)
7. explicit human approval gate (verify-before-approve = HTTP 409)
8. verify by execution: baseline reproduction fails, baseline regression
   passes, post-fix reproduction passes, post-fix regression passes →
   `verified_for_tested_behavior` **with a scope statement**
9. report with explained outcome, remaining risks and its own limitations;
   everything persists across restarts

## API (all mandated routes)

`GET /api/health` · `GET /api/projects` · `POST /api/audits` ·
`GET /api/audits` · `GET /api/audits/{id}` · `POST /api/audits/{id}/start` ·
`GET /api/audits/{id}/events` · `GET /api/audits/{id}/findings` ·
`GET /api/findings/{id}` · `POST /api/findings/{id}/tests` ·
`POST /api/findings/{id}/patches` · `POST /api/patches/{id}/approve` ·
`POST /api/patches/{id}/reject` · `POST /api/patches/{id}/verify` ·
`GET /api/audits/{id}/report` — plus dashboard, merge-requests, test-runs,
settings and duo-availability endpoints. Audits execute asynchronously on a
worker pool; clients poll events. The browser never holds GitLab credentials:
the Next server proxies `/api/*`.

## UI

Eight screens, dark engineering dashboard: Dashboard, Project Explorer, Audit
Workspace (live event trail), Findings Explorer (evidence with independent
checks), Patch Review (diff + side effects + decision record), Verification
Results (runs bound to SHAs and isolation tier), Audit History, Settings.
Real polling, real confirm dialogs, loading/empty/error states, no simulated
progress and no fabricated statistics.

## Demo video

`demo/ai-engineering-change-auditor-live-demo.mp4` (2:41) is a narrated **live
product demonstration** recorded by `demo/record_walkthrough.py` (Playwright
against the running stack; the recorder aborts if the UI ever fails to show
what a chapter claims). `docs/demo-script.md` carries the timeline; the earlier
API-driven capture remains as an appendix.

## Tests, acceptance, benchmark

```bash
cd backend && python3 -m pytest tests -q                 # unit tests (45)
cd backend && PYTHONPATH=. python3 ../tests/acceptance_e2e.py   # 14 stages, 91 checks
cd backend && PYTHONPATH=. python3 ../evaluation/run_benchmark.py  # §10 benchmark
python3 agents/validate_flow.py                          # Duo flow validation
```

Latest **measured** benchmark (7-case corpus, `docs/evaluation.md`):
defect recall 0.75 (one documented rule-scope miss), finding precision 1.0,
reproduction rate 1.0 of executed reproductions, patch verification rate 1.0,
regression rate 0.0, false-positive rate 0.0, 0 LLM credits. Numbers are
re-measured on every run and stored under `evaluation/results/`.

## CI

`.gitlab-ci.yml`: deps → lint → backend tests → frontend tests → integration
(acceptance) → **agent workflow validation** (fails with exit 2 when the flow
config is absent, exit 1 when malformed, and asserts those failure modes) →
build (Docker) → optional manual deploy (fails loudly when unconfigured).

## Security

Isolated execution (Docker → linux namespaces → rlimits, probed and reported
honestly), no network by default, scrubbed child environments, sanitized logs,
rate limiting, server-side authorization, least-privilege GitLab/Duo scopes,
injection screening and untrusted-content fencing, workspace cleanup, and an
explicit statement that namespace/rlimit isolation is **not** a hard security
boundary. The MVP never pushes to a default branch, merges, or deploys.
Details: `docs/security.md`.

## Repository layout

```
frontend/  backend/  agents/  examples/  evaluation/  docs/  tests/
.gitlab/   .gitlab-ci.yml  AGENTS.md  Dockerfile  docker-compose.yml
LICENSE    README.md  .env.example
```

## Documentation map

`docs/architecture.md` · `docs/security.md` · `docs/evaluation.md` ·
`docs/demo-script.md` · `agents/README.md` · `AGENTS.md`

## License

MIT — Copyright (c) 2026 Dev Parth. See `LICENSE`.
