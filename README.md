# bf-price-monitor

Zero-cost Black Friday price monitor for eMAG, Flanco, and PC Garage.
Scrapes listing pages on a schedule, tracks price history in the repo, uses
an LLM to judge whether a price drop is a genuine discount or a fake
pre-hike, and sends alerts to Telegram. Runs entirely on GitHub Actions'
free tier — no paid services required.

## Site status

| Site | Status | Method |
|---|---|---|
| eMAG | Live | plain `requests`; listing page has no seller data, so `seller`/`is_marketplace` are always `null` |
| PC Garage | Live | Playwright + playwright-stealth (Cloudflare JS challenge blocks plain `requests`); first-party only |
| Flanco | Live | Playwright + playwright-stealth, including the legally-mandated 30-day reference price; first-party only |
| Altex | Not working, excluded on purpose | — |

Altex is fronted by Akamai and stalls the TLS handshake for a plain
`requests` client. `curl_cffi` with `impersonate="chrome120"` (TLS
fingerprint impersonation) was tested on 2026-09-07 and does clear that
stall — but Altex's search results page is a client-rendered Next.js app:
the product listing is fetched by JavaScript after the page loads, not
present in the initial HTML or embedded page data. Any plain HTTP client,
regardless of its TLS fingerprint, only ever sees the empty page shell.
Reaching the real data would require either a JS-executing browser (which
defeats the point of using the lighter `curl_cffi` approach instead of
Playwright) or reverse-engineering Altex's internal API — both out of scope
for a $0-cost, low-maintenance project. Altex is intentionally left stubbed
in `scripts/scrape.py` (`scrape_altex_listing` logs and returns `[]`) rather
than crashing the run.

## How it works

1. `scripts/scrape.py` fetches search/listing pages for each watchlist entry
   (never direct product pages), records today's price into
   `data/price_history.json`, and writes any price changes to
   `data/alerts.json`. Each recorded product also carries:
   - `stock_status`: `in_stock`, `limited_stock`, `supplier_stock`,
     `out_of_stock`, or `unknown` — granularity varies by site depending on
     what its listing page actually exposes (see "Site status" below).
     Alerts are suppressed (but the price is still recorded to history) when
     `stock_status` is `out_of_stock`, since a sold-out price isn't
     actionable.
   - `seller` / `is_marketplace`: which company fulfills the listing, and
     whether that's a third-party marketplace seller rather than the
     retailer itself. Only populated for PC Garage and Flanco, which sell
     first-party only (`is_marketplace: false`); `null` for eMAG, whose
     listing pages carry no seller information at all (see "Site status").
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
needs a `site` (`emag`, `pcgarage`, or `flanco` — `altex` entries are
accepted but will never produce results, see "Site status" above) and a
`query` (the search term to use on that site's search page).

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

## Self-hosted runner environment (do not "clean up" these settings)

`.github/workflows/monitor.yml` runs on a Windows machine registered as a
self-hosted runner, not GitHub's shared runners — this was a deliberate
choice, because GitHub's shared IP range gets blocked by these sites' bot
detection. The runner service itself runs as `NT AUTHORITY\NETWORK SERVICE`,
a Windows account with its own isolated profile, separate from the
interactive user account the machine is normally used under. Two settings
in the workflow exist specifically because of that isolation, and both look
removable to someone unfamiliar with the setup — they are not:

- **`PYTHON` is a pinned absolute path**, not a bare `python` PATH lookup.
  `NETWORK SERVICE` can't see the interactive user's Store-app Python
  install, so the workflow calls `C:\Program Files\Python312\python.exe`
  directly.
- **`PLAYWRIGHT_BROWSERS_PATH` is pinned to a fixed path** under
  `C:\actions-runner\`, instead of relying on Playwright's default
  per-account browser cache location — `NETWORK SERVICE` and the
  interactive user would otherwise each get their own separate (and, for
  the service account, non-existent) cache.

The commit-back step also runs as native PowerShell rather than
`shell: bash`, because `shell: bash` resolves to an uninstalled WSL stub on
this machine.
