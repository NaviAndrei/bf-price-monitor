# bf-price-monitor

Zero-cost Black Friday price monitor for eMAG, Altex, Flanco, and PC Garage.
Scrapes listing pages on a schedule, tracks price history in the repo, uses
an LLM to judge whether a price drop is a genuine discount or a fake
pre-hike, and sends alerts to Telegram. Runs entirely on GitHub Actions'
free tier — no paid services required.

## How it works

1. `scripts/scrape.py` fetches search/listing pages for each watchlist entry
   (never direct product pages), records today's price into
   `data/price_history.json`, and writes any price changes to
   `data/alerts.json`.
2. `scripts/analyze.py` asks an LLM whether each price change looks like a
   genuine discount, using the Hugging Face Inference API first and falling
   back to a local Ollama model if that fails. Flanco listings carry a
   legally-mandated 30-day reference price (OUG 27/2022); other sites fall
   back to a 30-day low computed from scrape history. Results are grouped
   per watchlist entry into `data/formatted_alerts.json`.
3. `scripts/notify.py` sends each formatted message to Telegram, respecting
   Telegram's ~1 message/second rate limit and its `retry_after` value on a
   429 response.
4. `.github/workflows/monitor.yml` runs all three scripts every 2 hours (and
   on manual trigger), then commits the updated `data/price_history.json`
   and `data/watchlist.json` back to the repo.

## Setup

### 1. Secrets

In the GitHub repo, go to **Settings → Secrets and variables → Actions** and
add:

| Secret | Where to get it |
|---|---|
| `HF_TOKEN` | Hugging Face → Settings → Access Tokens (a free "read" token is enough for the Serverless Inference API) |
| `TELEGRAM_BOT_TOKEN` | Message [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`, copy the token it gives you |
| `TELEGRAM_CHAT_ID` | Message your new bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.chat.id` from the response |

No secrets are needed for the local Ollama fallback since it never leaves
your machine — it's only used when the HF call fails during a manual local
run.

### 2. Watchlist

Edit `data/watchlist.json` to add or remove products to track. Each entry
needs a `site` (`emag`, `altex`, `flanco`, or `pcgarage` — note `altex` has
no working scraper yet) and a `query` (the search term to use on that
site's search page).

### 3. Local testing (Windows/WSL)

```bash
pip install -r requirements.txt
python scripts/scrape.py      # writes data/alerts.json if any prices changed
python scripts/analyze.py     # HF_TOKEN must be set in the environment, or leave it unset to force the Ollama fallback
python scripts/notify.py      # TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set
```

For the Ollama fallback to work locally, install [Ollama](https://ollama.com)
and pull the model referenced by `OLLAMA_MODEL` in `scripts/analyze.py`
(currently `qwen3:8b`).

On Windows, run these directly in PowerShell or inside WSL — no path
differences beyond the usual `python` vs `python3` naming.

## Notes

- Scraping only ever hits listing/search pages, respecting each site's
  `robots.txt`, with a randomized 3–7 second delay between sites.
- Price history is capped at the last 30 daily entries per product.
- `data/alerts.json` and `data/formatted_alerts.json` are regenerated every
  run and are gitignored; `data/price_history.json` and
  `data/watchlist.json` are the persisted state committed by the workflow.
