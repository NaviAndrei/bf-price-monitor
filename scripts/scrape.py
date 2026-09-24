# scripts/scrape.py
import functools
import hashlib
import json
import random
import re
import sys
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from pydantic import ValidationError

from bf_price_monitor.config import load_watchlist
from bf_price_monitor.domain import Observation
from bf_price_monitor.storage.sqlite import init_db
from bf_price_monitor.storage.sqlite import (
    record_observation as sqlite_record_observation,
)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
REQUEST_TIMEOUT = 15

# Playwright's own navigation-timeout default is 30s; the listing pages this
# scrapes are simple search results, so 25s is plenty and fails fast instead
# of tying up the runner when a site truly stalls.
PLAYWRIGHT_NAV_TIMEOUT_MS = 25_000

# Aborted outright to cut bandwidth/time: none of these resource types affect
# the DOM data (title/price/stock) this scraper reads out of listing pages.
TRACKING_DOMAINS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "facebook.com",
    "hotjar.com",
    "clarity.ms",
    "criteo.com",
    "criteo.net",
)

# Scraper-level retry: guards against unhandled exceptions inside a whole
# scrape_*_listing() call (e.g. a page layout change BeautifulSoup can't
# navigate), separate from the 403/429 retry already inside fetch()/
# fetch_with_browser().
SCRAPER_RETRY_ATTEMPTS = 2
SCRAPER_RETRY_BASE_DELAY = 3  # seconds, plus random jitter


def with_retry(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        for attempt in range(1, SCRAPER_RETRY_ATTEMPTS + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if attempt == SCRAPER_RETRY_ATTEMPTS:
                    raise
                delay = SCRAPER_RETRY_BASE_DELAY + random.uniform(0, 1)
                print(
                    f"[{func.__name__}] failed ({e.__class__.__name__}: {e}), "
                    f"retrying in {delay:.1f}s (attempt {attempt + 1}/{SCRAPER_RETRY_ATTEMPTS})"
                )
                time.sleep(delay)

    return wrapper


_BLOCKED_RESOURCE_RE = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|woff2?|css|ico)(\?|$)", re.IGNORECASE
)


def _block_heavy_requests(route):
    # A single catch-all handler, rather than one route per pattern: Playwright
    # runs multiple matching handlers last-registered-first, so splitting this
    # into separate page.route() calls risks a later "**/*" registration
    # short-circuiting an earlier, more specific one before it ever runs.
    request = route.request
    url = request.url
    if _BLOCKED_RESOURCE_RE.search(url):
        return route.abort()
    if request.resource_type == "script" and any(
        domain in url for domain in TRACKING_DOMAINS
    ):
        return route.abort()
    return route.continue_()


# 403/429 can mean a transient rate-limit spike rather than a hard block, so
# it's worth a few spaced-out retries before giving up — unlike a persistent
# Cloudflare challenge page, which no amount of retrying clears.
# 1 initial fetch + 3 retries at 5s/10s/20s = worst case ~35s extra per site
# (negligible against GitHub Actions' 360-minute default job timeout, even
# with all 4 sites hitting this path in the same run).
RETRY_STATUS_CODES = (403, 429)
RETRY_BACKOFFS = [5, 10, 20]  # seconds before each retry
MAX_FETCH_ATTEMPTS = 1 + len(RETRY_BACKOFFS)

WATCHLIST_FILE = Path("data/watchlist.json")
HISTORY_FILE = Path("data/price_history.json")
DB_FILE = Path("data/price_history.db")
ALERTS_FILE = Path("data/alerts.json")
EXTRACTION_FAILURES_FILE = Path("data/extraction_failures.jsonl")
SCRAPE_HEALTH_FILE = Path("data/scrape_health.jsonl")
SCRAPE_HEALTH_ALERTS_FILE = Path("data/scrape_health_alerts.json")
HISTORY_SCHEMA_VERSION = 1

# T-09: a store with zero matches for two consecutive runs while at least one
# other store succeeded in the same run is "Critical Selector Drift" (a
# broken selector/site redesign) rather than a transient network blip.
DEAD_MAN_THRESHOLD_HOURS = 24
STORE_DISPLAY_NAMES = {
    "emag": "eMAG",
    "pcgarage": "PC Garage",
    "flanco": "Flanco",
    "altex": "Altex",
}


class _RunState:
    # Run-scoped signals that fetch()/fetch_with_browser() and
    # log_extraction_failure() report up to main() without changing any of
    # those functions' parameters or return shapes (T-07/T-08 own that
    # contract). Reset at the top of every main() call, so leftover state
    # from a prior run/test never leaks into the next one.
    def __init__(self) -> None:
        self.run_id = ""
        self.challenge_counts: dict[str, int] = {}
        self.failure_counts: dict[str, int] = {}

    def reset(self, run_id: str) -> None:
        self.run_id = run_id
        self.challenge_counts = {}
        self.failure_counts = {}


_run_state = _RunState()


def _record_challenge(site_name: str) -> None:
    _run_state.challenge_counts[site_name] = (
        _run_state.challenge_counts.get(site_name, 0) + 1
    )


def load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {}
    data = json.load(open(HISTORY_FILE, encoding="utf-8"))
    if "schema_version" not in data:
        # Pre-versioning file: bare {url: entry} dict. Wrap it so this run's
        # save writes the versioned format without losing any prior history.
        data = {"schema_version": HISTORY_SCHEMA_VERSION, "products": data}
    return _canonicalize_product_keys(data["products"])


