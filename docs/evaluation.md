# Evaluation & reproducible benchmark

This document defines the benchmark required by §10 of the project brief, how to
re-run it, and the **measured** results of the latest executed run. No accuracy
percentage anywhere in this repository is fabricated or estimated: every number
below was produced by `evaluation/run_benchmark.py` executing real audits, real
pytest runs and real verifications against the committed corpus.

## 1. Corpus

`evaluation/corpus/<case>/` holds seven cases. Each case is data, not code that
runs at import time:

```
corpus/<case>/
  base/      file tree at the target revision (main)
  change/    file tree at the source revision (feature/change)
  ground_truth.json   what was injected, independently of the engine
```

The harness materialises each case as a real two-branch git repository in a
temporary directory before auditing it, exactly like the bundled
`examples/discount-service` fixture.

| Case | Injected truth | Purpose |
|------|----------------|---------|
| `shipping-waiver` | `shipping_cost` returns the waiver portion instead of the remainder | net/adjustment semantics flip |
| `refund-penalty` | `refund_after_penalty` returns the penalty portion | same family, different domain/numbers |
| `ledger-fee` | `remaining_after_fee` treats a fraction as a percentage | value-level contract violation |
| `tax-gross` | `gross_price` returns the tax portion of an **additive/gross** contract | **known scope gap**: outside the v1 rule semantics classes; kept so the gap is measured, not hidden |
| `format-untested-change` | no defect; `format_price` behaviour changed with no test asserting it | coverage-gap finding (R-TEST-001), must not count as defect or FP |
| `clean-additive` | no defect; new documented + tested function | false-positive control |
| `clean-refactor` | no defect; helper extraction, behaviour unchanged | false-positive control |

Ground truth is written **before** looking at engine output and only describes
what was injected (`file`, `function`, expected vs injected expression). A
finding counts as defect-pointing when it cites the injected file and names the
injected function, regardless of which rule produced it.

## 2. Protocol

`evaluation/run_benchmark.py` (run from `backend/` with `PYTHONPATH=.`):

1. builds each case repository (git init, commit base on `main`, commit change
   on `feature/change`);
2. registers it as a `benchmark`-origin project row;
3. `POST /api/audits` with `auto_start`, polls `GET /api/audits/{id}` until
   terminal status (the same asynchronous path the UI uses);
4. collects findings; for cases with `fix_expected`, approves the patch as
   `benchmark-harness` and calls `POST /api/patches/{id}/verify`, executing the
   four-run verification matrix in isolated workspaces;
5. compares against ground truth and writes
   `evaluation/results/benchmark-<utc>.json` (git-ignored; this document quotes
   the measured summary).

The run uses the same isolation tier the host provides (`linux_namespace` here,
`container` when Docker exists) and the `local_deterministic` engine; the Duo
Agent Platform path is reported unavailable and is **not** benchmarked, because
benchmarking it would require credentials this environment does not have.

## 3. Metric definitions

| Metric | Formula |
|--------|---------|
| defect recall | injected defects with ≥1 defect-pointing critical/high finding ÷ injected defects |
| finding precision | defect-pointing findings on defective cases ÷ all defect-pointing findings (any on a clean case is a false positive) |
| reproduction rate | findings with `repro_outcome ∈ {reproduced, not_reproduced}` that are `reproduced` ÷ findings whose reproduction test actually executed |
| reproduction inconclusive count | findings whose outcome is `inconclusive`/`execution_error` (reported, never folded into the rate) |
| patch verification rate | patches reaching `verified_for_tested_behavior` ÷ patches whose verification was attempted |
| regression rate | `post_fix_regression` runs with exit ≠ 0 ÷ all `post_fix_regression` runs |
| false-positive rate | clean cases with ≥1 defect-pointing finding ÷ clean cases |
| median audit duration | median wall-clock seconds per audit (creation → terminal status) |
| resource consumption | pytest invocations, total wall seconds, LLM credits (0 by construction for `LOCAL_ENGINE`) |

