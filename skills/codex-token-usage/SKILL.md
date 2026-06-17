---
name: codex-token-usage
description: Summarize local Codex Desktop or Codex CLI token usage from session JSONL logs. Use when the user asks to count, audit, total, compare, or report Codex token usage for today, this week, this month, last month, a calendar month, a rolling N-day window, peak day, busiest week, cached input, output, reasoning output, cache hit rate, or net token usage.
---

# Codex Token Usage

## Overview

Use the bundled script to read local Codex session logs and produce a consistent
token usage report. Prefer script output over ad hoc `rg` or manual JSONL
summaries.

## Workflow

1. Identify the reporting window from the user request.
   - If the user asks for "one month" without naming a calendar month, use the
     last 30 local calendar days ending today.
   - If the user asks for "this month" or names a month, use that calendar
     month, clipped to today if it is the current month.
   - Use the user's timezone from context when available; otherwise use the
     local machine timezone.
2. Run `scripts/codex_token_usage.py` from this skill directory or by absolute
   path.
3. Use `--show-cost` when the user asks about money, cost, bill, price, API
   estimate, or spend. Report it as an API price estimate, not a subscription
   invoice.
4. Report the Markdown table directly for user-facing answers.
5. State the net usage formula when summarizing: `Input - Cached input + Output`.
6. Include the peak day and busiest week with exact dates when events exist.

## Script

Common commands:

```bash
python scripts/codex_token_usage.py --days 30 --timezone Asia/Shanghai
python scripts/codex_token_usage.py --month 2026-04 --timezone Asia/Shanghai
python scripts/codex_token_usage.py --start 2026-04-01 --end 2026-04-30
python scripts/codex_token_usage.py --days 30 --format json
python scripts/codex_token_usage.py --codex-home ~/.codex --days 30
python scripts/codex_token_usage.py --days 3 --show-cost --show-daily
python scripts/codex_token_usage.py --days 3 --show-cost --input-price 5 --cached-input-price 0.5 --output-price 30
```

Use `--format json` when another script, report, dashboard, or automation will
consume the result. Use Markdown for direct user answers.

Default cost parameters are API estimate defaults for the current Codex
`gpt-5.5` pricing shape: Input `$5.00/1M`, Cached input `$0.50/1M`, Output
`$30.00/1M`, and total-only events charged at the input price. Override them
with `--input-price`, `--cached-input-price`, `--output-price`, and
`--unpriced-token-price` when official pricing or the selected model changes.

## Definitions

- `total`: sum of `last_token_usage.total_tokens` across `token_count` events.
- `input`: sum of `last_token_usage.input_tokens`.
- `cached input`: sum of `last_token_usage.cached_input_tokens`.
- `output`: sum of `last_token_usage.output_tokens`.
- `reasoning output`: sum of `last_token_usage.reasoning_output_tokens`.
- `non-cached input`: `input - cached input`.
- `net usage`: `non-cached input + output`.
- `cache hit rate`: `cached input / input`.
- `daily average total`: `total / calendar days in the reporting range`.
- `unpriced total-only`: tokens from `token_count` events where
  `total_tokens` exists but input/output fields are absent; these are included
  in `total` and estimated separately for cost.
- `cost estimate`: `(non-cached input * input price + cached input * cached
  input price + output * output price + unpriced total-only * unpriced token
  price) / 1,000,000`.

Do not sum `total_token_usage` for each event. That field is cumulative within a
session and will overcount if added repeatedly.

## Privacy

The script reads local session JSONL files only. It does not upload logs, auth
data, SQLite databases, or usage reports. Do not paste raw session log content
into the final response; report aggregated metrics only.

## Response Format

Use a concise Markdown table. Localize surrounding prose to the user's language.
For Chinese answers, keep technical metric names such as `Input`,
`Cached input`, `Output`, and `Reasoning output` recognizable.

When `--show-cost` is used, mention that the amount is an API price estimate.
ChatGPT/Codex subscription plans may consume plan credits or limits instead of
billing the displayed dollar amount directly.

Mention when no `token_count` events were found for the selected range, because
that may mean the logs are old, absent, or outside the requested date window.