def _canonicalize_product_keys(products: dict) -> dict:
    # T-06: existing price_history.json entries predate URL canonicalization
    # and are keyed by the raw scraped URL (e.g. with "www." and a trailing
    # slash). Remapping them here means an already-tracked product keeps
    # updating under its new canonical key instead of forking into a fresh,
    # history-less duplicate the next time main() runs.
    canonical: dict[str, dict] = {}
    for raw_key, entry in products.items():
        key = canonicalize_url(raw_key)
        if key not in canonical:
            canonical[key] = entry
        else:
            # Only reached if two raw keys already aliased the same product
            # (e.g. a prior tracking-param duplicate) — combine rather than
            # silently drop one entry's history.
            canonical[key] = _merge_product_entries(canonical[key], entry)
    return canonical


def _merge_product_entries(a: dict, b: dict) -> dict:
    merged = dict(a)
    history = list(a.get("history", []))
    seen = {
        (h["date"], h.get("observed_at"), h["price"], h["stock_status"])
        for h in history
    }
    for h in b.get("history", []):
        signature = (h["date"], h.get("observed_at"), h["price"], h["stock_status"])
        if signature not in seen:
            history.append(h)
            seen.add(signature)
    history.sort(key=lambda h: (h["date"], h.get("observed_at") or ""))
    merged["history"] = history

    lows = [e["all_time_low"] for e in (a, b) if "all_time_low" in e]
    if lows:
        merged["all_time_low"] = min(lows)
    highs = [e["all_time_high"] for e in (a, b) if "all_time_high" in e]
    if highs:
        merged["all_time_high"] = max(highs)
    first_seens = [e["first_seen"] for e in (a, b) if "first_seen" in e]
    if first_seens:
        merged["first_seen"] = min(first_seens)
    return merged


def save_history(products: dict) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    json.dump(
        {"schema_version": HISTORY_SCHEMA_VERSION, "products": products},
        open(HISTORY_FILE, "w", encoding="utf-8"),
        indent=2,
        ensure_ascii=False,
    )


def is_challenge_page(html: str) -> bool:
    marker = html[:2000].lower()
    return (
        "just a moment" in marker
        or "cf-chl" in marker
        or "challenges.cloudflare.com" in marker
    )


def parse_price(raw: str) -> float | None:
    # Romanian retailers format prices as "1.234,56 Lei"/"RON": "." is a
    # thousands separator, "," is the decimal separator.
    if not raw:
        return None
    cleaned = re.sub(r"(?i)(lei|ron)", "", raw).strip()
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


# T-07: layered extraction, tried in the order CLAUDE.md specifies —
# JSON-LD structured data, then semantic HTML attributes, then the existing
# CSS selectors (kept below, untouched, as the final structured fallback).
# One shared implementation per layer rather than per-store logic, since the
# schema.org/microdata/OG conventions these layers read are the same across
# every store's Shopify/WooCommerce/Magento-class markup.


def _parse_semantic_price_value(raw: str | None) -> float | None:
    if not raw:
        return None
    raw = raw.strip()
    # schema.org/microdata price values are usually plain decimals
    # ("1798.99"); parse_price()'s Romanian "1.234,56" rules would mis-read
    # that as 179899.0, so a plain float is tried first and Romanian-format
    # text (e.g. a data-price copied straight from the visible label) falls
    # back to parse_price().
    try:
        return float(raw)
    except ValueError:
        return parse_price(raw)


def extract_semantic_price(card) -> float | None:
    # Second layer: stable semantic markers that survive a CSS class rename,
    # scoped to the individual card (a listing page has one price per
    # product, so this must not read page-wide Open Graph tags meant for a
    # single-product page).
    for selector, attr in (
        ('[itemprop="price"]', "content"),
        ("[data-price]", "data-price"),
    ):
        el = card.select_one(selector)
        if el is None:
            continue
        raw = el.get(attr) or el.get_text(strip=True)
        price = _parse_semantic_price_value(raw)
        if price is not None:
            return price
    return None


def _flatten_jsonld_nodes(data) -> list[dict]:
    # Unwraps the handful of shapes real sites use: a bare Product object, a
    # list of top-level objects, an @graph wrapper, or an ItemList whose
    # itemListElement entries each carry (or point at) a Product.
    nodes: list[dict] = []
    top_level = data if isinstance(data, list) else [data]
    for item in top_level:
        if not isinstance(item, dict):
            continue
        nodes.append(item)
        graph = item.get("@graph")
        if isinstance(graph, list):
            nodes.extend(n for n in graph if isinstance(n, dict))
        if item.get("@type") == "ItemList":
            for element in item.get("itemListElement") or []:
                if not isinstance(element, dict):
                    continue
                inner = element.get("item", element)
                if isinstance(inner, dict):
                    nodes.append(inner)
    return nodes


def _jsonld_node_to_product(node: dict) -> dict | None:
    if node.get("@type") != "Product":
        return None
    offer = node.get("offers")
    if isinstance(offer, list):
        offer = offer[0] if offer else None
    if not isinstance(offer, dict):
        return None
    price = _parse_semantic_price_value(
        str(offer["price"]) if offer.get("price") is not None else None
    )
    availability = offer.get("availability") or ""
    in_stock = None
    if isinstance(availability, str):
        if "OutOfStock" in availability or "SoldOut" in availability:
            in_stock = False
        elif "InStock" in availability or "LimitedAvailability" in availability:
            in_stock = True
    return {
        "name": node.get("name"),
        "url": node.get("url") or offer.get("url"),
        "price": price,
        "in_stock": in_stock,
    }


