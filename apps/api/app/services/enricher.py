import re
from datetime import datetime

from app.schemas.candidate import CandidateCreate


KNOWN_CITIES = (
    "Berlin",
    "Hamburg",
    "Leipzig",
    "Dresden",
    "Prague",
    "Vienna",
    "Brussels",
    "Paris",
    "Amsterdam",
)


def _candidate(field: str, value: str, confidence: float, source: str) -> CandidateCreate:
    return CandidateCreate(
        field_name=field,
        candidate_value=value.strip(" -–—,"),
        confidence=confidence,
        source=source,
    )


def extract_field_candidates(title: str, description: str | None = None) -> list[CandidateCreate]:
    candidates: list[CandidateCreate] = []
    description = description or ""
    title_without_year = re.sub(r"\b(?:19|20)\d{2}\b", "", title).strip()
    before_event, separator, after_event = title_without_year.partition("@")

    artist_part = re.sub(r"\([^)]*(?:live|set|mix)[^)]*\)", "", before_event, flags=re.I)
    for artist in re.split(r"\s+(?:b2b|vs\.?)\s+|\s*[&+,]\s*", artist_part, flags=re.I):
        cleaned = re.sub(r"\b(?:liveset|live set|dj set|mix)\b", "", artist, flags=re.I).strip(" -–—")
        if cleaned:
            candidates.append(_candidate("artist", cleaned, 0.86, "title_regex"))

    if separator and after_event.strip():
        candidates.append(_candidate("event", after_event, 0.82, "title_regex"))

    year_match = re.search(r"\b((?:19|20)\d{2})\b", f"{title} {description}")
    if year_match:
        candidates.append(_candidate("year", year_match.group(1), 0.92, "title_regex"))

    date_match = re.search(r"\b(\d{2})[./](\d{2})[./]((?:19|20)\d{2})\b", description)
    if date_match:
        try:
            parsed = datetime.strptime(
                ".".join(date_match.groups()),
                "%d.%m.%Y",
            ).date().isoformat()
        except ValueError:
            parsed = None
        if parsed is not None:
            candidates.append(
                _candidate(
                    "date",
                    parsed,
                    0.94,
                    "description_regex",
                )
            )

    venue_match = re.search(r"(?:recorded\s+at|at)\s+([^,\n]+),\s*([A-Za-zÀ-ÿ -]+)", description, re.I)
    if venue_match:
        candidates.append(_candidate("venue", venue_match.group(1), 0.72, "description_regex"))

    combined = f"{title} {description}"
    for city in KNOWN_CITIES:
        if re.search(rf"\b{re.escape(city)}\b", combined, re.I):
            candidates.append(_candidate("city", city, 0.84, "description_regex"))
            break

    unique: dict[tuple[str, str], CandidateCreate] = {}
    for item in candidates:
        unique[(item.field_name, item.candidate_value.casefold())] = item
    return list(unique.values())
