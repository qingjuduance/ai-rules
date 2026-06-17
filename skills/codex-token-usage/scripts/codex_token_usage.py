#!/usr/bin/env python3
"""Summarize local Codex token usage from session JSONL logs."""

from __future__ import annotations

import argparse
import calendar
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    ZoneInfo = None


LABELS = {
    "en": {
        "range": "Range",
        "codex_home": "Codex home",
        "files": "Files scanned",
        "events": "Token events",
        "sessions": "Sessions",
        "metric": "Metric",
        "tokens": "Tokens",
        "notes": "Notes",
        "total": "Total",
        "input": "Input",
        "cached_input": "Cached input",
        "output": "Output",
        "reasoning_output": "Reasoning output",
        "non_cached_input": "Non-cached input",
        "net_usage": "Net usage",
        "cache_hit_rate": "Cache hit rate",
        "daily_average_total": "Daily average total",
        "cost": "Cost",
        "cost_item": "Cost item",
        "amount": "Amount",
        "api_cost_estimate": "API cost estimate",
        "price_per_million": "Price per 1M tokens",
        "estimated_total_cost": "Estimated total",
        "cost_non_cached_input": "Non-cached input",
        "cost_cached_input": "Cached input",
        "cost_output": "Output",
        "cost_unpriced": "Total-only tokens",
        "peak_day": "Peak day",
        "busiest_week": "Busiest week",
        "no_events": "No token_count events were found in this range.",
        "total_note": "Sum of last_token_usage.total_tokens",
        "input_note": "Input tokens, including cached input",
        "cached_note": "Cached input tokens",
        "output_note": "Output tokens",
        "reasoning_note": "Reasoning output tokens",
        "non_cached_note": "Input - cached input",
        "net_note": "Non-cached input + output",
        "cache_rate_note": "Cached input / input",
        "daily_average_note": "Total / calendar days in range",
        "cost_note": "API estimate only; subscription plans may bill differently",
        "unpriced_note": "Only total_tokens was available, charged at the unpriced token rate",
    },
    "zh": {
        "range": "范围",
        "codex_home": "Codex home",
        "files": "扫描文件数",
        "events": "token_count 事件数",
        "sessions": "会话数",
        "metric": "指标",
        "tokens": "Token 数",
        "notes": "说明",
        "total": "总量",
        "input": "Input",
        "cached_input": "Cached input",
        "output": "Output",
        "reasoning_output": "Reasoning output",
        "non_cached_input": "非缓存 Input",
        "net_usage": "净用量",
        "cache_hit_rate": "缓存命中率",
        "daily_average_total": "日均总量",
        "cost": "估算费用",
        "cost_item": "费用项",
        "amount": "金额",
        "api_cost_estimate": "API 费用估算",
        "price_per_million": "每 100 万 tokens 价格",
        "estimated_total_cost": "估算合计",
        "cost_non_cached_input": "非缓存 Input",
        "cost_cached_input": "Cached input",
        "cost_output": "Output",
        "cost_unpriced": "仅 total 的 tokens",
        "peak_day": "最多的一天",
        "busiest_week": "最多的一周",
        "no_events": "这个时间范围内没有找到 token_count 事件。",
        "total_note": "汇总 last_token_usage.total_tokens",
        "input_note": "输入 token，包含 cached input",
        "cached_note": "命中缓存的输入 token",
        "output_note": "输出 token",
        "reasoning_note": "推理输出 token",
        "non_cached_note": "Input - cached input",
        "net_note": "非缓存 Input + Output",
        "cache_rate_note": "Cached input / Input",
        "daily_average_note": "总量 / 统计范围日历天数",
        "cost_note": "仅按 API 价格估算；订阅套餐的真实账单可能不同",
        "unpriced_note": "只有 total_tokens，没有 input/output 拆分，按未拆分 token 价格估算",
    },
}


DEFAULT_INPUT_PRICE = 5.00
DEFAULT_CACHED_INPUT_PRICE = 0.50
DEFAULT_OUTPUT_PRICE = 30.00
PRICE_UNIT = 1_000_000


