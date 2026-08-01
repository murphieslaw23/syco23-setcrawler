from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from app.schemas.merge import MergeComponentScores, MergeScore
from app.schemas.set import SetDetail


MERGE_SUGGESTION_THRESHOLD = 0.68
_NOISE = {
    "dj",
    "full",
    "live",
    "liveset",
    "mix",
    "official",
    "recording",
    "set",
}


def _tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    return {
        token
        for token in re.findall(r"[a-z0-9]+", normalized)
        if token not in _NOISE
    }


def _similarity(left: str | None, right: str | None) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _best_similarity(left: Iterable[str], right: Iterable[str]) -> float:
    return max(
        (
            _similarity(left_value, right_value)
            for left_value in left
            for right_value in right
        ),
        default=0,
    )


def _year(record: SetDetail) -> int | None:
    if record.year is not None:
        return record.year
    if record.published_at is not None:
        return record.published_at.year
    return None


def _aliases(record: SetDetail) -> list[str]:
    values = record.raw_payload.get("artist_aliases", [])
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str)]


def score_set_merge(left: SetDetail, right: SetDetail) -> MergeScore:
    artist_score = _best_similarity(left.artist_names, right.artist_names)
    title_artist = max(_similarity(left.title, right.title), artist_score)
    event = _similarity(left.event_name, right.event_name)

    left_year = _year(left)
    right_year = _year(right)
    if left_year is None or right_year is None:
        date_year = 0
    elif left_year == right_year:
        date_year = 1
    elif abs(left_year - right_year) == 1:
        date_year = 0.4
    else:
        date_year = 0

    durations = (left.duration_seconds, right.duration_seconds)
    if durations[0] and durations[1]:
        duration = max(
            0,
            1 - abs(durations[0] - durations[1]) / max(durations),
        )
    else:
        duration = 0

    left_aliases = _aliases(left)
    right_aliases = _aliases(right)
    aliases = max(
        _best_similarity(left_aliases, right.artist_names),
        _best_similarity(right_aliases, left.artist_names),
        _best_similarity(left_aliases, right_aliases),
    )
    components = MergeComponentScores(
        title_artist=round(title_artist, 6),
        event=round(event, 6),
        date_year=round(date_year, 6),
        duration=round(duration, 6),
        aliases=round(aliases, 6),
    )
    score = round(
        components.title_artist * 0.4
        + components.event * 0.2
        + components.date_year * 0.15
        + components.duration * 0.15
        + components.aliases * 0.1,
        6,
    )
    reasons: list[str] = []
    if components.aliases >= 0.75:
        reasons.append("alias_match")
    if components.date_year >= 0.8:
        reasons.append("date_year_match")
    if components.duration >= 0.9:
        reasons.append("duration_close")
    if components.event >= 0.75:
        reasons.append("event_match")
    if components.title_artist >= 0.75:
        reasons.append("title_artist_match")
    return MergeScore(
        score=score,
        components=components,
        reasons=sorted(reasons),
    )
