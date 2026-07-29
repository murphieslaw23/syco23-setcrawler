from app.services.enricher import extract_field_candidates
from app.services.normalizer import duplicate_fingerprint, normalize_raw_payload


def test_extracts_artist_event_year_and_location_candidates() -> None:
    candidates = extract_field_candidates(
        "MURPH b2b ZMK @ South Side Teknival 2026",
        "Recorded at Turbinenhalle, Berlin on 18.05.2026.",
    )
    by_field = {}
    for candidate in candidates:
        by_field.setdefault(candidate.field_name, []).append(candidate.candidate_value)

    assert by_field["artist"] == ["MURPH", "ZMK"]
    assert "South Side Teknival" in by_field["event"]
    assert "2026" in by_field["year"]
    assert "Berlin" in by_field["city"]
    assert "2026-05-18" in by_field["date"]


def test_normalizes_youtube_payload_without_downloading_media() -> None:
    payload = normalize_raw_payload(
        "youtube",
        {
            "id": "abc123",
            "title": "23HZ LIVESET @ RITUAL FLOOR",
            "description": "Recorded in Berlin",
            "duration_seconds": 4_800,
            "published_at": "2026-05-16T20:00:00Z",
            "thumbnails": {"high": {"url": "https://img.youtube.com/high.jpg"}},
        },
    )

    assert payload.source_id == "abc123"
    assert payload.canonical_url == "https://www.youtube.com/watch?v=abc123"
    assert payload.primary_image_url == "https://img.youtube.com/high.jpg"


def test_duplicate_fingerprint_normalizes_title_and_duration_bucket() -> None:
    first = duplicate_fingerprint("MURPH — LIVESET @ TEKNIVAL", 5_401)
    second = duplicate_fingerprint("murph liveset at teknival", 5_420)

    assert first == second
