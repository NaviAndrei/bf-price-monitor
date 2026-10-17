"""Watch identity, matching scripts/migrate_watchlist_to_modern.py."""

from __future__ import annotations

import uuid


def watch_id(site: str, query: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{site}:{query}"))


def duplicate_key(site: str, query: str) -> str:
    """Case- and whitespace-insensitive key (same rule as qa_watchlist.py)."""
    return f"{site}:{' '.join(query.lower().split())}"
