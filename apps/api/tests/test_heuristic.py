from app.services.heuristic import HeuristicConfig, calculate_set_score


def test_rejects_short_media_without_strong_keyword_override() -> None:
    result = calculate_set_score("New hardtek single", 780, HeuristicConfig())

    assert result.accepted is False
    assert result.score == 0.0
    assert "duration_below_minimum" in result.reasons


def test_scores_long_liveset_for_review() -> None:
    result = calculate_set_score(
        "MURPH liveset @ South Side Teknival 2026 hardtek",
        5_400,
        HeuristicConfig(),
    )

    assert result.score == 1.0
    assert result.accepted is True
    assert result.auto_accept is True


def test_negative_track_signals_reduce_score() -> None:
    clean = calculate_set_score("23HZ DJ set acid", 3_000, HeuristicConfig())
    negative = calculate_set_score(
        "23HZ DJ set acid official music video single",
        3_000,
        HeuristicConfig(),
    )

    assert negative.score < clean.score
    assert negative.auto_accept is False
