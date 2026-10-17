"""Read, validate and write data/watchlist.json (modern format only)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from bf_price_monitor.config.validator import load_watchlist

Doc = dict[str, Any]


class StoreError(Exception):
    """The file is unreadable, legacy-shaped, or fails the project validator."""


def load(path: Path) -> Doc:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StoreError(f"cannot read {path.name}: {exc}") from exc
    if isinstance(data, list):
        raise StoreError(
            "watchlist is in the legacy flat-array format; run "
            "scripts/migrate_watchlist_to_modern.py first"
        )
    if not isinstance(data, dict) or not isinstance(data.get("watches"), list):
        raise StoreError("watchlist must be an object with a 'watches' array")
    validate(data)
    return data


def validate(doc: Doc) -> None:
    """Run the same validator the bot uses (it takes a Path, so via a temp file)."""
    fd, name = tempfile.mkstemp(suffix=".json")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_dump(doc))
        load_watchlist(tmp)
    except ValueError as exc:
        raise StoreError(str(exc)) from exc
    finally:
        tmp.unlink(missing_ok=True)


def save(path: Path, doc: Doc) -> None:
    """Validate, then replace the file atomically. LF line endings."""
    validate(doc)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(_dump(doc))
    os.replace(tmp, path)


def _dump(doc: Doc) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
