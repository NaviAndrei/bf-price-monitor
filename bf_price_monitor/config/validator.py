from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import jsonschema
from jsonschema.exceptions import best_match

SCHEMA_PATH = Path(__file__).parent / "schemas" / "watchlist_schema.json"


def validate_watchlist(watchlist_path: Path) -> None:
    """Raises ValueError with a human-readable path-specific message on failure.

    Accepts either the current production flat-array watchlist shape
    (``[{"site": ..., "query": ...}, ...]``) or the future Watch-model shape
    (``{"watches": [{"id": ..., "drop_rule": ..., ...}, ...]}``).
    """
    schema = json.loads(SCHEMA_PATH.read_text())
    data = json.loads(watchlist_path.read_text())
    validator = jsonschema.Draft7Validator(schema)
    errors = list(validator.iter_errors(data))
    if not errors:
        return
    error = best_match(errors)
    path = list(error.absolute_path)
    raise ValueError(
        f"Invalid watchlist configuration:\n  watchlist{path}: {error.message}"
    )


def load_watchlist(watchlist_path: Path) -> list[dict[str, Any]]:
    """Validates and normalizes a watchlist file into a flat list of entries.

    Legacy (flat-array) files are returned unchanged, which is the shape
    ``scrape.py`` already consumes. Modern (Watch-model) files return the
    contents of their ``watches`` array as-is; mapping those entries to a
    scrapeable site/query pair is a separate future migration, not something
    this loader does.
    """
    validate_watchlist(watchlist_path)
    data = json.loads(watchlist_path.read_text())
    if isinstance(data, list):
        return cast("list[dict[str, Any]]", data)
    return cast("list[dict[str, Any]]", data["watches"])
