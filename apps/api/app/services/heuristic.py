from pydantic import BaseModel, Field


class HeuristicConfig(BaseModel):
    minimum_duration_seconds: int = 1_200
    review_threshold: float = 0.4
    auto_accept_threshold: float = 0.7
    strong_keywords: list[str] = [
        "liveset",
        "live set",
        "dj set",
        "djset",
        "b2b",
        "back to back",
        "@",
        "rave",
        "teknival",
        "free party",
        "freetekno",
        "mix",
        "mixtape",
        "recording",
        "recorded at",
        "boiler room",
    ]
    genre_keywords: list[str] = [
        "hardtek",
        "tekno",
        "tribe",
        "acid",
        "industrial",
        "breakcore",
    ]
    medium_keywords: list[str] = []
    negative_keywords: list[str] = [
        "official video",
        "music video",
        "lyric video",
        "single",
        " ep ",
        "album",
        "beatport",
        "bandcamp",
        "tutorial",
        "how to",
        "review",
    ]


class ScoreResult(BaseModel):
    score: float = Field(ge=0, le=1)
    accepted: bool
    auto_accept: bool
    reasons: list[str]


def _duration_score(duration_seconds: int) -> float:
    if duration_seconds < 1_200:
        return 0.0
    if duration_seconds < 1_800:
        return 0.2
    if duration_seconds < 3_600:
        return 0.4
    if duration_seconds <= 7_200:
        return 0.6
    return 0.8


def calculate_set_score(
    title: str,
    duration_seconds: int,
    config: HeuristicConfig,
) -> ScoreResult:
    haystack = f" {title.casefold()} "
    if duration_seconds < config.minimum_duration_seconds:
        return ScoreResult(
            score=0,
            accepted=False,
            auto_accept=False,
            reasons=["duration_below_minimum"],
        )

    duration = _duration_score(duration_seconds)
    strong_matches = [term for term in config.strong_keywords if term in haystack]
    medium_matches = [term for term in config.medium_keywords if term in haystack]
    genre_matches = [term for term in config.genre_keywords if term in haystack]
    negative_matches = [term.strip() for term in config.negative_keywords if term in haystack]

    strong_score = min(0.6, len(strong_matches) * 0.3)
    medium_score = min(0.3, len(medium_matches) * 0.1)
    genre_score = min(0.3, len(genre_matches) * 0.1)
    negative_score = len(negative_matches) * 0.3
    score = round(min(1.0, max(0.0, duration + strong_score + medium_score + genre_score - negative_score)), 3)
    reasons = [f"duration:{duration}"]
    reasons.extend(f"signal:{item}" for item in strong_matches + medium_matches + genre_matches)
    reasons.extend(f"negative:{item}" for item in negative_matches)
    return ScoreResult(
        score=score,
        accepted=score >= config.review_threshold,
        auto_accept=score >= config.auto_accept_threshold,
        reasons=reasons,
    )