@dataclass(frozen=True)
class TokenEvent:
    session: str
    source_file: str
    timestamp: str
    day: date
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class CostConfig:
    currency: str
    input_price: float
    cached_input_price: float
    output_price: float
    unpriced_token_price: float


def non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize Codex token usage from local session JSONL logs."
    )
    parser.add_argument(
        "--codex-home",
        default=os.environ.get("CODEX_HOME") or str(pathlib.Path.home() / ".codex"),
        help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--timezone",
        default=None,
        help="IANA timezone such as Asia/Shanghai, UTC, or a fixed offset like +08:00.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Rolling local calendar days ending on --end or today. Defaults to 30.",
    )
    parser.add_argument("--start", default=None, help="Inclusive start date, YYYY-MM-DD.")
    parser.add_argument("--end", default=None, help="Inclusive end date, YYYY-MM-DD.")
    parser.add_argument("--month", default=None, help="Calendar month, YYYY-MM.")
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format.",
    )
    parser.add_argument(
        "--language",
        choices=("zh", "en"),
        default="zh",
        help="Markdown label language.",
    )
    parser.add_argument(
        "--pattern",
        default="rollout-*.jsonl",
        help="Session log filename glob under session roots.",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Also scan ~/.codex/archived_sessions when present.",
    )
    parser.add_argument(
        "--show-daily",
        action="store_true",
        help="Include a daily total table in Markdown output.",
    )
    parser.add_argument(
        "--show-cost",
        action="store_true",
        help="Include an API cost estimate in Markdown and JSON output.",
    )
    parser.add_argument(
        "--input-price",
        type=non_negative_float,
        default=DEFAULT_INPUT_PRICE,
        help="Input price per 1M non-cached input tokens. Default: 5.00.",
    )
    parser.add_argument(
        "--cached-input-price",
        type=non_negative_float,
        default=DEFAULT_CACHED_INPUT_PRICE,
        help="Cached input price per 1M cached input tokens. Default: 0.50.",
    )
    parser.add_argument(
        "--output-price",
        type=non_negative_float,
        default=DEFAULT_OUTPUT_PRICE,
        help="Output price per 1M output tokens. Default: 30.00.",
    )
    parser.add_argument(
        "--unpriced-token-price",
        type=non_negative_float,
        default=None,
        help=(
            "Price per 1M tokens for token_count events that only expose total_tokens. "
            "Defaults to --input-price."
        ),
    )
    parser.add_argument(
        "--currency",
        default="USD",
        help="Currency label for cost estimates. Default: USD.",
    )
    return parser.parse_args()


def parse_fixed_offset(value: str) -> timezone | None:
    if value in {"Z", "UTC", "+00:00", "-00:00"}:
        return timezone.utc
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", value)
    if not match:
        return None
    sign, hours, minutes = match.groups()
    delta = timedelta(hours=int(hours), minutes=int(minutes))
    if sign == "-":
        delta = -delta
    return timezone(delta)


def resolve_timezone(value: str | None):
    if not value:
        return datetime.now().astimezone().tzinfo or timezone.utc
    fixed = parse_fixed_offset(value)
    if fixed:
        return fixed
    if value == "Asia/Shanghai":
        return timezone(timedelta(hours=8), name="Asia/Shanghai")
    if ZoneInfo is not None:
        try:
            return ZoneInfo(value)
        except Exception as exc:  # pragma: no cover - depends on local tzdata
            raise SystemExit(f"Unknown timezone {value!r}: {exc}") from exc
    raise SystemExit(f"Timezone {value!r} requires zoneinfo support or a fixed offset.")


def local_today(tz) -> date:
    return datetime.now(tz).date()


def parse_month(value: str) -> tuple[int, int]:
    try:
        year_text, month_text = value.split("-", 1)
        year = int(year_text)
        month = int(month_text)
    except ValueError as exc:
        raise SystemExit("--month must use YYYY-MM format") from exc
    if month < 1 or month > 12:
        raise SystemExit("--month must use a month from 01 to 12")
    return year, month


