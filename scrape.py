# scripts/scrape.py
import requests
from bs4 import BeautifulSoup
import json, time, random
from pathlib import Path

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
WATCHLIST = json.load(open("data/watchlist.json"))
HISTORY_FILE = Path("data/price_history.json")


def scrape_emag_listing(query):
    url = f"https://www.emag.ro/search/{query.replace(' ', '+')}"  # listing page, not /product/
    r = requests.get(url, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for card in soup.select(".card-item"):
        title_el = card.select_one(".card-v2-title")
        price_el = card.select_one(".product-new-price")
        if title_el and price_el:
            results.append(
                {
                    "title": title_el.get_text(strip=True),
                    "price": price_el.get_text(strip=True),
                    "url": card.select_one("a")["href"],
                }
            )
    return results


def main():
    history = json.load(open(HISTORY_FILE)) if HISTORY_FILE.exists() else {}
    alerts = []
    for item in WATCHLIST:
        results = scrape_emag_listing(item["query"])
        time.sleep(random.uniform(3, 7))  # polite delay between requests
        for r in results:
            key = r["url"]
            prev_price = history.get(key, {}).get("price")
            history[key] = {"title": r["title"], "price": r["price"], "url": r["url"]}
            if prev_price and prev_price != r["price"]:
                alerts.append(
                    {
                        "title": r["title"],
                        "old": prev_price,
                        "new": r["price"],
                        "url": r["url"],
                    }
                )
    json.dump(history, open(HISTORY_FILE, "w"), indent=2, ensure_ascii=False)
    json.dump(alerts, open("data/alerts.json", "w"), indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
