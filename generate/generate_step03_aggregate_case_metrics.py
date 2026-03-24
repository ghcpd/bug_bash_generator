#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def load_metrics(metrics_dir: Path) -> list[dict]:
    metrics: list[dict] = []
    for path in sorted(metrics_dir.glob("*_attempt_*.json")):
        metrics.append(json.loads(path.read_text(encoding="utf-8")))
    return metrics


def aggregate(items: list[dict]) -> dict:
    attempts = sorted(items, key=lambda item: (item.get("attempt", 0), item.get("invocation_type", "")))
    return {
        "invocations_count": len(attempts),
        "model_calls_count": sum(int(item.get("model_calls_count", 0) or 0) for item in attempts),
        "prompt_tokens": sum(int(item.get("prompt_tokens", 0) or 0) for item in attempts),
        "completion_tokens": sum(int(item.get("completion_tokens", 0) or 0) for item in attempts),
        "total_tokens": sum(int(item.get("total_tokens", 0) or 0) for item in attempts),
        "duration_ms": sum(int(item.get("duration_ms", 0) or 0) for item in attempts),
        "wall_time_ms": sum(int(item.get("wall_time_ms", 0) or 0) for item in attempts),
        "telemetry_cost": sum(int(item.get("telemetry_cost", 0) or 0) for item in attempts),
        "attempts": attempts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate Copilot CLI metrics for one synthetic case task")
    parser.add_argument("--metrics-dir", required=True)
    parser.add_argument("--task-run-id", required=True)
    parser.add_argument("--repo-slug", required=True)
    parser.add_argument("--case-index", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-retries", type=int, required=True)
    parser.add_argument("--pipeline-success", choices=["true", "false"], required=True)
    parser.add_argument("--instance-id")
    parser.add_argument("--successful-attempt", type=int)
    parser.add_argument("--failure-reason", default="")
    args = parser.parse_args()

    metrics_dir = Path(args.metrics_dir)
    items = load_metrics(metrics_dir)
    grouped: dict[str, list[dict]] = defaultdict(list)
    distinct_attempts: set[int] = set()
    for item in items:
        grouped[item.get("invocation_type", "unknown")].append(item)
        distinct_attempts.add(int(item.get("attempt", 0) or 0))

    generate = aggregate(grouped.get("generate", []))
    critic = aggregate(grouped.get("critic", []))
    audit = aggregate(grouped.get("audit", []))

    totals = {
        "invocations_count": generate["invocations_count"] + critic["invocations_count"] + audit["invocations_count"],
        "model_calls_count": generate["model_calls_count"] + critic["model_calls_count"] + audit["model_calls_count"],
        "prompt_tokens": generate["prompt_tokens"] + critic["prompt_tokens"] + audit["prompt_tokens"],
        "completion_tokens": generate["completion_tokens"] + critic["completion_tokens"] + audit["completion_tokens"],
        "total_tokens": generate["total_tokens"] + critic["total_tokens"] + audit["total_tokens"],
        "duration_ms": generate["duration_ms"] + critic["duration_ms"] + audit["duration_ms"],
        "wall_time_ms": generate["wall_time_ms"] + critic["wall_time_ms"] + audit["wall_time_ms"],
        "telemetry_cost": generate["telemetry_cost"] + critic["telemetry_cost"] + audit["telemetry_cost"],
    }

    result = {
        "schema_version": 1,
        "collection_method": "copilot_cli_debug_logs",
        "task_run_id": args.task_run_id,
        "instance_id": args.instance_id,
        "repo": args.repo_slug.replace("__", "/"),
        "case_index": args.case_index,
        "model": args.model,
        "max_retries": args.max_retries,
        "attempts_used": max(distinct_attempts) if distinct_attempts else 0,
        "successful_attempt": args.successful_attempt,
        "pipeline_success": args.pipeline_success == "true",
        "failure_reason": args.failure_reason or None,
        "generate": generate,
        "critic": critic,
        "audit": audit,
        "totals": totals,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()