def resolve_range(args: argparse.Namespace, tz) -> tuple[date, date]:
    if args.month:
        year, month = parse_month(args.month)
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        today = local_today(tz)
        if start <= today <= end:
            end = today
        return start, end

    end = date.fromisoformat(args.end) if args.end else local_today(tz)
    if args.start:
        start = date.fromisoformat(args.start)
    else:
        days = args.days or 30
        if days <= 0:
            raise SystemExit("--days must be greater than 0")
        start = end - timedelta(days=days - 1)
    if start > end:
        raise SystemExit("--start must be earlier than or equal to --end")
    return start, end


def session_id(path: pathlib.Path) -> str:
    match = re.search(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        path.name,
    )
    return match.group(1).lower() if match else path.stem


def iter_session_files(codex_home: pathlib.Path, pattern: str, include_archived: bool):
    roots = [codex_home / "sessions"]
    if include_archived:
        roots.append(codex_home / "archived_sessions")
    for root in roots:
        if not root.exists():
            continue
        yield from sorted(root.rglob(pattern))


def read_int(mapping: dict, *keys: str) -> int:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


def parse_timestamp(value: str | None, tz):
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz)


def extract_last_usage(obj: dict) -> tuple[dict | None, str | None]:
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    event_type = payload.get("type") or obj.get("type")
    if event_type != "token_count":
        return None, None

    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    if not info and isinstance(obj.get("info"), dict):
        info = obj["info"]

    usage = info.get("last_token_usage")
    if usage is None:
        usage = payload.get("last_token_usage") or obj.get("last_token_usage")
    if not isinstance(usage, dict):
        return None, None

    timestamp = obj.get("timestamp") or payload.get("timestamp") or info.get("timestamp")
    return usage, timestamp


def iter_token_events(codex_home: pathlib.Path, pattern: str, include_archived: bool, tz):
    seen = set()
    scanned_files = 0
    invalid_lines = 0
    for path in iter_session_files(codex_home, pattern, include_archived):
        scanned_files += 1
        sid = session_id(path)
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line_number, line in enumerate(handle, start=1):
                if '"token_count"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    invalid_lines += 1
                    continue
                usage, timestamp = extract_last_usage(obj)
                dt = parse_timestamp(timestamp, tz)
                if usage is None or dt is None:
                    continue
                event = TokenEvent(
                    session=sid,
                    source_file=str(path),
                    timestamp=timestamp or "",
                    day=dt.date(),
                    input_tokens=read_int(usage, "input_tokens", "input"),
                    cached_input_tokens=read_int(
                        usage, "cached_input_tokens", "cached_input"
                    ),
                    output_tokens=read_int(usage, "output_tokens", "output"),
                    reasoning_output_tokens=read_int(
                        usage, "reasoning_output_tokens", "reasoning_output"
                    ),
                    total_tokens=read_int(usage, "total_tokens", "total"),
                )
                key = (
                    event.session,
                    event.timestamp,
                    event.input_tokens,
                    event.cached_input_tokens,
                    event.output_tokens,
                    event.total_tokens,
                )
                if key in seen:
                    continue
                seen.add(key)
                yield event, scanned_files, invalid_lines


def collect_events(
    codex_home: pathlib.Path,
    pattern: str,
    include_archived: bool,
    tz,
    start: date,
    end: date,
) -> tuple[list[TokenEvent], int, int]:
    events: list[TokenEvent] = []
    scanned_files = 0
    invalid_lines = 0
    iterator = iter_token_events(codex_home, pattern, include_archived, tz)
    for event, scanned_count, invalid_count in iterator:
        scanned_files = max(scanned_files, scanned_count)
        invalid_lines = max(invalid_lines, invalid_count)
        if start <= event.day <= end:
            events.append(event)

    # If no events were yielded, count files separately so reports still show scan scope.
    if scanned_files == 0:
        scanned_files = sum(1 for _ in iter_session_files(codex_home, pattern, include_archived))
    return events, scanned_files, invalid_lines


