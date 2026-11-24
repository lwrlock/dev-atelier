"""Lightweight 5-field Cron parser and next-run UTC calculator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def _parse_field(field: str, min_val: int, max_val: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"Empty cron segment in {field!r}")
        if "/" in part:
            base, step_str = part.split("/", 1)
            step = int(step_str)
            if step <= 0:
                raise ValueError("Cron step must be positive")
        else:
            base, step = part, 1

        if base == "*":
            start, end = min_val, max_val
        elif "-" in base:
            s_str, e_str = base.split("-", 1)
            start, end = int(s_str), int(e_str)
        else:
            val = int(base)
            start, end = val, val if "/" not in part else max_val

        if start < min_val or end > max_val or start > end:
            raise ValueError(f"Cron value out of range [{min_val},{max_val}]: {part!r}")
        for v in range(start, end + 1, step):
            values.add(v)
    return values


@dataclass(frozen=True, slots=True)
class CronSpec:
    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0=Sunday .. 6=Saturday (matching standard cron)


def parse_cron(expr: str) -> CronSpec:
    parts = (expr or "").strip().split()
    if len(parts) != 5:
        raise ValueError(f"Cron expression must have 5 fields: {expr!r}")
    return CronSpec(
        expr=" ".join(parts),
        minutes=frozenset(_parse_field(parts[0], 0, 59)),
        hours=frozenset(_parse_field(parts[1], 0, 23)),
        days=frozenset(_parse_field(parts[2], 1, 31)),
        months=frozenset(_parse_field(parts[3], 1, 12)),
        weekdays=frozenset(_parse_field(parts[4], 0, 6)),
    )


def next_cron_run(spec: CronSpec, after: datetime | None = None) -> datetime:
    """Compute the next UTC trigger minute strictly after `after`."""
    cur = (after or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(second=0, microsecond=0)
    cur += timedelta(minutes=1)
    # Bounded scan up to 366 days ahead
    for _ in range(366 * 24 * 60):
        # Python weekday(): Monday=0..Sunday=6 -> standard cron 0=Sunday..6=Saturday
        cron_wd = (cur.weekday() + 1) % 7
        if (
            cur.minute in spec.minutes
            and cur.hour in spec.hours
            and cur.day in spec.days
            and cur.month in spec.months
            and cron_wd in spec.weekdays
        ):
            return cur
        cur += timedelta(minutes=1)
    raise ValueError(f"No matching run within 366 days for cron: {spec.expr}")
