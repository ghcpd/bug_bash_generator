# SWE-bench Synthetic Case Generator

This file is the English counterpart of `HANDOVER.md`.

Generate synthetic SWE-bench BenchInstances from any GitHub Python repo, then audit them automatically.

## Prerequisites

Before first use, make sure the runtime image or local shell already has:

- `git`
- `gh` (GitHub CLI with Copilot access)
- `python3` with working `pip`
- `curl`

Notes for manual execution:

- The shell entrypoints currently expect the exact positional arguments shown in their `Usage` headers.
- They are primarily designed to be called by ADF / Batch, so manual runs should follow the documented argument order exactly.

## Quick Start (ADF Pipeline)

### 1. Deploy scripts to Azure Batch

Upload **this entire folder** (`scripts/generate_case/`) to:

```
Blob: raw-data / code/scripts/owen/bug_bash/
```

### 2. Import ADF pipelines

Import these two JSON files into your Data Factory:

| Pipeline JSON | Purpose |
|---|---|
| `bug_bash_batch_generate_pipeline.json` | Generate N synthetic cases |
| `bug_bash_audit_case_pipeline.json` | Audit generated cases (L1-L7) |

### 3. Run — Generate Cases

Trigger `bug_bash_batch_generate_pipeline` with **only 2 required parameters**:

| Parameter | Example | Required? |
|---|---|---|
| `repoUrl` | `https://github.com/jazzband/prettytable` | **Yes** |
| `github_token` | `ghp_xxxx` | **Yes** |
| `num_cases` | `3` | No (default: 3) |
| `category` | `Logic & Algorithm` | No (random if empty) |
| `difficulty` | `L2` | No (random if empty) |

Everything else has defaults. Output goes to:

```
data_processing_demo/owen/generate-case/
├── tar.gz/   ← buggy code snapshots
├── jsonl/    ← case metadata (with category, difficulty, labels)
├── metrics/  ← Copilot CLI token/time metrics per task
```

### 4. Run — Audit Cases

Trigger `bug_bash_audit_case_pipeline` — no required parameters (all have defaults).

Output: `data_processing_demo/owen/generate-case/audit/audit_results.json`

---

## What Happens Under the Hood

```
ADF ForEach(0..num_cases)
  └─ Azure Batch node
     └─ generate/generate_step01_generate_case.sh
            ├─ git clone <repo>
            ├─ Build prompt (generate/default_prompt.md + repo context)
            ├─ Invoke Copilot CLI (gh copilot suggest)
            └─ generate/generate_step04_package_case_artifacts.py
                 ├─ Parse AI output → case JSON
                 ├─ Reverse-apply gold_patch → buggy snapshot
                 ├─ Package → tar.gz
                 └─ Write → jsonl
```

## Directory Layout

The folder is now organized by responsibility instead of keeping all scripts flat at the root:

```text
scripts/generate_case/
├── generate/
│   ├── generate_step01_generate_case.sh
│   ├── generate_step02_extract_copilot_metrics.py
│   ├── generate_step03_aggregate_case_metrics.py
│   ├── generate_step04_package_case_artifacts.py
│   └── default_prompt.md
├── audit/
│   ├── audit_step01_dispatch_audit.sh
│   ├── audit_step02_batch_audit_cases.py
│   └── audit_step03_validate_instance.py
├── auth/
│   ├── get_tokens.ps1
│   ├── token_server.ps1
│   ├── token_server.sh
│   └── prompt_generator.html
├── README.md                      ← English handover
└── requirements.txt
```

Use the folders like this:

- `generate/`: case generation main flow, metrics parsing, post-processing, and built-in prompt.
- `audit/`: single-case audit entrypoint, batch wrapper, and L1-L7 audit engine.
- `auth/`: local token helpers and the legacy HTML helper page.
- root files: documentation plus shared Python dependencies only.

If you are tracing the runtime path, the shortest mapping is:

- Generate pipeline entry: `generate/generate_step01_generate_case.sh`
- Audit pipeline entry: `audit/audit_step01_dispatch_audit.sh`
- Token helper entry: `auth/get_tokens.ps1` or `auth/token_server.ps1`

## Copilot CLI Metrics

The generator now records exact Copilot CLI usage from debug logs for each synthetic case task.

- Source of truth: Copilot CLI debug telemetry, not local token estimation.
- Current coverage: `generate` and `critic` invocations in the generate pipeline.
- Not yet covered: the separate audit pipeline is not instrumented yet, so `audit.*` remains `0` for now.

Artifacts:

```
data_processing_demo/owen/generate-case/
├── metrics/
│   ├── <instance_id>.metrics.json
│   └── <task_run_id>.failed.metrics.json
└── jsonl/
     └── <instance_id>.jsonl   ← includes the same summary under `benchmark`
```

### Metric Scope

- One `gh copilot` invocation can trigger multiple internal model calls.
- We aggregate all internal model calls inside that invocation.
- A successful case writes a sidecar metrics file in `metrics/`.
- The same successful case also writes the same summary into the JSONL record under `benchmark`.
- A failed task writes only `<task_run_id>.failed.metrics.json`.

