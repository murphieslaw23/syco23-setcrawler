from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _field_matches(value: int, expression: str, *, minimum: int, maximum: int) -> bool:
    for part in expression.split(","):
        step = 1
        base = part
        if "/" in part:
            base, raw_step = part.split("/", 1)
            step = int(raw_step)
            if step < 1:
                raise ValueError("cron step must be positive")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            raw_start, raw_end = base.split("-", 1)
            start, end = int(raw_start), int(raw_end)
        else:
            start = end = int(base)
        if start < minimum or end > maximum or start > end:
            raise ValueError("cron value out of range")
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


def validate_timezone(value: str) -> str:
    if value != value.strip() or not value or value.casefold() == "local":
        raise ValueError("schedule_timezone must be an IANA timezone")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError("schedule_timezone must be an IANA timezone") from error
    return value


def cron_matches(
    expression: str,
    value: datetime,
    *,
    timezone: str = "UTC",
) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("schedule_cron must contain five fields")
    if value.tzinfo is None:
        raise ValueError("cron comparison must be timezone-aware")
    local_value = value.astimezone(ZoneInfo(validate_timezone(timezone)))
    minute, hour, day, month, weekday = fields
    cron_weekday = (local_value.weekday() + 1) % 7
    day_matches = _field_matches(local_value.day, day, minimum=1, maximum=31)
    weekday_matches = _field_matches(cron_weekday, weekday, minimum=0, maximum=6)
    if day == "*":
        calendar_day_matches = weekday_matches
    elif weekday == "*":
        calendar_day_matches = day_matches
    else:
        calendar_day_matches = day_matches or weekday_matches
    return all(
        (
            _field_matches(local_value.minute, minute, minimum=0, maximum=59),
            _field_matches(local_value.hour, hour, minimum=0, maximum=23),
            calendar_day_matches,
            _field_matches(local_value.month, month, minimum=1, maximum=12),
        )
    )


def next_cron_time(
    expression: str,
    after: datetime,
    *,
    timezone: str = "UTC",
) -> datetime:
    candidate = after.astimezone(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if cron_matches(expression, candidate, timezone=timezone):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("schedule_cron has no occurrence within one year")


def previous_cron_time(
    expression: str,
    at: datetime,
    *,
    timezone: str = "UTC",
) -> datetime:
    candidate = at.astimezone(UTC).replace(second=0, microsecond=0)
    for _ in range(366 * 24 * 60):
        if cron_matches(expression, candidate, timezone=timezone):
            return candidate
        candidate -= timedelta(minutes=1)
    raise ValueError("schedule_cron has no occurrence within one year")


def validate_cron(expression: str) -> str:
    next_cron_time(expression, datetime(2026, 1, 1, tzinfo=UTC))
    return expression
