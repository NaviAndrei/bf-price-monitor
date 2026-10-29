"""Read-only, on-demand product search across the repo's live shops.

Takes a product name, asks each registered (non-placeholder) shop once through
its existing scraper, and prints what was found. Nothing is written to
data/: no price history, alerts, health logs or watchlist changes. The only
optional write is the explicit --cache-dir. Design: docs/design/product-search.md.

Usage:
    .venv/Scripts/python.exe scripts/search_product.py "ssd 1tb" --json

Exit codes: 0 if at least one shop answered (ok or no_match), 1 if none did,
2 for invalid usage.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent))

import scrape  # noqa: E402

from bf_price_monitor.identity import (  # noqa: E402
    HIGH_CONFIDENCE,
    MATCH_THRESHOLD,
    _fold,
    match_titles,
    normalize_title,
)

CURRENCY = "RON"
SHOP_PAUSE_SECONDS = 5.0
DEFAULT_CACHE_TTL = 900
ERROR_CACHE_TTL = 90
QUERY_MIN_LENGTH = 2
QUERY_MAX_LENGTH = 80
QUERY_EXTRA_CHARS = " -.,"

# Shops ruled out by the owner. Altex is registered only as a placeholder; the
# rest are not registered. Listed so a future registration cannot add them.
EXCLUDED_SHOPS = frozenset(
    {"evomag", "forit", "cel", "altex", "mediagalaxy", "compari"}
)

STATUS_OK = "ok"
STATUS_NO_MATCH = "no_match"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "error"
STATUS_UNSUPPORTED = "unsupported"
ANSWERED_STATUSES = frozenset({STATUS_OK, STATUS_NO_MATCH})
LISTING_MISSING_DETAIL = "listing never appeared"

OVERRIDDEN_ATTRIBUTES = (
    "MAX_FETCH_ATTEMPTS",
    "SCRAPER_RETRY_ATTEMPTS",
    "EXTRACTION_FAILURES_FILE",
    "DEBUG_DIR",
    "title_matches_query",
)

_UNITS = frozenset({"gb", "tb", "mb", "ghz", "mhz", "hz", "inch", "w", "mah"})
_NUMBER = re.compile(r"^\d+(\.\d+)?$")
_WORD = re.compile(r"[^\W_]+(?:\.[^\W_]+)*")
_HYPHENATED = re.compile(r"[^\W_]+(?:-[^\W_]+)+")
_CAPACITY = re.compile(r"^\d+(gb|tb)$")
_REFURBISHED = frozenset({"resigilat", "refurbished", "renewed", "reconditionat"})


# -- query validation -------------------------------------------------------


def validate_query(raw: str) -> str:
    query = " ".join(raw.split())
    if not QUERY_MIN_LENGTH <= len(query) <= QUERY_MAX_LENGTH:
        raise ValueError(
            f"query must be {QUERY_MIN_LENGTH}-{QUERY_MAX_LENGTH} characters"
        )
    bad = sorted(
        {
            ch
            for ch in query
            if not (
                ch.isalpha()
                or (ch.isascii() and ch.isdigit())
                or ch in QUERY_EXTRA_CHARS
            )
        }
    )
    if bad:
        raise ValueError(
            "query may only contain letters, digits, space and '-' '.' ','; "
            f"found {''.join(bad)!r}"
        )
    if not any(ch.isalnum() for ch in query):
        raise ValueError("query needs at least one letter or digit")
    return query


# -- loose matching ---------------------------------------------------------


def _fold_text(text: str) -> str:
    return re.sub(r"(?<=\d),(?=\d)", ".", _fold(text))


def _merge_units(words: list[str]) -> list[str]:
    merged: list[str] = []
    for word in words:
        if word in _UNITS and merged and _NUMBER.match(merged[-1]):
            merged[-1] += word
        else:
            merged.append(word)
    return merged


def _title_tokens(title: str) -> set[str]:
    folded = _fold_text(title)
    words = _WORD.findall(folded)
    tokens = set(words) | set(_merge_units(words))
    tokens.update(m.replace("-", "") for m in _HYPHENATED.findall(folded))
    return tokens


def make_loose_matcher(
    strict: Callable[[str, str], bool],
) -> Callable[[str, str], bool]:
    """A superset of ``strict``: folds case, diacritics, hyphens and unit
    spacing, and never drops a query word."""

    def loose(title: str, query: str) -> bool:
        if strict(title, query):
            return True
        folded_words = _fold_text(query).split()
        if not folded_words:
            return False
        merged = _merge_units(folded_words)
        title_tokens = _title_tokens(title)
        for word in merged:
            parts = _WORD.findall(word)
            if not parts:
                return False  # nothing to fold: strict alone decides
            if all(part in title_tokens for part in parts):
                continue
            if "-" in word and "".join(parts) in title_tokens:
                continue
            return False
        return True

    return loose


# -- scrape.py overrides ----------------------------------------------------


def apply_overrides(
    workdir: Path, *, loose_matcher: Callable[[str, str], bool] | None
) -> dict[str, Any]:
    saved = {name: getattr(scrape, name) for name in OVERRIDDEN_ATTRIBUTES}
    scrape.MAX_FETCH_ATTEMPTS = 1
    scrape.SCRAPER_RETRY_ATTEMPTS = 1
    scrape.EXTRACTION_FAILURES_FILE = workdir / "extraction_failures.jsonl"
    scrape.DEBUG_DIR = workdir / "debug"
    if loose_matcher is not None:
        setattr(scrape, "title_matches_query", loose_matcher)
    return saved


def restore_overrides(saved: dict[str, Any]) -> None:
    for name, value in saved.items():
        setattr(scrape, name, value)


# -- per-shop search --------------------------------------------------------


def _snapshot(shop: str) -> dict[str, int]:
    state = scrape._run_state
    shots = len(list(Path(scrape.DEBUG_DIR).glob(f"{shop}-*.png")))
    return {
        "challenge": state.challenge_counts.get(shop, 0),
        "rate_limit": state.rate_limit_hit_counts.get(shop, 0),
        "attempts": state.fetch_attempt_counts.get(shop, 0),
        "failures": state.failure_counts.get(shop, 0),
        "shots": shots,
    }


def _clean_offers(
    shop: str,
    rows: list[dict[str, Any]],
    query: str,
    strict: Callable[[str, str], bool],
    fetched_at: str,
) -> list[dict[str, Any]]:
    by_url: dict[str, dict[str, Any]] = {}
    for row in rows:
        price = row.get("price")
        title = row.get("title")
        url = row.get("url")
        if price is None or not title or not url:
            continue
        canonical = scrape.canonicalize_url(url)
        offer = {
            "shop": shop,
            "title": title,
            "price": float(price),
            "currency": CURRENCY,
            "stock_status": row.get("stock_status", "unknown"),
            "seller": row.get("seller") or "",
            "is_marketplace": bool(row.get("is_marketplace")),
            "url": canonical,
            "fetched_at": fetched_at,
            "match": "strict" if strict(title, query) else "normalized",
            "group_id": None,
            "group_confidence": None,
        }
        known = by_url.get(canonical)
        if known is None or offer["price"] < known["price"]:
            by_url[canonical] = offer
    return list(by_url.values())


def search_shop(
    shop: str,
    scraper: Callable[[str], list[dict[str, Any]]],
    query: str,
    strict: Callable[[str, str], bool],
    now: datetime,
) -> dict[str, Any]:
    before = _snapshot(shop)
    rows: list[dict[str, Any]] = []
    failure: Exception | None = None
    try:
        with contextlib.redirect_stdout(sys.stderr):
            rows = scraper(query)
    except Exception as exc:  # any scraper fault is a per-shop result
        failure = exc
    after = _snapshot(shop)
    delta = {key: after[key] - before[key] for key in after}
    fetched_at = now.isoformat()
    offers = _clean_offers(shop, rows, query, strict, fetched_at)
    listing_missing = (
        isinstance(failure, scrape.ListingReadinessTimeout) or delta["shots"] > 0
    )
    blocked = delta["challenge"] > 0 or delta["rate_limit"] > 0

    detail = ""
    if offers:
        status = STATUS_OK
    elif blocked:
        status = STATUS_BLOCKED
        detail = "challenge, 403 or 429 seen; not retried"
    elif listing_missing:
        status = STATUS_NO_MATCH
        detail = LISTING_MISSING_DETAIL
    elif failure is not None:
        status = STATUS_ERROR
        detail = f"{type(failure).__name__}: {failure}"[:200]
    elif delta["attempts"] == 0:
        status = STATUS_ERROR
        detail = "no response received"
    elif delta["failures"] > 0:
        status = STATUS_ERROR
        detail = "matched cards had no extractable price"
    else:
        status = STATUS_NO_MATCH
        detail = "no matching offers"
    return {
        "shop": shop,
        "status": status,
        "detail": detail,
        "offers": offers,
        "from_cache": False,
        "fetched_at": fetched_at,
        "signals": {
            "challenge": delta["challenge"],
            "rate_limit": delta["rate_limit"],
            "attempts": delta["attempts"],
        },
    }


# -- cache ------------------------------------------------------------------


def cache_key(shop: str, query: str, mode: str) -> str:
    folded = " ".join(_fold(query).split())
    return hashlib.sha256(f"{shop}\x1f{folded}\x1f{mode}".encode()).hexdigest()[:32]


def cache_ttl_for(status: str, ok_ttl: int) -> int:
    if status == STATUS_BLOCKED:
        return int(scrape.RATE_LIMIT_COOLDOWN_MINUTES * 60)
    if status == STATUS_ERROR:
        return ERROR_CACHE_TTL
    return ok_ttl


def load_cache(
    cache_dir: Path, key: str, now: datetime, ok_ttl: int
) -> dict[str, Any] | None:
    path = cache_dir / f"{key}.json"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        stored_at = datetime.fromisoformat(entry["stored_at"])
        status = entry["result"]["status"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if stored_at.tzinfo is None:
        return None
    if status in ANSWERED_STATUSES and ok_ttl <= 0:
        return None
    if (now - stored_at).total_seconds() >= cache_ttl_for(status, ok_ttl):
        return None
    result: dict[str, Any] = entry["result"]
    result["from_cache"] = True
    return result


def store_cache(
    cache_dir: Path, key: str, result: dict[str, Any], now: datetime
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{key}.json"
    tmp = path.with_suffix(".tmp")
    payload = {"stored_at": now.isoformat(), "result": result}
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# -- shop selection ---------------------------------------------------------


def _shop_name(raw: str) -> str:
    name = raw.strip().lower()
    name = name.removeprefix("www.").removesuffix(".ro")
    return name


def select_shops(
    sites: list[str] | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    registry = scrape.SCRAPERS
    live = [
        key
        for key, func in registry.items()
        if key not in EXCLUDED_SHOPS and not scrape.is_placeholder_scraper(func)
    ]
    if sites is None:
        return live, []
    chosen: list[str] = []
    unsupported: list[dict[str, Any]] = []
    for raw in sites:
        name = _shop_name(raw)
        if not name or name in chosen:
            continue
        if name in EXCLUDED_SHOPS:
            reason = "shop is excluded"
        elif name not in registry:
            reason = "unknown shop"
        elif name not in live:
            reason = "placeholder scraper (out of scope)"
        else:
            chosen.append(name)
            continue
        unsupported.append(
            {
                "shop": name,
                "status": STATUS_UNSUPPORTED,
                "detail": reason,
                "offers": [],
                "from_cache": False,
                "fetched_at": None,
                "signals": {"challenge": 0, "rate_limit": 0, "attempts": 0},
            }
        )
    return chosen, unsupported


# -- grouping ---------------------------------------------------------------


def _capacities(title: str) -> frozenset[str]:
    return frozenset(t for t in normalize_title(title).tokens if _CAPACITY.match(t))


def _is_refurbished(title: str) -> bool:
    return bool(normalize_title(title).tokens & _REFURBISHED)


def _link_score(a: str, b: str) -> float | None:
    cap_a, cap_b = _capacities(a), _capacities(b)
    if cap_a and cap_b and cap_a != cap_b:
        return None
    if _is_refurbished(a) != _is_refurbished(b):
        return None
    result = match_titles(a, b)
    return result.score if result.score >= MATCH_THRESHOLD else None


def group_offers(offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Suggest cross-shop equivalences. Offers must already be price-sorted.
    Complete-link: a new offer joins a group only if it matches every member
    (all from other shops). Mutates group_id/group_confidence on the offers."""
    clusters: list[list[dict[str, Any]]] = []
    scores: list[list[float]] = []
    for offer in offers:
        best_index = -1
        best_scores: list[float] = []
        for index, members in enumerate(clusters):
            if any(m["shop"] == offer["shop"] for m in members):
                continue
            links = [_link_score(offer["title"], m["title"]) for m in members]
            if any(link is None for link in links):
                continue
            found = [link for link in links if link is not None]
            if best_index < 0 or min(found) > min(best_scores):
                best_index, best_scores = index, found
        if best_index < 0:
            clusters.append([offer])
            scores.append([])
        else:
            clusters[best_index].append(offer)
            scores[best_index].extend(best_scores)

    groups: list[dict[str, Any]] = []
    for members, member_scores in zip(clusters, scores, strict=True):
        if len(members) < 2:
            continue
        group_id = f"g{len(groups) + 1}"
        low = min(member_scores)
        confidence = "high" if low >= HIGH_CONFIDENCE else "medium"
        for member in members:
            member["group_id"] = group_id
            member["group_confidence"] = confidence
        groups.append(
            {
                "group_id": group_id,
                "confidence": confidence,
                "shops": sorted({m["shop"] for m in members}),
                "score": round(low, 4),
                "reasons": ["TITLE_MATCH", "SUGGESTED_EQUIVALENCE"],
            }
        )
    return groups


