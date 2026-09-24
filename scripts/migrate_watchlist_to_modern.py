"""Migrate data/watchlist.json's legacy flat-array format to the modern Watch-model shape.

Read-only against the source JSON. `owner` and `seller_policy` are NOT
present anywhere in the legacy format — this script introduces them as new
values (DEFAULT_WATCH_OWNER / DEFAULT_SELLER_POLICY below), it does not
derive them from existing data. `target_price` and `min_drop_percent` are
carried over so should_alert()'s existing dual-gate behavior (target_price
OR min_drop_percent) is preserved exactly; `cooldown_hours` is NOT carried
over or mapped to `cadence_minutes` — cadence keeps the Watch model's own
default. `site` is carried over 1:1 (see #56 / docs/DECISIONS.md
2026-09-24) so scrape.py's item["site"] adapter-selection read keeps
working against a promoted modern-format watchlist.

`id` is a deterministic uuid5 of "{site}:{query}", matching the same
derivation notify.py already uses for watch_id in AlertDecision — so a
migrated Watch's id lines up with alerts already flowing through the
runtime path.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bf_price_monitor.domain import Watch  # noqa: E402

DEFAULT_WATCH_OWNER = "NaviAndrei"
DEFAULT_SELLER_POLICY = "any"


def _watch_id(site: str, query: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{site}:{query}")


def convert(entry: dict) -> Watch:
    site = entry["site"]
    query = entry["query"]
    return Watch(
        id=_watch_id(site, query),
        owner=DEFAULT_WATCH_OWNER,
        site=site,
        query=query,
        target_price=entry.get("target_price"),
        min_drop_percent=entry.get("min_drop_percent"),
        track_all_time_low=True,
        seller_policy=DEFAULT_SELLER_POLICY,
    )


def _watch_to_dict(watch: Watch) -> dict:
    # model_dump_json() renders Decimal as a JSON string (e.g. "2500.0"),
    # but watchlist_schema.json requires target_price/min_drop_percent as
    # bare numbers. Decimal stays the correct type on the Watch model
    # itself for exact arithmetic; only this JSON write needs floats.
    data = watch.model_dump(mode="json", exclude_none=True)
    for field in ("target_price", "min_drop_percent"):
        value = getattr(watch, field)
        if value is not None:
            data[field] = float(value)
    return data


def migrate(source: Path, dry_run: bool) -> dict:
    with source.open(encoding="utf-8") as f:
        legacy = json.load(f)

    watches = [convert(entry) for entry in legacy]
    modern = {"watches": [_watch_to_dict(watch) for watch in watches]}
    return {"legacy_count": len(legacy), "modern": modern, "dry_run": dry_run}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=REPO_ROOT / "data" / "watchlist.json"
    )
    parser.add_argument(
        "--target", type=Path, default=REPO_ROOT / "data" / "watchlist.json"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = migrate(args.source, args.dry_run)

    print(f"Legacy entries read: {result['legacy_count']}")
    print(f"Modern entries produced: {len(result['modern']['watches'])}")
    print(json.dumps(result["modern"], indent=2))

    if args.dry_run:
        print("\n[dry-run] target not written")
        return 0

    with args.target.open("w", encoding="utf-8") as f:
        json.dump(result["modern"], f, indent=2)
        f.write("\n")
    print(f"\nWrote modern watchlist to {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