def extract_jsonld_products(soup: BeautifulSoup) -> list[dict]:
    # First layer: schema.org Product/Offer markup, read once per page
    # rather than per card since <script type="application/ld+json"> tags
    # sit outside any individual product card in the DOM.
    products = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _flatten_jsonld_nodes(data):
            product = _jsonld_node_to_product(node)
            if product is not None:
                products.append(product)
    return products


def match_jsonld_product(products: list[dict], title: str, url: str) -> dict | None:
    # Matched by canonical URL first (the reliable identity key elsewhere in
    # this module), falling back to an exact case-insensitive name match for
    # markup where the JSON-LD entry's url differs from the visible link
    # (e.g. a canonical product URL vs. a tracking-param listing link).
    for product in products:
        if product.get("url") and canonicalize_url(product["url"]) == canonicalize_url(
            url
        ):
            return product
    normalized_title = title.strip().lower()
    for product in products:
        if (product.get("name") or "").strip().lower() == normalized_title:
            return product
    return None


def resolve_layered_price(
    card, jsonld_products: list[dict], title: str, url: str
) -> float | None:
    # Tries JSON-LD then semantic attributes; the caller still owns the CSS
    # selector attempt (its final, store-specific fallback) and the AI-stub/
    # failure-capture step once every layer here returns None.
    matched = match_jsonld_product(jsonld_products, title, url)
    if matched is not None and matched.get("price") is not None:
        return matched["price"]
    return extract_semantic_price(card)


def extract_with_ai(query: str, url: str) -> None:
    # Tier-4 (optional/placeholder) per Issue #17 and CLAUDE.md's extraction
    # hierarchy: Sprint 1 scope is a stub only. rule_verdict's deterministic
    # primacy applies here too — this must never produce a price that
    # bypasses the JSON-LD/semantic/CSS layers above it.
    print(f"AI extraction not implemented (query={query!r}, url={url})")


