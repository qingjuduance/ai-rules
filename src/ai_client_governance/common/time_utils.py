"""统一时间工具：所有模块共用的时间生成与解析入口。

替代各模块散写的 ``def utc_now(): ...`` / ``def now_iso(): ...`` /
``def parse_dt(): ...``，便于统一时区行为与时间序列化格式。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    """返回当前本地时区的 ISO 8601 字符串，精度到秒，带偏移。

    示例输出: ``2026-06-19T10:15:30+08:00``
    """
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# 兼容旧命名：历史上 utc_now() 实际返回本地时区时间
utc_now = now_iso


def parse_dt(value: str | None) -> datetime | None:
    """解析 ISO 8601 或简单日期时间字符串为带时区的 ``datetime``。

    解析失败或输入为空时返回 ``None``。输入无时区信息时按 UTC 解释。
    """
    if not value:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        normalized = normalized + "T00:00:00"
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def clean_time(value: Any, label: str, *, default_now: bool = True) -> str:
    """规范化时间字段文本。

    为空时按 ``default_now`` 决定回填当前时间或报错。
    """
    text = "" if value is None else str(value).strip()
    if not text:
        if not default_now:
            raise ValueError(f"{label} is required")
        return now_iso()
    return text


def time_order_key(value: str) -> tuple[int, ...]:
    """把 ISO 时间字符串转成可排序的元组。"""
    parsed = parse_dt(value)
    if parsed is None:
        return (0,)
    return (int(parsed.timestamp()),)


__all__ = ["now_iso", "utc_now", "parse_dt", "clean_time", "time_order_key"]