def day_count(start: date, end: date) -> int:
    return max((end - start).days + 1, 1)


def summarize(events: list[TokenEvent], days: int | None = None) -> dict:
    input_tokens = sum(event.input_tokens for event in events)
    cached_input = sum(event.cached_input_tokens for event in events)
    output = sum(event.output_tokens for event in events)
    reasoning = sum(event.reasoning_output_tokens for event in events)
    total = sum(event.total_tokens for event in events)
    non_cached_input = max(input_tokens - cached_input, 0)
    net_usage = non_cached_input + output
    unpriced_total_only = sum(
        max(event.total_tokens - event.input_tokens - event.output_tokens, 0)
        for event in events
    )
    return {
        "events": len(events),
        "sessions": len({event.session for event in events}),
        "total": total,
        "input": input_tokens,
        "cached_input": cached_input,
        "output": output,
        "reasoning_output": reasoning,
        "non_cached_input": non_cached_input,
        "net_usage": net_usage,
        "unpriced_total_only": unpriced_total_only,
        "cache_hit_rate": (cached_input / input_tokens) if input_tokens else 0.0,
        "daily_average_total": (total / days) if days else None,
    }


def build_cost_config(args: argparse.Namespace) -> CostConfig:
    return CostConfig(
        currency=args.currency,
        input_price=args.input_price,
        cached_input_price=args.cached_input_price,
        output_price=args.output_price,
        unpriced_token_price=(
            args.input_price
            if args.unpriced_token_price is None
            else args.unpriced_token_price
        ),
    )


def estimate_cost(summary: dict, config: CostConfig) -> dict:
    non_cached_input = summary["non_cached_input"]
    cached_input = summary["cached_input"]
    output = summary["output"]
    unpriced = summary["unpriced_total_only"]
    input_cost = non_cached_input * config.input_price / PRICE_UNIT
    cached_input_cost = cached_input * config.cached_input_price / PRICE_UNIT
    output_cost = output * config.output_price / PRICE_UNIT
    unpriced_cost = unpriced * config.unpriced_token_price / PRICE_UNIT
    return {
        "currency": config.currency,
        "price_unit_tokens": PRICE_UNIT,
        "prices_per_million_tokens": {
            "input": config.input_price,
            "cached_input": config.cached_input_price,
            "output": config.output_price,
            "unpriced_total_only": config.unpriced_token_price,
        },
        "tokens": {
            "non_cached_input": non_cached_input,
            "cached_input": cached_input,
            "output": output,
            "unpriced_total_only": unpriced,
        },
        "input_cost": input_cost,
        "cached_input_cost": cached_input_cost,
        "output_cost": output_cost,
        "unpriced_total_only_cost": unpriced_cost,
        "total_cost": input_cost + cached_input_cost + output_cost + unpriced_cost,
        "note": "API estimate only; subscription plans may bill differently.",
    }


def attach_cost_estimates(report: dict, config: CostConfig) -> None:
    report["summary"]["cost_estimate"] = estimate_cost(report["summary"], config)
    report["cost_estimate"] = report["summary"]["cost_estimate"]
    for key in ("peak_day", "busiest_week"):
        row = report.get(key)
        if row:
            row["summary"]["cost_estimate"] = estimate_cost(row["summary"], config)
    for key in ("daily", "weekly"):
        for row in report.get(key, []):
            row["summary"]["cost_estimate"] = estimate_cost(row["summary"], config)


def group_events(events: list[TokenEvent], key_func):
    grouped: dict[object, list[TokenEvent]] = {}
    for event in events:
        grouped.setdefault(key_func(event), []).append(event)
    return grouped


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def daily_rows(events: list[TokenEvent]) -> list[dict]:
    rows = []
    for day, bucket in sorted(group_events(events, lambda event: event.day).items()):
        rows.append({"date": day, "summary": summarize(bucket)})
    return rows


