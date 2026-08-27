"""Canonical text normalization for database-backed search projections."""

import unicodedata


def normalize_search_text(value: str | None) -> str:
    """Return the exact value stored in searchable projection fields."""

    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFC", value).strip().casefold()
