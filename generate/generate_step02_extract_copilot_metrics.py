#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def extract_json_blocks(text: str, marker: str) -> list[dict]:
    blocks: list[dict] = []
    start = 0
    while True:
        idx = text.find(marker, start)
        if idx == -1:
            break
        brace_start = text.find("{", idx)
        if brace_start == -1:
            break

        depth = 0
        in_string = False
        escape = False
        for pos in range(brace_start, len(text)):
            char = text[pos]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        blocks.append(json.loads(text[brace_start:pos + 1]))
                    except json.JSONDecodeError:
                        pass
                    start = pos + 1
                    break
        else:
            break
    return blocks


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse Copilot CLI debug logs into invocation metrics")
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--invocation-type", required=True, choices=["generate", "critic", "audit"])
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--wall-time-ms", type=int, required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    log_files = sorted(log_dir.glob("process-*.log"))

    model_calls: list[dict] = []
    assistant_usage: list[dict] = []
    for log_file in log_files:
        content = log_file.read_text(encoding="utf-8", errors="replace")
        for block in extract_json_blocks(content, "[Telemetry] cli.model_call:"):
            model_calls.append(block)
        for block in extract_json_blocks(content, "[Telemetry] cli.telemetry:"):
            if block.get("kind") == "assistant_usage":
                assistant_usage.append(block)

    prompt_tokens = sum(int(block.get("prompt_tokens_count", 0) or 0) for block in model_calls)
    completion_tokens = sum(int(block.get("completion_tokens_count", 0) or 0) for block in model_calls)
    total_tokens = sum(int(block.get("total_tokens_count", 0) or 0) for block in model_calls)
    duration_ms = sum(int(block.get("duration_ms", 0) or 0) for block in model_calls)
    cost = sum(int((block.get("metrics") or {}).get("cost", 0) or 0) for block in assistant_usage)

    model = ""
    for block in model_calls:
        model = block.get("model") or model
    if not model:
        for block in assistant_usage:
            model = (block.get("properties") or {}).get("model") or model

    result = {
        "schema_version": 1,
        "collection_method": "copilot_cli_debug_logs",
        "invocation_type": args.invocation_type,
        "attempt": args.attempt,
        "model": model,
        "log_dir": str(log_dir),
        "log_files_count": len(log_files),
        "model_calls_count": len(model_calls),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "duration_ms": duration_ms,
        "wall_time_ms": args.wall_time_ms,
        "telemetry_cost": cost,
        "exit_code": args.exit_code,
        "timed_out": args.exit_code == 124,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()