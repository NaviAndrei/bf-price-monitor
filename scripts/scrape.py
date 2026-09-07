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

WATCHLIST_FILE = Path("data/watchlist.json")
HISTORY_FILE = Path("data/price_history.json")
ALERTS_FILE = Path("data/alerts.json")


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

    try:
        with Stealth().use_sync(sync_playwright()) as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(
                url, timeout=REQUEST_TIMEOUT * 1000, wait_until="domcontentloaded"
            )

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

    if is_challenge_page(html):
        print(
            f"[{site_name}] still blocked by bot-challenge after Playwright wait, skipping this run"
        )
        return None
    return html


def fetch(url: str, site_name: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as e:
        print(f"[{site_name}] unreachable ({e.__class__.__name__}), skipping this run")
        return None
    if r.status_code in (403, 503) or is_challenge_page(r.text):
        print(
            f"[{site_name}] hit a bot-challenge page (status {r.status_code}), skipping this run"
        )
        return None
    if r.status_code != 200:
        print(f"[{site_name}] unexpected status {r.status_code}, skipping this run")
        return None
    return r.text


def scrape_emag_listing(query: str) -> list[dict]:
    # Listing page only: /search/<query> redirects to a /<category>/c page.
    # robots.txt disallows /product/ — never requested here.
    # Card HTML (verified 2026-09-06):
    #   <div class="card-item card-standard ...">
    #     <a class="card-v2-title ..." href="https://www.emag.ro/.../pd/...">Title</a>
    #     <p class="product-new-price">3&#46;999<sup><small class="mf-decimal">&#44;</small>99</sup> <span>Lei</span></p>
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
            }
        )
    return results


def scrape_pcgarage_listing(query: str) -> list[dict]:
    # Listing page only: /cauta/?q=<query> is PC Garage's search-results page.
    # robots.txt disallows /detalii-produs/ (product pages) — never requested here.
    # Card HTML (verified 2026-09-06):
    #   <div class="product_box">
    #     <div class="product_box_name"><h2><a href="https://www.pcgarage.ro/...">Title</a></h2></div>
    #     <div class="product_box_price_container"><div class="pb-price"><p class="price">1.798,99 RON</p></div></div>
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
            }
        )
    return results


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
    history = (
        json.load(open(HISTORY_FILE, encoding="utf-8")) if HISTORY_FILE.exists() else {}
    )
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

            entry["history"].append({"date": today, "price": r["price"]})
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
                    }
                )

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    json.dump(
        history, open(HISTORY_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False
    )
    json.dump(
        alerts, open(ALERTS_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False
    )
    print(
        f"Checked {len(watchlist)} watchlist entries, {len(alerts)} price change(s) detected"
    )


if __name__ == "__main__":
    main()