## 4. Measured results — run of 2026-10-26 (UTC), host isolation `linux_namespace`

```
corpus: 7 cases (4 injected defects, 1 coverage-gap, 2 clean)

  defect_recall                      0.75     (3/4; tax-gross missed, see §5)
  finding_precision                  1.0      (4 defect-pointing findings, 0 on clean cases)
  reproduction_rate                  1.0      (3/3 executed reproductions reproduced)
  reproduction_inconclusive_count    1        (R-TEST-001: not mechanically testable)
  patch_verification_rate            1.0      (3/3 approved patches verified by execution)
  regression_rate                    0.0      (0/3 post-fix regression runs failed)
  false_positive_rate                0.0      (0/3 clean or coverage-only cases)
  median_audit_duration_seconds      1.03
  pytest_invocations                 15       (= 12 verification-matrix runs + 3 in-audit reproductions)
  llm_credits                        0
```

Per-case detail (from `evaluation/results/benchmark-20261026-205004.json`):

| Case | Detected | Findings | Reproduced | pytest runs | Patch outcome |
|------|----------|----------|------------|-------------|---------------|
| shipping-waiver | yes | 1 | yes | 4 | verified_for_tested_behavior |
| refund-penalty | yes | 1 | yes | 4 | verified_for_tested_behavior |
| ledger-fee | yes | 1 | yes | 4 | verified_for_tested_behavior |
| tax-gross | **no** | 0 | – | 0 | – |
| format-untested-change | n/a | 1 (R-TEST-001, medium) | inconclusive by design | 0 | – |
| clean-additive | n/a | 0 | – | 0 | – |
| clean-refactor | n/a | 0 | – | 0 | – |

Note on pytest invocations: each verified case executes one baseline
reproduction *during* the audit (3 total) and the four-run verification matrix
*after* approval (12 total); both are counted, and the per-case JSON keeps the
two phases separate (`audit_phase_runs` vs `pytest_runs`).

## 5. Honest interpretation and known gaps

* **`tax-gross` is a real miss.** The v1 arithmetic rule reasons about
  *net/remainder* contracts and their adjustment-flip violations. Additive
  ("gross = net + tax") contracts are outside its semantics classes, so the
  defect is neither found nor silently "found": recall is 0.75 and this section
  explains why. Extending the contract classifier to additive semantics is the
  first planned engine improvement.
* **Coverage-gap findings are not defects.** `format-untested-change` produces a
  medium `missing_tests` finding and is explicitly *not* mechanically testable;
  it is reported as inconclusive rather than being forced into the reproduction
  rate.
* **Durations are corpus-dependent.** The corpus modules are tiny; median audit
  duration on this corpus says nothing about duration on large repositories.
  The acceptance fixture (`examples/discount-service`, 16 tests) is the better
  reference for realistic timing.
* **The Duo Agent Platform path is unbenchmarked here.** It requires a
  Premium/Ultimate instance with credentials; in this environment the product
  reports the missing prerequisite and labels every audit
  `local_deterministic`. When credentials exist, the same protocol can
  benchmark the live flow by switching project origin to `gitlab`.
* **Isolation tier is host-dependent.** Numbers above were measured under
  `linux_namespace`, which the security documentation explicitly describes as
  *not* a hard security boundary. Under Docker the executor changes; results
  should be re-measured and re-quoted, not assumed identical.

## 6. Re-running

```bash
cd backend
PYTHONPATH=. python3 ../evaluation/run_benchmark.py            # full corpus
PYTHONPATH=. python3 ../evaluation/run_benchmark.py --cases shipping-waiver,tax-gross
PYTHONPATH=. python3 ../evaluation/run_benchmark.py --no-verify   # analysis-only, faster
```

Each run appends a new `evaluation/results/benchmark-<utc>.json`; compare runs
by diffing those files. The CI `test:integration` job runs the mandatory
acceptance test; the benchmark is intentionally a local/nightly tool because it
executes untrusted corpus code and writes result artefacts.
