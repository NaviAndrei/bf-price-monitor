# scripts/scrape.py
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
HISTORY_LIMIT = (
    30  # keep last 30 daily price points per product, for the 30-day-low check
)

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
                response = page.goto(
                    url, timeout=REQUEST_TIMEOUT * 1000, wait_until="domcontentloaded"
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
    # data-availability-id values for matched cards: "3" ("în stoc") and "2"
    # ("ultimul produs in stoc" - last unit). No out-of-stock marker text
    # ("stoc epuizat", "indisponibil") appeared anywhere on either page, so
    # eMAG appears to exclude sold-out offers from search results entirely.
    # The text check below is kept as a defensive fallback in case that
    # changes; "unknown" covers any card whose markup doesn't match either.
    text = card.get_text(" ", strip=True).lower()
    if "stoc epuizat" in text or "indisponibil" in text:
        return "out_of_stock"
    if card.get("data-availability-id") in ("2", "3") or "in stoc" in text:
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
            }
        )
    return results


def pcgarage_stock_status(card) -> str:
    # PC Garage marks every listed card with a `.product_box_availability`
    # div whose second class is the actual state (verified live 2026-09-07
    # against "placa video rtx 3050" and a discontinued-laptop query, which
    # surfaced all three real values): "instock" ("Stoc magazin
    # suficient/limitat"), "insupplierstock" ("In stoc furnizor" - still
    # orderable, fulfilled by the supplier rather than PC Garage's own
    # warehouse), and "outofstock" ("Nu este in stoc").
    el = card.select_one(".product_box_availability")
    if not el:
        return "unknown"
    classes = el.get("class") or []
    if "outofstock" in classes:
        return "out_of_stock"
    if "instock" in classes or "insupplierstock" in classes:
        return "in_stock"
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
            }
        )
    return results


def flanco_stock_status(card) -> str:
    # Flanco marks every real listed card with a `.stocky-txt` span inside
    # `.produs-status .stock` whose class is the actual state (verified live
    # 2026-09-07 against "laptop asus vivobook" and "iphone 13 mini", 20+25
    # real cards checked — every one of them had this marker present):
    # "in-stock" ("In stoc"), "limited-stock" ("Stoc limitat"),
    # "supplier-stock" ("Exclusiv online"), "bin-display" ("Expus in
    # magazin") — all four are purchasable states, just different fulfilment
    # channels. No out-of-stock class was observed on either query, so
    # Flanco appears to exclude sold-out products from search results
    # entirely; the "out-of-stock"/"sold-out" check below is a defensive
    # fallback in case that changes.
    el = card.select_one(".stocky-txt")
    if not el:
        return "unknown"
    classes = el.get("class") or []
    if any("out-of-stock" in c or "sold-out" in c for c in classes):
        return "out_of_stock"
    if any(
        c in classes
        for c in ("in-stock", "limited-stock", "supplier-stock", "bin-display")
    ):
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
    url = f"https://www.altex.ro/search/?q={query.replace(' ', '+')}"
    html = fetch(url, "altex")
    if not html:
        return []
    print(
        "[altex] page fetched successfully but no selectors are implemented yet, skipping"
    )
    return []


SCRAPERS = {
    "emag": scrape_emag_listing,
    "pcgarage": scrape_pcgarage_listing,
    "flanco": scrape_flanco_listing,
    "altex": scrape_altex_listing,
}


def main():
    watchlist = json.load(open(WATCHLIST_FILE, encoding="utf-8"))
    history = load_history()
    alerts = []
    today = date.today().isoformat()

    for item in watchlist:
        scraper = SCRAPERS.get(item["site"])
        if not scraper:
            print(f"Unknown site '{item['site']}' in watchlist, skipping")
            continue

        results = scraper(item["query"])
        time.sleep(random.uniform(3, 7))  # polite delay between requests

        for r in results:
            key = r["url"]
            entry = history.setdefault(
                key, {"title": r["title"], "site": item["site"], "history": []}
            )
            entry["title"] = r["title"]
            if "reference_price" in r:
                entry["reference_price"] = r["reference_price"]

            if entry["history"] and entry["history"][-1]["date"] == today:
                continue  # already recorded today (e.g. two queries matched the same product)

            past_prices = [h["price"] for h in entry["history"]]
            prev_price = past_prices[-1] if past_prices else None
            stock_status = r.get("stock_status", "unknown")

            entry["history"].append(
                {"date": today, "price": r["price"], "stock_status": stock_status}
            )
            entry["history"] = entry["history"][-HISTORY_LIMIT:]

            if prev_price is not None and prev_price != r["price"]:
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