def weekly_rows(events: list[TokenEvent], range_start: date, range_end: date) -> list[dict]:
    rows = []
    for start, bucket in sorted(group_events(events, lambda event: week_start(event.day)).items()):
        end = start + timedelta(days=6)
        rows.append(
            {
                "start": max(start, range_start),
                "end": min(end, range_end),
                "summary": summarize(bucket),
            }
        )
    return rows


def build_report(
    codex_home: pathlib.Path,
    start: date,
    end: date,
    events: list[TokenEvent],
    scanned_files: int,
    invalid_lines: int,
    include_archived: bool,
) -> dict:
    days = day_count(start, end)
    summary = summarize(events, days=days)
    daily = daily_rows(events)
    weekly = weekly_rows(events, start, end)
    return {
        "codex_home": str(codex_home),
        "start": start,
        "end": end,
        "days": days,
        "include_archived": include_archived,
        "files_scanned": scanned_files,
        "invalid_json_lines": invalid_lines,
        "summary": summary,
        "peak_day": max(daily, key=lambda row: row["summary"]["total"], default=None),
        "busiest_week": max(weekly, key=lambda row: row["summary"]["total"], default=None),
        "daily": daily,
        "weekly": weekly,
    }


def json_ready(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    return value


def format_number(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,.2f}"
    return f"{value:,}"


def format_percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def format_money(value: float, currency: str) -> str:
    if currency.upper() == "USD":
        return f"${value:,.2f}"
    return f"{value:,.2f} {currency}"


def format_price(value: float, currency: str) -> str:
    if currency.upper() == "USD":
        return f"${value:,.2f}"
    return f"{value:,.2f} {currency}"


def metric_rows(summary: dict, labels: dict) -> list[tuple[str, str, str]]:
    return [
        (labels["total"], format_number(summary["total"]), labels["total_note"]),
        (labels["input"], format_number(summary["input"]), labels["input_note"]),
        (
            labels["cached_input"],
            format_number(summary["cached_input"]),
            labels["cached_note"],
        ),
        (labels["output"], format_number(summary["output"]), labels["output_note"]),
        (
            labels["reasoning_output"],
            format_number(summary["reasoning_output"]),
            labels["reasoning_note"],
        ),
        (
            labels["non_cached_input"],
            format_number(summary["non_cached_input"]),
            labels["non_cached_note"],
        ),
        (labels["net_usage"], format_number(summary["net_usage"]), labels["net_note"]),
        (
            labels["cache_hit_rate"],
            format_percent(summary["cache_hit_rate"]),
            labels["cache_rate_note"],
        ),
        (
            labels["daily_average_total"],
            format_number(summary["daily_average_total"]),
            labels["daily_average_note"],
        ),
    ]


def cost_rows(cost: dict, labels: dict) -> list[tuple[str, str, str]]:
    currency = cost["currency"]
    prices = cost["prices_per_million_tokens"]
    tokens = cost["tokens"]
    rows = [
        (
            labels["cost_non_cached_input"],
            format_money(cost["input_cost"], currency),
            (
                f"{format_number(tokens['non_cached_input'])} tokens x "
                f"{format_price(prices['input'], currency)} / 1M"
            ),
        ),
        (
            labels["cost_cached_input"],
            format_money(cost["cached_input_cost"], currency),
            (
                f"{format_number(tokens['cached_input'])} tokens x "
                f"{format_price(prices['cached_input'], currency)} / 1M"
            ),
        ),
        (
            labels["cost_output"],
            format_money(cost["output_cost"], currency),
            (
                f"{format_number(tokens['output'])} tokens x "
                f"{format_price(prices['output'], currency)} / 1M"
            ),
        ),
    ]
    if tokens["unpriced_total_only"]:
        rows.append(
            (
                labels["cost_unpriced"],
                format_money(cost["unpriced_total_only_cost"], currency),
                (
                    f"{format_number(tokens['unpriced_total_only'])} tokens x "
                    f"{format_price(prices['unpriced_total_only'], currency)} / 1M; "
                    f"{labels['unpriced_note']}"
                ),
            )
        )
    rows.append(
        (
            labels["estimated_total_cost"],
            format_money(cost["total_cost"], currency),
            labels["cost_note"],
        )
    )
    return rows


def print_markdown(report: dict, language: str, show_daily: bool, show_cost: bool) -> None:
    labels = LABELS[language]
    summary = report["summary"]
    print(f"{labels['range']}: {report['start']} to {report['end']}")
    print(f"{labels['codex_home']}: {report['codex_home']}")
    print(f"{labels['files']}: {report['files_scanned']:,}")
    print(f"{labels['events']}: {summary['events']:,}")
    print(f"{labels['sessions']}: {summary['sessions']:,}")
    print()
    print(f"| {labels['metric']} | {labels['tokens']} | {labels['notes']} |")
    print("|---|---:|---|")
    for metric, tokens, note in metric_rows(summary, labels):
        print(f"| {metric} | {tokens} | {note} |")

    if summary["events"] == 0:
        print()
        print(labels["no_events"])
        return

    if show_cost:
        cost = summary["cost_estimate"]
        prices = cost["prices_per_million_tokens"]
        currency = cost["currency"]
        print()
        print(
            f"{labels['api_cost_estimate']}: "
            f"Input {format_price(prices['input'], currency)}/1M, "
            f"Cached input {format_price(prices['cached_input'], currency)}/1M, "
            f"Output {format_price(prices['output'], currency)}/1M."
        )
        print(f"| {labels['cost_item']} | {labels['amount']} | {labels['notes']} |")
        print("|---|---:|---|")
        for item, amount, note in cost_rows(cost, labels):
            print(f"| {item} | {amount} | {note} |")

    if report["peak_day"]:
        peak = report["peak_day"]
        print()
        print(
            f"{labels['peak_day']}: {peak['date']}, "
            f"{format_number(peak['summary']['total'])} tokens."
        )
    if report["busiest_week"]:
        peak = report["busiest_week"]
        print(
            f"{labels['busiest_week']}: {peak['start']} to {peak['end']}, "
            f"{format_number(peak['summary']['total'])} tokens."
        )

    if show_daily and report["daily"]:
        print()
        if show_cost:
            print(
                f"| Date | {labels['total']} | {labels['cost']} | "
                f"{labels['events']} | {labels['sessions']} |"
            )
            print("|---|---:|---:|---:|---:|")
        else:
            print(f"| Date | {labels['total']} | {labels['events']} | {labels['sessions']} |")
            print("|---|---:|---:|---:|")
        for row in report["daily"]:
            row_summary = row["summary"]
            if show_cost:
                print(
                    f"| {row['date']} | {format_number(row_summary['total'])} | "
                    f"{format_money(row_summary['cost_estimate']['total_cost'], row_summary['cost_estimate']['currency'])} | "
                    f"{row_summary['events']:,} | {row_summary['sessions']:,} |"
                )
            else:
                print(
                    f"| {row['date']} | {format_number(row_summary['total'])} | "
                    f"{row_summary['events']:,} | {row_summary['sessions']:,} |"
                )


def main() -> int:
    args = parse_args()
    tz = resolve_timezone(args.timezone)
    start, end = resolve_range(args, tz)
    codex_home = pathlib.Path(args.codex_home).expanduser()
    events, scanned_files, invalid_lines = collect_events(
        codex_home=codex_home,
        pattern=args.pattern,
        include_archived=args.include_archived,
        tz=tz,
        start=start,
        end=end,
    )
    report = build_report(
        codex_home=codex_home,
        start=start,
        end=end,
        events=events,
        scanned_files=scanned_files,
        invalid_lines=invalid_lines,
        include_archived=args.include_archived,
    )
    if args.show_cost:
        attach_cost_estimates(report, build_cost_config(args))
    if args.format == "json":
        print(json.dumps(json_ready(report), ensure_ascii=False, indent=2))
    else:
        print_markdown(report, args.language, args.show_daily, args.show_cost)
    return 0


if __name__ == "__main__":
    sys.exit(main())
