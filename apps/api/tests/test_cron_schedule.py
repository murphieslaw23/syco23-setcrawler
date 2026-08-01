from datetime import UTC, datetime

import pytest

from app.services.cron_schedule import (
    cron_matches,
    next_cron_time,
    previous_cron_time,
    validate_timezone,
)


def test_cron_uses_profile_timezone_and_skips_nonexistent_dst_minute() -> None:
    assert cron_matches(
        "30 2 * * *",
        datetime(2026, 3, 28, 1, 30, tzinfo=UTC),
        timezone="Europe/Berlin",
    )
    assert next_cron_time(
        "30 2 * * *",
        datetime(2026, 3, 28, 1, 30, tzinfo=UTC),
        timezone="Europe/Berlin",
    ) == datetime(2026, 3, 30, 0, 30, tzinfo=UTC)


def test_previous_cron_time_finds_the_most_recent_missed_occurrence() -> None:
    assert previous_cron_time(
        "0 6 * * *",
        datetime(2026, 8, 1, 8, 15, tzinfo=UTC),
        timezone="UTC",
    ) == datetime(2026, 8, 1, 6, 0, tzinfo=UTC)


def test_cron_day_of_month_and_weekday_follow_standard_or_semantics() -> None:
    # Saturday 1 August matches the day-of-month even though it is not Monday.
    assert cron_matches(
        "0 6 1 * 1",
        datetime(2026, 8, 1, 6, 0, tzinfo=UTC),
    )


def test_timezone_validation_rejects_unknown_or_ambiguous_values() -> None:
    assert validate_timezone("Europe/Berlin") == "Europe/Berlin"
    with pytest.raises(ValueError, match="schedule_timezone"):
        validate_timezone("local")