def log_extraction_failure(store: str, url: str, query: str, reason: str) -> None:
    # Every layer (JSON-LD, semantic, CSS, AI stub) failed for a candidate
    # card — recorded so a fully broken selector set is visible instead of
    # silently indistinguishable from "no matching products this run".
    # HTML is intentionally not persisted here (may contain no PII in
    # practice, but keeping this log lean and out of scope for redaction).
    EXTRACTION_FAILURES_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "store": store,
        "url": url,
        "query": query,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "failure_reason": reason,
        "run_id": _run_state.run_id,
    }
    with open(EXTRACTION_FAILURES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    _run_state.failure_counts[store] = _run_state.failure_counts.get(store, 0) + 1


# Retailer-added tracking/session params that don't change product identity —
# stripped so a tracking-link variant of the same listing doesn't fork its
# history into a duplicate offer (T-06).
TRACKING_QUERY_PARAMS = {
    "ref",
    "cmpid",
    "emag_click_id",
    "fbclid",
    "gclid",
    "gclsrc",
    "msclkid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "spm",
    "_ga",
}


def canonicalize_url(url: str) -> str:
    # Identity key for history/offer tracking: lowercase host with any
    # leading "www." dropped, no trailing slash on the path, and tracking
    # query params (utm_*, ref, fbclid, gclid, ...) stripped, with the
    # remaining params sorted for a stable ordering. Two URLs differing only
    # in tracking params canonicalize to the same key.
    parsed = urlsplit(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    kept_params = sorted(
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_QUERY_PARAMS
    )
    query = urlencode(kept_params)
    return urlunsplit((parsed.scheme, host, path, query, ""))


def title_matches_query(title: str, query: str) -> bool:
    # Retailer search is fuzzy/token-based, so a query like "iphone 15" can
    # surface "Xiaomi 15T" (matches "15") or "Honor 600" (matches nothing but
    # still ranks). Requiring every query word to appear in the title rejects
    # those false matches while still allowing normal word-order/case drift
    # between the search query and the listed title.
    title_words = title.lower().split()
    return all(word in title_words for word in query.lower().split())


def fetch_with_browser(url: str, site_name: str) -> str | None:
    # For sites whose Cloudflare challenge blocks plain `requests` outright
    # (403 on every attempt, even from a residential IP). A stealth-patched
    # headless Chromium clears the JS-fingerprint half of the challenge;
    # IP reputation is already covered since this runs on the self-hosted
    # runner. Still only reads listing/search pages — same scope as fetch().
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    html = None
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            with Stealth().use_sync(sync_playwright()) as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent=HEADERS["User-Agent"])
                page.set_default_navigation_timeout(PLAYWRIGHT_NAV_TIMEOUT_MS)
                page.route("**/*", _block_heavy_requests)
                response = page.goto(
                    url,
                    timeout=PLAYWRIGHT_NAV_TIMEOUT_MS,
                    wait_until="domcontentloaded",
                )
                status = response.status if response else None

                # Cloudflare-challenge polling loop — unrelated to the retry
                # below, left exactly as before. This waits out a JS
                # challenge within a single attempt; the retry loop instead
                # re-attempts the whole fetch after a 403/429 status.
                deadline = time.time() + REQUEST_TIMEOUT
                html = page.content()
                while is_challenge_page(html) and time.time() < deadline:
                    time.sleep(1)
                    html = page.content()
                browser.close()
        except Exception as e:
            print(
                f"[{site_name}] playwright fetch failed ({e.__class__.__name__}), skipping this run"
            )
            return None

        if status in RETRY_STATUS_CODES and attempt < MAX_FETCH_ATTEMPTS:
            delay = RETRY_BACKOFFS[attempt - 1]
            print(
                f"[{site_name}] got {status}, retrying in {delay}s "
                f"(attempt {attempt + 1}/{MAX_FETCH_ATTEMPTS})"
            )
            time.sleep(delay)
            continue
        break

    if is_challenge_page(html):
        print(
            f"[{site_name}] still blocked by bot-challenge after Playwright wait, skipping this run"
        )
        _record_challenge(site_name)
        return None
    return html


def fetch(url: str, site_name: str) -> str | None:
    r = None
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            print(
                f"[{site_name}] unreachable ({e.__class__.__name__}), skipping this run"
            )
            return None

        if r.status_code in RETRY_STATUS_CODES and attempt < MAX_FETCH_ATTEMPTS:
            delay = RETRY_BACKOFFS[attempt - 1]
            print(
                f"[{site_name}] got {r.status_code}, retrying in {delay}s "
                f"(attempt {attempt + 1}/{MAX_FETCH_ATTEMPTS})"
            )
            time.sleep(delay)
            continue
        break

    if r.status_code in (403, 503) or is_challenge_page(r.text):
        print(
            f"[{site_name}] hit a bot-challenge page (status {r.status_code}), skipping this run"
        )
        if is_challenge_page(r.text):
            _record_challenge(site_name)
        return None
    if r.status_code != 200:
        print(f"[{site_name}] unexpected status {r.status_code}, skipping this run")
        return None
    return r.text


def emag_stock_status(card) -> str:
    # eMAG's search listing (verified live 2026-09-07 against "iphone 15" and
    # "laptop lenovo v15", 60+54 real cards checked) only ever surfaces two
    # data-availability-id values for matched cards: "3" ("în stoc" - plenty)
    # and "2" ("ultimul produs in stoc" - last unit, a real limited-stock
    # signal). No out-of-stock marker text ("stoc epuizat", "indisponibil")
    # appeared anywhere on either page, so eMAG appears to exclude sold-out
    # offers from search results entirely. The text check below is kept as a
    # defensive fallback in case that changes; "unknown" covers any card
    # whose markup doesn't match either.
    text = card.get_text(" ", strip=True).lower()
    if "stoc epuizat" in text or "indisponibil" in text:
        return "out_of_stock"
    availability_id = card.get("data-availability-id")
    if availability_id == "2" or "ultimul produs" in text:
        return "limited_stock"
    if availability_id == "3" or "in stoc" in text:
        return "in_stock"
    return "unknown"


def scrape_emag_listing(query: str) -> list[dict]:
    # Listing page only: /search/<query> redirects to a /<category>/c page.
    # robots.txt disallows /product/ — never requested here.
    # Card HTML (verified 2026-09-06):
    #   <div class="card-item card-standard ..." data-availability-id="3">
    #     <a class="card-v2-title ..." href="https://www.emag.ro/.../pd/...">Title</a>
    #     <p class="product-new-price">3&#46;999<sup><small class="mf-decimal">&#44;</small>99</sup> <span>Lei</span></p>
    #     ... "în stoc" / "ultimul produs in stoc" text near the price ...
    url = f"https://www.emag.ro/search/{query.replace(' ', '+')}"
    html = fetch(url, "emag")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    jsonld_products = extract_jsonld_products(soup)
    results = []
    for card in soup.select(".card-item"):
        title_el = card.select_one(".card-v2-title")
        price_el = card.select_one(".product-new-price")
        if not (title_el and title_el.get("href")):
            continue
        title_text = title_el.get_text(strip=True)
        if not title_matches_query(title_text, query):
            continue
        card_url = title_el["href"]
        price = resolve_layered_price(card, jsonld_products, title_text, card_url)
        if price is None and price_el:
            price = parse_price(price_el.get_text(strip=True))
        if price is None:
            extract_with_ai(query, card_url)
            log_extraction_failure(
                "emag", card_url, query, "no price found in any extraction layer"
            )
            continue
        results.append(
            {
                "title": title_text,
                "price": price,
                "url": card_url,
                "stock_status": emag_stock_status(card),
                # eMAG's search/listing page carries no seller identity
                # anywhere in the card markup (verified live 2026-09-10:
                # no vendor/seller text or data-* attribute on any card) —
                # only individual product pages show "Vandut si livrat de",
                # and robots.txt disallows /product/. Recorded as unknown
                # rather than assumed, since guessing "eMAG" would be wrong
                # for any card actually fulfilled by a marketplace seller.
                "seller": None,
                "is_marketplace": None,
            }
        )
    return results


def pcgarage_stock_status(card) -> str:
    # PC Garage marks every listed card with a `.product_box_availability`
    # div whose class is "instock", "insupplierstock", or "outofstock"
    # (verified live 2026-09-07 and re-verified 2026-09-10 across 4 queries,
    # which surfaced all three). Re-verification on 2026-09-10 found the
    # "instock" class covers two different texts: "Stoc magazin suficient"
    # (plenty) and "Stoc magazin limitat" (limited) — the class alone can't
    # tell them apart, so the text has to be checked too. "insupplierstock"
    # ("In stoc furnizor") means still orderable but fulfilled by the
    # supplier rather than PC Garage's own warehouse.
    el = card.select_one(".product_box_availability")
    if not el:
        return "unknown"
    classes = el.get("class") or []
    text = el.get_text(strip=True).lower()
    if "outofstock" in classes:
        return "out_of_stock"
    if "insupplierstock" in classes:
        return "supplier_stock"
    if "instock" in classes:
        return "limited_stock" if "limitat" in text else "in_stock"
    return "unknown"


def scrape_pcgarage_listing(query: str) -> list[dict]:
    # Listing page only: /cauta/?q=<query> is PC Garage's search-results page.
    # robots.txt disallows /detalii-produs/ (product pages) — never requested here.
    # Card HTML (verified 2026-09-06):
    #   <div class="product_box">
    #     <div class="product_box_name"><h2><a href="https://www.pcgarage.ro/...">Title</a></h2></div>
    #     <div class="product_box_price_container"><div class="pb-price"><p class="price">1.798,99 RON</p></div></div>
    #     <div class="product_box_availability instock">Stoc magazin suficient</div>
    url = f"https://www.pcgarage.ro/cauta/?q={query.replace(' ', '+')}"
    html = fetch_with_browser(url, "pcgarage")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    jsonld_products = extract_jsonld_products(soup)
    results = []
    for card in soup.select(".product_box"):
        title_el = card.select_one(".product_box_name a")
        price_el = card.select_one(".product_box_price_container p.price")
        if not (title_el and title_el.get("href")):
            continue
        title_text = title_el.get_text(strip=True)
        if not title_matches_query(title_text, query):
            continue
        card_url = title_el["href"]
        price = resolve_layered_price(card, jsonld_products, title_text, card_url)
        if price is None and price_el:
            price = parse_price(price_el.get_text(strip=True))
        if price is None:
            extract_with_ai(query, card_url)
            log_extraction_failure(
                "pcgarage", card_url, query, "no price found in any extraction layer"
            )
            continue
        results.append(
            {
                "title": title_text,
                "price": price,
                "url": card_url,
                "stock_status": pcgarage_stock_status(card),
                # PC Garage sells first-party only, no marketplace program.
                "seller": "PC Garage",
                "is_marketplace": False,
            }
        )
    return results


def flanco_stock_status(card) -> str:
    # Flanco marks every real listed card with a `.stocky-txt` span inside
    # `.produs-status .stock` whose class is the actual state (verified live
    # 2026-09-07, re-verified 2026-09-10 against 3 queries, 65+ real cards
    # checked — every one of them had this marker present): "in-stock"
    # ("In stoc"), "limited-stock" ("Stoc limitat"), "supplier-stock"
    # ("Exclusiv online" - fulfilled by the supplier rather than Flanco's own
    # stock), "bin-display" ("Expus in magazin" - in-store display unit, not
    # separately confirmed online). No out-of-stock class was observed on any
    # verification query, so Flanco appears to exclude sold-out products from
    # search results entirely; the "out-of-stock"/"sold-out" check below is a
    # defensive fallback in case that changes.
    el = card.select_one(".stocky-txt")
    if not el:
        return "unknown"
    classes = el.get("class") or []
    if any("out-of-stock" in c or "sold-out" in c for c in classes):
        return "out_of_stock"
    if "limited-stock" in classes:
        return "limited_stock"
    if "supplier-stock" in classes:
        return "supplier_stock"
    if "in-stock" in classes or "bin-display" in classes:
        return "in_stock"
    return "unknown"


def scrape_flanco_listing(query: str) -> list[dict]:
    # Listing page only: /catalogsearch/result/?q=<query> redirects to a
    # category listing page (Magento). robots.txt itself is Cloudflare-
    # challenge-gated on this site, so is_challenge_page() below is the
    # real gate — it caught nothing on 2 verified fetches, but expect it
    # to trigger intermittently in production.
    # Card HTML (verified 2026-09-06):
    #   <li class="item product product-item produs">
    #     <a class="product-item-link" href="https://www.flanco.ro/....html"><h2>Title</h2></a>
    #     <div class="price-box price-final_price">
    #       <span class="special-price"><span class="price">2.398,<sup class="decimal">99</sup> lei</span></span>
    #     <div class="produs-status"><div class="stock">
    #       <span class="stocky-txt in-stock">In stoc</span></div></div>
    url = f"https://www.flanco.ro/catalogsearch/result/?q={query.replace(' ', '+')}"
    html = fetch_with_browser(url, "flanco")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    jsonld_products = extract_jsonld_products(soup)
    results = []
    for card in soup.select("li.product-item"):
        title_el = card.select_one(".product-item-link")
        price_el = card.select_one(".price-box.price-final_price .special-price .price")
        # OUG 27/2022 requires retailers to publish the lowest price from the
        # last 30 days when a product is discounted. Despite that legal
        # intent, this markup ("pretVechi"/"pricePrp") is Flanco's own
        # claimed strike-through reference price (PRP) — a retailer-supplied
        # figure, not independently verified here. It is stored below as
        # reference_price for display/audit purposes only; thirty_day_low
        # must always be computed from our own recorded history in
        # data/price_history.json, never from this field.
        reference_el = card.select_one(".pretVechi .pricePrp .price")
        if not (title_el and title_el.get("href")):
            continue
        title_text = title_el.get_text(strip=True)
        if not title_matches_query(title_text, query):
            continue
        card_url = title_el["href"]
        price = resolve_layered_price(card, jsonld_products, title_text, card_url)
        if price is None and price_el:
            price = parse_price(price_el.get_text(strip=True))
        if price is None:
            extract_with_ai(query, card_url)
            log_extraction_failure(
                "flanco", card_url, query, "no price found in any extraction layer"
            )
            continue
        result = {
            "title": title_text,
            "price": price,
            "url": card_url,
            "stock_status": flanco_stock_status(card),
            # Flanco sells first-party only, no marketplace program.
            "seller": "Flanco",
            "is_marketplace": False,
        }
        if reference_el:
            reference_price = parse_price(reference_el.get_text(strip=True))
            if reference_price is not None:
                result["reference_price"] = reference_price
        results.append(result)
    return results


def scrape_altex_listing(query: str) -> list[dict]:
    # altex.ro is fronted by Akamai and stalls the TLS handshake for a plain
    # `requests` client (confirmed unreachable from two independent
    # networks). Tested 2026-09-07 with curl_cffi's impersonate="chrome120",
    # which DOES clear the TLS-layer stall (200 OK in <1s) — but that only
    # proves the block is a separate problem from what's actually missing:
    # altex.ro's search results page is a Next.js app that renders its
    # product list client-side via a post-load API call, not in the initial
    # HTML or in __NEXT_DATA__. A plain HTTP client (curl_cffi or requests)
    # never receives that JS-fetched listing at all, TLS fingerprint aside.
    # Getting real data would mean either running a JS-executing browser
    # here too (defeating the point of trying curl_cffi as a lighter
    # alternative to Playwright) or reverse-engineering Altex's internal
    # API — out of scope per the one-bounded-attempt rule. Altex stays
    # stubbed; see README for the accepted-gap note.
    print("[altex] skipped (Akamai-protected, out of scope)")
    return []


SCRAPERS = {
    "emag": with_retry(scrape_emag_listing),
    "pcgarage": with_retry(scrape_pcgarage_listing),
    "flanco": with_retry(scrape_flanco_listing),
    "altex": with_retry(scrape_altex_listing),
}


AtlPolicy = Literal["conservative", "off", "aggressive"]


def should_alert(
    prev_price: float | None,
    new_price: float,
    stock_status: str,
    target_price: float | None = None,
    min_drop_percent: float | None = None,
    all_time_low: float | None = None,
    atl_policy: AtlPolicy = "aggressive",
) -> bool:
    # A deal monitor only cares about savings: price increases and
    # unchanged prices never alert, regardless of thresholds.
    if prev_price is None or new_price >= prev_price or stock_status == "out_of_stock":
        return False

    if atl_policy not in ("conservative", "off", "aggressive"):
        raise ValueError(f"Unknown atl_policy: {atl_policy!r}")

    # Below the all-time-low override, a "drop" of a few lei on a
    # three-figure item isn't a deal worth a notification — it's noise from
    # normal price-tracking granularity. Both an absolute (RON) and a
    # relative (%) floor are required since a flat RON cutoff alone would
    # let a 5 RON drop on a 20 RON accessory (25%) alert, while a
    # percent-only cutoff would let a 2% drop on a 2000 RON laptop (40 RON)
    # through — neither is the "false micro-drop" this guards against.
    drop_val = prev_price - new_price
    drop_percent = (drop_val / prev_price) * 100
    passes_micro_drop_floor = drop_val >= 5.0 and drop_percent >= 2.0

    # An all-time low is sourced from the persistent entry["all_time_low"]
    # field (pre-update) rather than min(past_prices), since past_prices is
    # now bounded to a 90-day rolling window and would miss a true record
    # low set further back than that.
    is_new_all_time_low = all_time_low is not None and new_price < all_time_low

    # "aggressive" is the historical default: an all-time low always
    # alerts, bypassing the micro-drop floor, target_price, and
    # min_drop_percent entirely. "conservative" still lets an all-time low
    # override target_price/min_drop_percent, but only once it clears the
    # micro-drop floor — a 1 RON new low on a three-figure item is still
    # noise. "off" removes the override altogether, so an all-time low is
    # judged by the normal gates below like any other drop.
    if is_new_all_time_low and atl_policy == "aggressive":
        return True
    if is_new_all_time_low and atl_policy == "conservative" and passes_micro_drop_floor:
        return True

    if not passes_micro_drop_floor:
        return False
    if target_price is not None and new_price > target_price:
        return False
    if min_drop_percent is not None and drop_percent < min_drop_percent:
        return False
    return True


def update_lifetime_stats(entry: dict, new_price: float, today_str: str) -> dict:
    # Legacy entries (recorded before this field existed) carry no
    # all_time_low/all_time_high/first_seen — derive a starting baseline from
    # their existing history before folding in new_price, so upgrading an old
    # price_history.json doesn't silently reset a product's recorded extremes.
    if (
        "all_time_low" not in entry
        or "all_time_high" not in entry
        or "first_seen" not in entry
    ):
        past_prices = [h["price"] for h in entry.get("history", [])]
        if "all_time_low" not in entry:
            entry["all_time_low"] = min(past_prices) if past_prices else new_price
        if "all_time_high" not in entry:
            entry["all_time_high"] = max(past_prices) if past_prices else new_price
        if "first_seen" not in entry:
            past_dates = [h["date"] for h in entry.get("history", [])]
            entry["first_seen"] = min(past_dates) if past_dates else today_str

    entry["all_time_low"] = min(entry["all_time_low"], new_price)
    entry["all_time_high"] = max(entry["all_time_high"], new_price)
    return entry


def record_observation(
    entry: dict, today: str, observed_at: str, price: float, stock_status: str
) -> None:
    # Appends every scrape as its own observation — no same-day dedup — so a
    # site checked more than once in a day (the 2-hour cron cadence, or a
    # manual re-run) keeps each price point instead of only the first.
    # `date` stays for the existing daily-summary/extrema/chart consumers,
    # which only ever group or sort by calendar date.
    entry["history"].append(
        {
            "date": today,
            "observed_at": observed_at,
            "price": price,
            "stock_status": stock_status,
        }
    )


def prune_history(
    history: list[dict], reference_date: date, max_days: int = 90, min_entries: int = 2
) -> list[dict]:
    kept = [
        h
        for h in history
        if (reference_date - date.fromisoformat(h["date"])).days <= max_days
    ]
    # A product with sparse history (e.g. only checked once every few
    # months) could otherwise be pruned down to 0-1 entries, which breaks
    # prev_price/past_prices comparisons in main(). Falling back to the most
    # recent min_entries keeps those comparisons possible even though it
    # means occasionally keeping an entry older than max_days.
    if len(kept) < min_entries:
        return history[-min_entries:]
    return kept


def _read_health_records() -> list[dict]:
    if not SCRAPE_HEALTH_FILE.exists():
        return []
    records = []
    for line in SCRAPE_HEALTH_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _previous_record(records: list[dict], store: str) -> dict | None:
    # scrape_health.jsonl is append-only, so the last matching line is
    # always that store's most recent run.
    for rec in reversed(records):
        if rec["store"] == store:
            return rec
    return None


def _append_health_records(records: list[dict]) -> None:
    if not records:
        return
    SCRAPE_HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SCRAPE_HEALTH_FILE, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _quarantined_stores(records: list[dict]) -> set[str]:
    # Critical Selector Drift: a store's last two recorded runs both parsed
    # zero products while at least one other store succeeded in one of those
    # same runs — distinguishing a broken selector/site redesign from a
    # network-wide outage, where every store would show zero. Re-derived from
    # scrape_health.jsonl on every call rather than persisted separately, so
    # there is no separate quarantine state machine to keep in sync (T-09
    # decision: quarantine is a query over history, not a stored flag).
    by_store: dict[str, list[dict]] = {}
    for rec in records:
        by_store.setdefault(rec["store"], []).append(rec)

    quarantined = set()
    for store, store_records in by_store.items():
        last_two = store_records[-2:]
        if len(last_two) < 2 or any(r["products_parsed"] != 0 for r in last_two):
            continue
        run_ids = {r["run_id"] for r in last_two}
        other_store_succeeded = any(
            rec["store"] != store
            and rec["run_id"] in run_ids
            and rec["products_parsed"] > 0
            for rec in records
        )
        if other_store_succeeded:
            quarantined.add(store)
    return quarantined


def main():
    try:
        watchlist = load_watchlist(WATCHLIST_FILE)
    except ValueError as e:
        print(f"Watchlist validation failed: {e}", file=sys.stderr)
        sys.exit(1)
    history = load_history()
    db = init_db(DB_FILE)
    alerts = []
    now_utc = datetime.now(UTC)
    today_date = now_utc.date()
    today = today_date.isoformat()
    observed_at = now_utc.isoformat()
    run_id = str(uuid.uuid4())
    run_started_utc = now_utc.isoformat()
    _run_state.reset(run_id)

    health_records_before = _read_health_records()
    quarantined_before = _quarantined_stores(health_records_before)
    per_store: dict[str, dict] = {}

    for item in watchlist:
        site = item["site"]
        stats = per_store.setdefault(
            site,
            {
                "watches_requested": 0,
                "products_parsed": 0,
                "matched_count": 0,
                "policy_blocked_count": 0,
                "latency_seconds": 0.0,
            },
        )
        stats["watches_requested"] += 1

        if site in quarantined_before:
            print(
                f"[{site}] skipped: quarantined after Critical Selector Drift "
                "(zero matches for 2 consecutive runs while other stores succeeded)"
            )
            continue

        scraper = SCRAPERS.get(site)
        if not scraper:
            print(f"Unknown site '{site}' in watchlist, skipping")
            continue

        item_started = time.monotonic()
        try:
            results = scraper(item["query"])
        except Exception as e:
            print(
                f"[{site}] scrape failed after retries ({e.__class__.__name__}: {e}), skipping"
            )
            results = []
        stats["latency_seconds"] += time.monotonic() - item_started
        stats["products_parsed"] += len(results)
        time.sleep(random.uniform(3, 7))  # polite delay between requests

        for r in results:
            # Canonicalized, not the raw scraped URL (T-06): the history dict
            # key and the identity used for offer_id/alerts below must stay
            # stable across a retailer swapping tracking params between runs.
            key = canonicalize_url(r["url"])
            entry = history.setdefault(
                key, {"title": r["title"], "site": item["site"], "history": []}
            )
            entry["title"] = r["title"]
            entry["seller"] = r.get("seller")
            entry["is_marketplace"] = r.get("is_marketplace")
            if "reference_price" in r:
                entry["reference_price"] = r["reference_price"]

            prior_history = list(entry["history"])
            past_prices = [h["price"] for h in prior_history]
            prev_price = past_prices[-1] if past_prices else None
            stock_status = r.get("stock_status", "unknown")
            prior_all_time_low = entry.get("all_time_low")

            update_lifetime_stats(entry, r["price"], today)

            record_observation(entry, today, observed_at, r["price"], stock_status)
            entry["history"] = prune_history(entry["history"], today_date)

            # Structure-only validation (T-01): confirms every real observation
            # this run persists conforms to the Observation domain model, without
            # changing the legacy dict shape that price_history.json stores.
            # offer_id/content_hash are deterministic derivations, not stored
            # fields, since the legacy history format has neither.
            try:
                Observation.model_validate(
                    {
                        "offer_id": uuid.uuid5(uuid.NAMESPACE_URL, key),
                        "price": Decimal(str(r["price"])),
                        "shipping": Decimal("0"),
                        "reference_price": (
                            Decimal(str(r["reference_price"]))
                            if r.get("reference_price") is not None
                            else None
                        ),
                        "in_stock": stock_status != "out_of_stock",
                        "extraction_method": "html",
                        "content_hash": hashlib.sha256(
                            f"{r['title']}|{r['price']}|{stock_status}".encode()
                        ).hexdigest(),
                        "run_id": run_id,
                    }
                )
            except ValidationError as e:
                print(
                    f"[{item['site']}] Observation validation failed for {key}: {e}",
                    file=sys.stderr,
                )
                raise

            # Dual-write (T-37): price_history.json above remains the live
            # source of truth; this additionally persists the same
            # observation into SQLite using the dict shape
            # migrate_history_to_sqlite.py already established, so retailer
            # SKU-derived offer/product ids line up with the historical
            # migration.
            sku = urlparse(key).path.rstrip("/").rsplit("/", 1)[-1]
            sqlite_record_observation(
                db,
                {
                    "sku": sku,
                    "title": r["title"],
                    "price": r["price"],
                    "in_stock": stock_status != "out_of_stock",
                    "retailer": item["site"],
                    "url": key,
                    "scraped_at": observed_at,
                },
            )

            # An out-of-stock listing's price isn't buyable, so a "price
            # change" against it isn't actionable — still recorded above for
            # history/trend purposes, just not surfaced as an alert.
            if should_alert(
                prev_price,
                r["price"],
                stock_status,
                target_price=item.get("target_price"),
                min_drop_percent=item.get("min_drop_percent"),
                all_time_low=prior_all_time_low,
                atl_policy=item.get("atl_policy", "aggressive"),
            ):
                # T-40 (#57): "trusted" means first-party-verified only.
                # eMAG's listing page never reports a seller (robots.txt
                # blocks the only page that would), so is_marketplace is
                # always None there — a "trusted" watch correctly never
                # alerts on eMAG until that becomes resolvable some other
                # way, rather than guessing.
                if (
                    item.get("seller_policy") == "trusted"
                    and r.get("is_marketplace") is not False
                ):
                    stats["policy_blocked_count"] += 1
                    continue
                stats["matched_count"] += 1
                thirty_day_cutoff = today_date - timedelta(days=30)
                recent_prices = [
                    h["price"]
                    for h in prior_history
                    if date.fromisoformat(h["date"]) >= thirty_day_cutoff
                ]
                oldest_date = (
                    date.fromisoformat(prior_history[0]["date"])
                    if prior_history
                    else today_date
                )
                alerts.append(
                    {
                        "title": r["title"],
                        "site": item["site"],
                        "query": item["query"],
                        "url": key,
                        "old_price": prev_price,
                        "new_price": r["price"],
                        "thirty_day_low": min(recent_prices)
                        if recent_prices
                        else prev_price,
                        "history_days": (today_date - oldest_date).days,
                        "reference_price": r.get("reference_price"),
                        "stock_status": stock_status,
                        "seller": r.get("seller"),
                        "is_marketplace": r.get("is_marketplace"),
                        "all_time_low": entry["all_time_low"],
                        "all_time_high": entry["all_time_high"],
                    }
                )

    new_health_records = []
    for site, stats in per_store.items():
        if site in quarantined_before or site not in SCRAPERS:
            continue
        prev = _previous_record(health_records_before, site)
        last_known_good_utc = (
            run_started_utc
            if stats["products_parsed"] > 0
            else (prev["last_known_good_utc"] if prev else None)
        )
        new_health_records.append(
            {
                "store": site,
                "run_id": run_id,
                "run_started_utc": run_started_utc,
                "watches_requested": stats["watches_requested"],
                "products_parsed": stats["products_parsed"],
                "matched_count": stats["matched_count"],
                "policy_blocked_count": stats["policy_blocked_count"],
                "parse_failures": _run_state.failure_counts.get(site, 0),
                "challenge_detected": _run_state.challenge_counts.get(site, 0) > 0,
                "latency_seconds": round(stats["latency_seconds"], 3),
                "last_known_good_utc": last_known_good_utc,
            }
        )
    _append_health_records(new_health_records)

    quarantined_after = _quarantined_stores(health_records_before + new_health_records)
    newly_quarantined = sorted(quarantined_after - quarantined_before)

    health_alerts: list[str] = []
    for store in newly_quarantined:
        display = STORE_DISPLAY_NAMES.get(store, store.title())
        health_alerts.append(
            f"🚨 SCRAPER BREAKDOWN: {display} returned 0 items across all watchlist "
            "queries! Possible website redesign or bot-wall."
        )

    stale_stores = [
        rec["store"]
        for rec in new_health_records
        if rec["last_known_good_utc"] is not None
        and (
            now_utc - datetime.fromisoformat(rec["last_known_good_utc"])
        ).total_seconds()
        > DEAD_MAN_THRESHOLD_HOURS * 3600
    ]
    if stale_stores:
        stale_display = ", ".join(
            STORE_DISPLAY_NAMES.get(s, s.title()) for s in stale_stores
        )
        print(
            f"WARNING: dead-man check — no successful scrape for {stale_display} "
            f"in over {DEAD_MAN_THRESHOLD_HOURS}h",
            file=sys.stderr,
        )
        health_alerts.append(
            f"⚠️ DEAD-MAN CHECK: no successful scrape for {stale_display} in over "
            f"{DEAD_MAN_THRESHOLD_HOURS}h. Site may be down or fully blocked."
        )

    SCRAPE_HEALTH_ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    json.dump(
        health_alerts,
        open(SCRAPE_HEALTH_ALERTS_FILE, "w", encoding="utf-8"),
        indent=2,
        ensure_ascii=False,
    )

    save_history(history)
    db.close()
    json.dump(
        alerts, open(ALERTS_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False
    )
    print(
        f"Checked {len(watchlist)} watchlist entries, {len(alerts)} price change(s) detected"
    )


if __name__ == "__main__":
    main()
