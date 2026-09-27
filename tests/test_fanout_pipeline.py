"""T-40 follow-up (#59): one fan-out watch producing offers on several
retailers in the same run, driven end to end through scrape.py, analyze.py
and notify.py with fake scrapers and a fake Telegram endpoint. Every file
path is redirected to tmp_path; nothing here touches data/ or the network.
"""

import json
import uuid

import analyze
import notify
import scrape

QUERY = "laptop lenovo v15"
URLS = {
    "emag": "https://www.emag.ro/laptop-lenovo-v15/pd/EMAG1/",
    "pcgarage": "https://www.pcgarage.ro/laptop-lenovo-v15/",
    "flanco": "https://www.flanco.ro/laptop-lenovo-v15.html",
}


def _scraper_for(site):
    def _scraper(query):
        return [
            {
                "title": "Laptop Lenovo V15",
                "price": 2499.99,
                "url": URLS[site],
                "stock_status": "in_stock",
                "seller": None if site == "emag" else site,
                "is_marketplace": None if site == "emag" else False,
            }
        ]

    return _scraper


def _seed_history(history_file):
    products = {
        scrape.canonicalize_url(url): {
            "title": "Laptop Lenovo V15",
            "site": site,
            "all_time_low": 2999.99,
            "all_time_high": 2999.99,
            "first_seen": "2026-01-01",
            "history": [
                {
                    "date": "2026-01-01",
                    "observed_at": "2026-01-01T09:00:00+00:00",
                    "price": 2999.99,
                    "stock_status": "in_stock",
                }
            ],
        }
        for site, url in URLS.items()
    }
    history_file.write_text(
        json.dumps({"schema_version": 1, "products": products}), encoding="utf-8"
    )


def test_one_watch_three_retailers_end_to_end(tmp_path, monkeypatch):
    watchlist_file = tmp_path / "watchlist.json"
    history_file = tmp_path / "price_history.json"
    alerts_file = tmp_path / "alerts.json"
    formatted_file = tmp_path / "formatted_alerts.json"
    health_alerts_file = tmp_path / "scrape_health_alerts.json"
    watchlist_file.write_text(
        json.dumps(
            {
                "watches": [
                    {
                        "id": "babebbc9-2a56-5d0c-b57c-9ca8ad44d7b4",
                        "site": scrape.FANOUT_SITE,
                        "query": QUERY,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _seed_history(history_file)

    # scrape.py
    monkeypatch.setattr(scrape, "WATCHLIST_FILE", watchlist_file)
    monkeypatch.setattr(scrape, "HISTORY_FILE", history_file)
    monkeypatch.setattr(scrape, "ALERTS_FILE", alerts_file)
    monkeypatch.setattr(scrape, "DB_FILE", tmp_path / "price_history.db")
    monkeypatch.setattr(scrape, "SCRAPE_HEALTH_FILE", tmp_path / "scrape_health.jsonl")
    monkeypatch.setattr(scrape, "SCRAPE_HEALTH_ALERTS_FILE", health_alerts_file)
    monkeypatch.setattr(
        scrape, "EXTRACTION_FAILURES_FILE", tmp_path / "extraction_failures.jsonl"
    )
    monkeypatch.setattr(scrape, "SCRAPERS", {site: _scraper_for(site) for site in URLS})
    monkeypatch.setattr(scrape.time, "sleep", lambda *_: None)

    # analyze.py: the deterministic rule engine runs for real; only the LLM
    # explanation is replaced by the offline default.
    monkeypatch.setattr(analyze, "ALERTS_FILE", alerts_file)
    monkeypatch.setattr(analyze, "FORMATTED_FILE", formatted_file)
    monkeypatch.setattr(analyze, "AI_AUDIT_FILE", tmp_path / "ai_audit.jsonl")
    monkeypatch.setattr(analyze, "SCRAPE_HEALTH_ALERTS_FILE", health_alerts_file)
    monkeypatch.setattr(
        analyze,
        "get_analysis",
        lambda client, prompt, rule_verdict, thirty_day_low, **kw: (
            analyze.default_analysis(rule_verdict, thirty_day_low, **kw)
        ),
    )

    # notify.py
    monkeypatch.setattr(notify, "FORMATTED_FILE", formatted_file)
    monkeypatch.setattr(notify, "PRICE_HISTORY_FILE", history_file)
    monkeypatch.setattr(notify, "WATCHLIST_FILE", watchlist_file)
    monkeypatch.setattr(notify, "SCRAPE_HEALTH_ALERTS_FILE", health_alerts_file)
    monkeypatch.setattr(notify, "DB_FILE", tmp_path / "notify.db")
    monkeypatch.setattr(notify, "OUTBOX_FILE", tmp_path / "alert_outbox.jsonl")
    monkeypatch.setattr(notify, "DLQ_FILE", tmp_path / "dlq.jsonl")
    monkeypatch.setattr(notify.time, "sleep", lambda *_: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "testtoken")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    for name in ("NTFY_TOPIC", "SMTP_HOST", "EMAIL_TO"):
        monkeypatch.delenv(name, raising=False)

    sent_texts = []

    class _Ok:
        status_code = 200
        headers: dict = {}
        text = ""

        def json(self):
            return {}

        def raise_for_status(self):
            return None

    def _post(url, json=None, timeout=None):
        assert url.endswith("/sendMessage"), url
        sent_texts.append(json["text"])
        return _Ok()

    monkeypatch.setattr(notify.requests, "post", _post)

    scrape.main()
    analyze.main()
    notify.main()

    formatted = json.loads(formatted_file.read_text(encoding="utf-8"))
    assert sorted(a["site"] for a in formatted) == sorted(URLS)
    assert all(a["watch_site"] == scrape.FANOUT_SITE for a in formatted)

    # One delivered message per retailer offer, none collapsed or duplicated.
    assert len(sent_texts) == 3
    for site in URLS:
        assert sum(site.upper() in text for text in sent_texts) == 1

    outbox = [
        json.loads(line)
        for line in notify.OUTBOX_FILE.read_text(encoding="utf-8").splitlines()
    ]
    sent = [r for r in outbox if r["status"] == "SENT"]
    assert sorted(r["site"] for r in sent) == sorted(URLS)
    assert len({r["event_id"] for r in sent}) == 3
    assert {r["alert_payload"]["watch_id"] for r in sent} == {
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scrape.FANOUT_SITE}:{QUERY}"))
    }