### Field Definitions

Top-level fields in `<instance_id>.metrics.json` and `benchmark`:

| Field | Meaning |
|---|---|
| `schema_version` | Metrics schema version. Current value: `1`. |
| `collection_method` | How metrics were collected. Current value: `copilot_cli_debug_logs`. |
| `task_run_id` | Unique ID for one task execution attempt group. Stable across retries for the same repo + case index run. |
| `instance_id` | Final generated case ID. Present for successful cases, typically `null` for failed tasks. |
| `repo` | Repository in `owner/name` format. |
| `case_index` | Requested synthetic case index for that repo. |
| `model` | Copilot model configured for the task, for example `claude-sonnet-4.6`. |
| `max_retries` | Maximum retry count allowed by the generate script. |
| `attempts_used` | Highest attempt number actually used by this task. |
| `successful_attempt` | Which attempt produced the final successful case. `null` if the task failed. |
| `pipeline_success` | Whether the generate pipeline produced a valid final case for this task. |
| `failure_reason` | Final failure reason if no successful case was produced. |

Per-phase sections: `generate`, `critic`, `audit`, and `totals`

| Field | Meaning |
|---|---|
| `invocations_count` | Number of CLI invocations recorded in this section. Usually one per phase per attempt. |
| `model_calls_count` | Number of internal model calls emitted by Copilot CLI telemetry. Important because one invocation may fan out into many model calls. |
| `prompt_tokens` | Sum of input tokens across all model calls in this section. |
| `completion_tokens` | Sum of output tokens across all model calls in this section. |
| `total_tokens` | `prompt_tokens + completion_tokens` across all model calls in this section. |
| `duration_ms` | Sum of telemetry-reported model call durations. Best used as model-side generation time. |
| `wall_time_ms` | End-to-end wall clock time measured around the CLI invocation. Includes CLI overhead and is the best proxy for user wait time. |
| `telemetry_cost` | Copilot telemetry cost counter from debug logs. Useful for internal relative comparison only; do not treat it as billing currency. |
| `attempts` | Raw per-invocation metric entries that were aggregated into this section. |

Fields inside each item of `attempts`:

| Field | Meaning |
|---|---|
| `invocation_type` | Which phase emitted the metrics: `generate`, `critic`, or `audit`. |
| `attempt` | Retry number for this invocation. |
| `log_dir` | Local debug log directory used to collect telemetry for that invocation. |
| `log_files_count` | Number of Copilot CLI log files parsed. |
| `exit_code` | CLI process exit code. |
| `timed_out` | Whether the invocation timed out. Derived from exit code `124`. |

### Recommended Interpretation

- For token usage, use `total_tokens`.
- For model-side latency, use `duration_ms`.
- For real end-user waiting time, use `wall_time_ms`.
- For whole-task comparison across models, use `totals.*`.
- For phase breakdown, compare `generate.*` and `critic.*` separately.
- For batch-level benchmarking, compute `total tokens across all attempted tasks / successful case count`.
- For batch-level benchmarking, compute `total wall time across all attempted tasks / successful case count`.

### Current Limitation

- Resume logic skips tasks that already have a matching JSONL + tar.gz for the same repo and case index.
- If a matching JSONL exists but the paired tar.gz is missing, the incomplete record is deleted and the case is regenerated.
- Skipped tasks do not create new metrics, because no new Copilot CLI invocation happens.
- Audit pipeline metrics are not yet instrumented; `audit` is currently a reserved section in the schema.

## Manual Entry Points

For local debugging, these are the expected shell signatures:

```bash
bash generate/generate_step01_generate_case.sh <task_json_base64> <github_token> <prompt_path> <output_base>
bash audit/audit_step01_dispatch_audit.sh <jsonl_blob_path> <mount_root> <audit_level> <output_dir>
bash audit/audit_step01_dispatch_audit.sh --batch <input_folder> <output_folder> [audit_level]
```

If you are running locally instead of through ADF, verify the target paths exist before invoking the scripts.

## Valid Labels

**Categories (10):** Logic & Algorithm, Data Handling & Transformation, API & Interface Contract, Error Handling & Edge Cases, Infrastructure & Tooling, Performance & Efficiency, Security & Access Control, Configuration & Environment, Type & Validation, Documentation & Naming

**Difficulty:** L1 (trivial) → L4 (cross-module)

**Localization:** explicit, implicit, cross_file, cross_module

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No valid case definitions found" | Copilot output didn't contain parseable JSON. Check `copilot_output.txt` on the Batch node. |
| `git apply --reverse` failed | `gold_patch` has wrong paths or format. Must be `diff --git a/... b/...` unified diff. |
| L4 audit fails "tests did not FAIL on buggy code" | Mutation was too weak or test doesn't exercise the bug. Regenerate. |
| Prompt not found warning | Upload `generate/default_prompt.md` to blob, or it auto-falls back to the built-in copy. |