# -- orchestration ----------------------------------------------------------


def _count_lines(path: Path) -> int:
    try:
        with path.open(encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def run_search(
    query: str,
    *,
    sites: list[str] | None = None,
    strict: bool = False,
    cache_dir: Path | None = None,
    cache_ttl: int = DEFAULT_CACHE_TTL,
    delay: bool = True,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    query = validate_query(query)
    mode = "strict" if strict else "loose"
    shops, unsupported = select_shops(sites)
    strict_matcher = scrape.title_matches_query  # captured before any override
    loose_matcher = None if strict else make_loose_matcher(strict_matcher)
    started = now_fn()
    results: list[dict[str, Any]] = list(unsupported)
    failure_rows = 0

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        workdir = Path(tmp)
        saved = apply_overrides(workdir, loose_matcher=loose_matcher)
        try:
            scrape._run_state.reset(f"search-{started:%Y%m%dT%H%M%SZ}")
            live_calls = 0
            for shop in shops:
                key = cache_key(shop, query, mode)
                if cache_dir is not None:
                    cached = load_cache(cache_dir, key, now_fn(), cache_ttl)
                    if cached is not None:
                        results.append(cached)
                        continue
                if live_calls and delay:
                    sleep_fn(SHOP_PAUSE_SECONDS)
                live_calls += 1
                result = search_shop(
                    shop, scrape.SCRAPERS[shop], query, strict_matcher, now_fn()
                )
                results.append(result)
                if cache_dir is not None:
                    try:
                        store_cache(cache_dir, key, result, now_fn())
                    except OSError as exc:
                        print(f"warning: could not write cache: {exc}", file=sys.stderr)
        finally:
            try:
                scrape._browser_state.close()
            finally:
                failure_rows = _count_lines(workdir / "extraction_failures.jsonl")
                restore_overrides(saved)

    offers = [o for r in results for o in r["offers"]]
    offers.sort(key=lambda o: (o["price"], o["shop"], o["title"]))
    groups = group_offers(offers)  # matching runs in price order
    offers.sort(
        key=lambda o: (
            o["stock_status"] == "out_of_stock",
            o["price"],
            o["shop"],
            o["title"],
        )
    )
    shop_rows = [
        {
            "shop": r["shop"],
            "status": r["status"],
            "detail": r["detail"],
            "offer_count": len(r["offers"]),
            "from_cache": r["from_cache"],
            "fetched_at": r["fetched_at"],
            "signals": r["signals"],
        }
        for r in results
    ]
    return {
        "query": query,
        "mode": mode,
        "generated_at": started.isoformat(),
        "shops": shop_rows,
        "offers": offers,
        "groups": groups,
        "diagnostics": {"extraction_failure_rows": failure_rows},
    }


def exit_code(report: dict[str, Any]) -> int:
    answered = any(
        s["status"] == STATUS_OK
        or (s["status"] == STATUS_NO_MATCH and s["detail"] != LISTING_MISSING_DETAIL)
        for s in report["shops"]
    )
    return 0 if answered else 1


# -- output -----------------------------------------------------------------


def _use_utf8_stdout() -> None:
    # Windows pipes and redirects default to cp1252, which cannot encode some
    # scraped titles and would turn Romanian letters into "?". UTF-8 keeps them,
    # so consumers of the output must decode it as UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def format_report(report: dict[str, Any]) -> str:
    lines = [f"Search: {report['query']!r} ({report['mode']} matching)", ""]
    lines.append("Shops")
    for shop in report["shops"]:
        note = f" - {shop['detail']}" if shop["detail"] else ""
        cached = " [cached]" if shop["from_cache"] else ""
        lines.append(
            f"  {shop['shop']:<10} {shop['status']:<12} "
            f"{shop['offer_count']} offer(s){cached}{note}"
        )
    lines.append("")
    if report["offers"]:
        lines.append("Offers (cheapest first)")
        for offer in report["offers"]:
            group = (
                f" [{offer['group_id']} {offer['group_confidence']}]"
                if offer["group_id"]
                else ""
            )
            tag = "" if offer["match"] == "strict" else " ~"
            flags = (" [out]" if offer["stock_status"] == "out_of_stock" else "") + (
                " [mkt]" if offer.get("is_marketplace") else ""
            )
            lines.append(
                f"  {offer['price']:>10.2f} {offer['currency']}  "
                f"{offer['shop']:<9} {offer['stock_status']:<15}"
                f"{offer['title']}{tag}{flags}{group}"
            )
            lines.append(f"             {offer['url']}")
        if any(o["match"] != "strict" for o in report["offers"]):
            lines.append("  (~ = matched only after normalisation)")
    else:
        lines.append("No offers found.")
    rows = report["diagnostics"]["extraction_failure_rows"]
    if rows:
        lines.append("")
        lines.append(f"Note: {rows} matched card(s) had no extractable price.")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only product search across the repo's live shops."
    )
    parser.add_argument("query", nargs="+", help="product name to search for")
    parser.add_argument("--sites", help="comma-separated shops, e.g. emag,flanco")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="use the monitor's exact title matching (no folding of case, "
        "diacritics, hyphens or unit spacing)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON to stdout")
    parser.add_argument(
        "--no-delay",
        action="store_true",
        help="skip the 5 s pause between shops (local use only)",
    )
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=DEFAULT_CACHE_TTL,
        help=f"seconds to reuse ok/no_match results (default {DEFAULT_CACHE_TTL}); "
        "0 ignores them",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="enable the on-disk cache in this folder (the only possible write)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sites = args.sites.split(",") if args.sites is not None else None
    if args.no_delay and args.cache_dir is None:
        print(
            "no pause and no cache: repeated runs will hit the shops again",
            file=sys.stderr,
        )
    try:
        report = run_search(
            " ".join(args.query),
            sites=sites,
            strict=args.strict,
            cache_dir=args.cache_dir,
            cache_ttl=args.cache_ttl,
            delay=not args.no_delay,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.sites is not None and not report["shops"]:
        print("error: --sites needs at least one shop", file=sys.stderr)
        return 2
    _use_utf8_stdout()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
