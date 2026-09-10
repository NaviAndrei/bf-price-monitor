# scripts/scrape.py
import functools
import json
import random
import re
import time
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

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
ALERTS_FILE = Path("data/alerts.json")
HISTORY_SCHEMA_VERSION = 1


def load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {}
    data = json.load(open(HISTORY_FILE, encoding="utf-8"))
    if "schema_version" not in data:
        # Pre-versioning file: bare {url: entry} dict. Wrap it so this run's
        # save writes the versioned format without losing any prior history.
        data = {"schema_version": HISTORY_SCHEMA_VERSION, "products": data}
    return data["products"]


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
                    url, timeout=PLAYWRIGHT_NAV_TIMEOUT_MS, wait_until="domcontentloaded"
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
        return None
    return html


def fetch(url: str, site_name: str) -> str | None:
    r = None
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            print(f"[{site_name}] unreachable ({e.__class__.__name__}), skipping this run")
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
    results = []
    for card in soup.select(".card-item"):
        title_el = card.select_one(".card-v2-title")
        price_el = card.select_one(".product-new-price")
        if not (title_el and price_el and title_el.get("href")):
            continue
        price = parse_price(price_el.get_text(strip=True))
        if price is None:
            continue
        results.append(
            {
                "title": title_el.get_text(strip=True),
                "price": price,
                "url": title_el["href"],
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
    results = []
    for card in soup.select(".product_box"):
        title_el = card.select_one(".product_box_name a")
        price_el = card.select_one(".product_box_price_container p.price")
        if not (title_el and price_el and title_el.get("href")):
            continue
        price = parse_price(price_el.get_text(strip=True))
        if price is None:
            continue
        results.append(
            {
                "title": title_el.get_text(strip=True),
                "price": price,
                "url": title_el["href"],
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
    results = []
    for card in soup.select("li.product-item"):
        title_el = card.select_one(".product-item-link")
        price_el = card.select_one(".price-box.price-final_price .special-price .price")
        # OUG 27/2022 requires retailers to publish the lowest price from the
        # last 30 days when a product is discounted — Flanco surfaces this
        # legally-audited figure directly in the markup, so we capture it
        # instead of relying only on our own scrape history.
        reference_el = card.select_one(".pretVechi .pricePrp .price")
        if not (title_el and price_el and title_el.get("href")):
            continue
        price = parse_price(price_el.get_text(strip=True))
        if price is None:
            continue
        result = {
            "title": title_el.get_text(strip=True),
            "price": price,
            "url": title_el["href"],
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


def should_alert(
    prev_price: float | None,
    new_price: float,
    past_prices: list[float],
    stock_status: str,
    target_price: float | None = None,
    min_drop_percent: float | None = None,
) -> bool:
    # A deal monitor only cares about savings: price increases and
    # unchanged prices never alert, regardless of thresholds.
    if prev_price is None or new_price >= prev_price or stock_status == "out_of_stock":
        return False

    # An all-time low is always worth surfacing, even if it's a smaller drop
    # than min_drop_percent or hasn't reached target_price yet.
    if not past_prices or new_price < min(past_prices):
        return True

    if target_price is not None and new_price > target_price:
        return False
    if min_drop_percent is not None:
        drop_percent = ((prev_price - new_price) / prev_price) * 100
        if drop_percent < min_drop_percent:
            return False
    return True


def update_lifetime_stats(entry: dict, new_price: float, today_str: str) -> dict:
    # Legacy entries (recorded before this field existed) carry no
    # all_time_low/all_time_high/first_seen — derive a starting baseline from
    # their existing history before folding in new_price, so upgrading an old
    # price_history.json doesn't silently reset a product's recorded extremes.
    if "all_time_low" not in entry or "all_time_high" not in entry or "first_seen" not in entry:
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


def main():
    watchlist = json.load(open(WATCHLIST_FILE, encoding="utf-8"))
    history = load_history()
    alerts = []
    today_date = date.today()
    today = today_date.isoformat()

    for item in watchlist:
        scraper = SCRAPERS.get(item["site"])
        if not scraper:
            print(f"Unknown site '{item['site']}' in watchlist, skipping")
            continue

        try:
            results = scraper(item["query"])
        except Exception as e:
            print(
                f"[{item['site']}] scrape failed after retries ({e.__class__.__name__}: {e}), skipping"
            )
            results = []
        time.sleep(random.uniform(3, 7))  # polite delay between requests

        for r in results:
            key = r["url"]
            entry = history.setdefault(
                key, {"title": r["title"], "site": item["site"], "history": []}
            )
            entry["title"] = r["title"]
            entry["seller"] = r.get("seller")
            entry["is_marketplace"] = r.get("is_marketplace")
            if "reference_price" in r:
                entry["reference_price"] = r["reference_price"]

            if entry["history"] and entry["history"][-1]["date"] == today:
                continue  # already recorded today (e.g. two queries matched the same product)

            past_prices = [h["price"] for h in entry["history"]]
            prev_price = past_prices[-1] if past_prices else None
            stock_status = r.get("stock_status", "unknown")

            update_lifetime_stats(entry, r["price"], today)

            entry["history"].append(
                {"date": today, "price": r["price"], "stock_status": stock_status}
            )
            entry["history"] = prune_history(entry["history"], today_date)

            # An out-of-stock listing's price isn't buyable, so a "price
            # change" against it isn't actionable — still recorded above for
            # history/trend purposes, just not surfaced as an alert.
            if (
                prev_price is not None
                and prev_price != r["price"]
                and stock_status != "out_of_stock"
            ):
                alerts.append(
                    {
                        "title": r["title"],
                        "site": item["site"],
                        "query": item["query"],
                        "url": key,
                        "old_price": prev_price,
                        "new_price": r["price"],
                        "thirty_day_low": min(past_prices) if past_prices else None,
                        "reference_price": r.get("reference_price"),
                        "stock_status": stock_status,
                        "seller": r.get("seller"),
                        "is_marketplace": r.get("is_marketplace"),
                        "all_time_low": entry["all_time_low"],
                        "all_time_high": entry["all_time_high"],
                    }
                )

    save_history(history)
    json.dump(
        alerts, open(ALERTS_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False
    )
    print(
        f"Checked {len(watchlist)} watchlist entries, {len(alerts)} price change(s) detected"
    )


if __name__ == "__main__":
    main()
