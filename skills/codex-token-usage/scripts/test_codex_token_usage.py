#!/usr/bin/env python3
"""Minimal tests for codex_token_usage.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("codex_token_usage.py")


def write_event(handle, timestamp, input_tokens, cached, output, reasoning, total):
    payload = {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": reasoning,
                    "total_tokens": total,
                },
                "total_token_usage": {
                    "input_tokens": 999999,
                    "cached_input_tokens": 999999,
                    "output_tokens": 999999,
                    "reasoning_output_tokens": 999999,
                    "total_tokens": 999999,
                },
            },
        },
    }
    handle.write(json.dumps(payload) + "\n")


def write_total_only_event(handle, timestamp, total):
    payload = {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "total_tokens": total,
                },
            },
        },
    }
    handle.write(json.dumps(payload) + "\n")


def write_session(codex_home: Path) -> None:
    session_dir = codex_home / "sessions" / "2026" / "04" / "29"
    session_dir.mkdir(parents=True)
    path = session_dir / (
        "rollout-2026-04-29T10-00-00-"
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    )
    with path.open("w", encoding="utf-8") as handle:
        write_event(handle, "2026-04-28T01:00:00.000Z", 100, 40, 30, 5, 130)
        write_event(handle, "2026-04-28T02:00:00.000Z", 200, 50, 80, 10, 280)
        write_event(handle, "2026-04-29T01:00:00.000Z", 50, 10, 20, 2, 70)
        write_total_only_event(handle, "2026-04-29T02:00:00.000Z", 25)


def run_script(codex_home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-B",
        str(SCRIPT),
        "--codex-home",
        str(codex_home),
        "--timezone",
        "Asia/Shanghai",
        "--language",
        "en",
        *args,
    ]
    return subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )


def test_json_output() -> None:
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
        codex_home = Path(temp)
        write_session(codex_home)
        result = run_script(
            codex_home,
            "--start",
            "2026-04-28",
            "--end",
            "2026-04-29",
            "--format",
            "json",
        )
        data = json.loads(result.stdout)

    assert data["summary"]["total"] == 505
    assert data["summary"]["input"] == 350
    assert data["summary"]["cached_input"] == 100
    assert data["summary"]["output"] == 130
    assert data["summary"]["reasoning_output"] == 17
    assert data["summary"]["non_cached_input"] == 250
    assert data["summary"]["net_usage"] == 380
    assert data["summary"]["unpriced_total_only"] == 25
    assert data["summary"]["cache_hit_rate"] == 100 / 350
    assert data["summary"]["daily_average_total"] == 252.5
    assert data["peak_day"]["date"] == "2026-04-28"
    assert data["peak_day"]["summary"]["total"] == 410
    assert data["files_scanned"] == 1
    assert data["summary"]["events"] == 4
    assert data["summary"]["sessions"] == 1


def test_markdown_output_mentions_key_metrics() -> None:
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
        codex_home = Path(temp)
        write_session(codex_home)
        result = run_script(
            codex_home,
            "--start",
            "2026-04-28",
            "--end",
            "2026-04-29",
            "--format",
            "markdown",
        )

    assert "| Cache hit rate | 28.57% |" in result.stdout
    assert "| Daily average total | 252.50 |" in result.stdout
    assert "Peak day: 2026-04-28, 410 tokens." in result.stdout
    assert "Busiest week: 2026-04-28 to 2026-04-29, 505 tokens." in result.stdout


def test_cost_estimate_json_output() -> None:
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
        codex_home = Path(temp)
        write_session(codex_home)
        result = run_script(
            codex_home,
            "--start",
            "2026-04-28",
            "--end",
            "2026-04-29",
            "--format",
            "json",
            "--show-cost",
            "--input-price",
            "10",
            "--cached-input-price",
            "1",
            "--output-price",
            "20",
        )
        data = json.loads(result.stdout)

    cost = data["summary"]["cost_estimate"]
    assert cost["currency"] == "USD"
    assert cost["prices_per_million_tokens"]["input"] == 10
    assert cost["prices_per_million_tokens"]["cached_input"] == 1
    assert cost["prices_per_million_tokens"]["output"] == 20
    assert cost["tokens"]["non_cached_input"] == 250
    assert cost["tokens"]["cached_input"] == 100
    assert cost["tokens"]["output"] == 130
    assert cost["tokens"]["unpriced_total_only"] == 25
    expected = ((250 * 10) + (100 * 1) + (130 * 20) + (25 * 10)) / 1_000_000
    assert cost["total_cost"] == expected


def test_cost_estimate_markdown_output() -> None:
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
        codex_home = Path(temp)
        write_session(codex_home)
        result = run_script(
            codex_home,
            "--start",
            "2026-04-28",
            "--end",
            "2026-04-29",
            "--format",
            "markdown",
            "--show-cost",
            "--show-daily",
            "--input-price",
            "10",
            "--cached-input-price",
            "1",
            "--output-price",
            "20",
        )

    assert "API cost estimate: Input $10.00/1M" in result.stdout
    assert "| Estimated total | $0.01 |" in result.stdout
    assert "| Total-only tokens | $0.00 |" in result.stdout
    assert "| Date | Total | Cost | Token events | Sessions |" in result.stdout


def test_month_output_and_empty_range() -> None:
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
        codex_home = Path(temp)
        write_session(codex_home)
        result = run_script(codex_home, "--month", "2026-05", "--format", "json")
        data = json.loads(result.stdout)

    assert data["start"] == "2026-05-01"
    assert data["end"] == "2026-05-31"
    assert data["summary"]["events"] == 0
    assert data["summary"]["total"] == 0


if __name__ == "__main__":
    test_json_output()
    test_markdown_output_mentions_key_metrics()
    test_cost_estimate_json_output()
    test_cost_estimate_markdown_output()
    test_month_output_and_empty_range()
    print("tests passed")
