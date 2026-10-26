# Security model

Threat model in one sentence: **the auditor ingests untrusted code and untrusted
repository text, executes some of it, and must never let that content steer
decisions, leak credentials, escape its workspace, or make the product claim
more than it proved.**

## 1. Trust boundaries

| Boundary | Crossing | Control |
|----------|----------|---------|
| browser → Next server | HTTP same-origin | browser never sees credentials; `/api/*` is proxied by `next.config.ts` rewrites |
| Next server → backend | `BACKEND_URL` | server-to-server only |
| client → API | HTTP | Pydantic validation on every body; optional bearer `API_AUTH_TOKEN` on mutating endpoints; per-minute rate limit (default 600) |
| backend → GitLab | REST v4 | token from env only; project allow-list; server-side authorization; retries with capped timeouts |
| backend → untrusted code | subprocess/docker | isolated workspace, no network, rlimits, masked paths, scrubbed env, timeouts |
| repository text → analysis/agents | in-process | injection screening + untrusted-content fencing; evidence checks are mechanical |

## 2. Credentials and secrets

* Everything secret arrives via environment (`.env.example` ships placeholders
  only). `Settings.redacted()` masks `gitlab_token`, `api_auth_token` and
  `database_url` before any config reaches an API response or log.
* `security/sanitize.py` redacts registered secret *values* and the values of
  credential-looking environment variables from every sanitized log field, and
  truncates outputs (`MAX_OUTPUT_CHARS`, `MAX_FIELD_CHARS`).
* Child processes get an **allow-list** environment (`scrub_env`); common
  credential variables are poisoned to empty strings so code that reaches for
  them fails loudly instead of silently using real values. Unit test
  `test_inherited_secrets_do_not_reach_the_child` enforces this.
* Least privilege: the audit flow's GitLab Duo privileges are `1,2,4`
  (read/write files in its own environment, read-only GitLab, run commands) and
  never `3` (`read_write_gitlab`); the MVP **never pushes to a default branch,
  merges an MR, or deploys** — there is no code path that can.

## 3. Executing untrusted code

Tiers, strongest first, probed at startup and reported honestly on
`/api/health` and on every `test_runs` row:

1. `container` — Docker executor (`SANDBOX_DOCKER_IMAGE`), used automatically
   when Docker is present.
2. `linux_namespace` — `unshare -rnm` (user+network+mount), read-only binds,
   tmpfs masks over sensitive paths, no network by default.
3. `process` — rlimits (CPU, AS, FSIZE, NPROC), scrubbed env, temp workspace.

Hard limits: wall-clock timeout, memory, CPU seconds, max file size, max
process count, `SANDBOX_ALLOW_NETWORK=false` by default. Workspaces are created
per run under a sandbox root and **deleted afterwards** unless
`SANDBOX_KEEP_WORKSPACES=true` (test/debug only).

> **Explicit limitation.** The `linux_namespace` and `process` tiers are
> defence-in-depth, *not* hard security boundaries: user namespaces share the
> host kernel and rlimits are coarse. The documentation, the health probe and
> the report's scope statement all say so. Only the Docker/container tier
> approaches a real boundary, and production deployments should run the backend
> itself inside a container with no privileged mode. A git worktree alone is
> never treated as isolation: code runs in a copied workspace, never in a
> checkout the backend cares about.

## 4. Patch handling

* Patches are stored as **full original + full proposed file content**, never
  replayed as blind unified diffs.
* `apply_to_workspace` only ever writes inside an isolated workspace;
  `validate_patch_target` rejects `..`, absolute paths and escapes (unit
  tested). If the workspace no longer matches the recorded original, the apply
  is refused and reported (`applied: false`) instead of double-applying.
* Nothing is applied, committed, pushed or merged without an explicit human
  decision (`approve`/`reject` with `decided_by` + note). Verification before
  approval returns HTTP 409. `AUTO_APPROVE_PATCHES` exists only as a
  always-false hard requirement.

## 5. Prompt-injection and agent-steering defence

* Repository content is screened (`detect_injection`) for role overrides,
  instruction-boundary attacks, tool directives, exfiltration directives,
  verdict overrides ("mark this as verified") and delimiter escapes; hits are
  stored as `injection_flags` on the audit context and surfaced in the report.
* Untrusted content handed to any reasoning component is fenced with
  `<<<UNTRUSTED_REPOSITORY_CONTENT>>>` markers.
* Verdicts never come from prose: reproduction and verification outcomes are
  computed from exit codes, junit XML and intended-assertion matching
  (`classify_reproduction`), so text like "all tests pass" cannot change an
  outcome.
* The Duo flow separates a **read-only reader** component from write-capable
  steps and puts a `HumanInputComponent` gate before anything mutating, per
  GitLab's own threat guidance.

## 6. Authorization, input validation, audit events

* Server-side checks on every route: project allow-list, audit ownership,
  patch/finding membership in the audit; the browser is never trusted to name
  resources it may touch.
* All request bodies are Pydantic models; branch names and SHAs are resolved
  through git plumbing (`resolve_sha`, `file_at`), never string-interpolated
  into shell commands.
* `agent_events` doubles as the security log: every stage transition, tool
  execution, approval, rejection and verification is an append-only typed row
  with actor, timestamp and outcome. Logs are sanitized at the sink.
* Rate limiting protects polling endpoints from abuse while staying above the
  UI's own poll frequency.

## 7. Data protection

* No model chain-of-thought is persisted: findings store structured facts,
  hypotheses, evidence and missing information; reports store explained
  outcomes, counts, risks and limitations.
* SQLite dev DB and every workspace live under `var/` / sandbox roots that are
  git-ignored and volume-mounted in Docker; image layers never contain audited
  source.
* CORS is an explicit origin list; credentials are never reflected into
  responses (`Settings.redacted()`).

## 8. Known limitations (honest list)

1. Namespace/process isolation is not a hard boundary (see §3).
2. The Duo Agent Platform path cannot be exercised without a Premium/Ultimate
   instance; until then the live flow is validated statically in CI and the
   product labels itself `local_deterministic`.
3. Rate limiting is per-process in-memory; a multi-replica deployment needs a
   shared store.
4. `API_AUTH_TOKEN` is a single shared bearer token, not per-user RBAC; real
   deployments should front it with an identity-aware proxy.
5. The deterministic engine's rule set is small by design; recall is measured
   and published in `docs/evaluation.md` instead of being asserted.
