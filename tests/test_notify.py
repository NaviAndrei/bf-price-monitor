import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from notify import (  # noqa: E402
    build_inline_keyboard,
    format_telegram_message,
    generate_quickchart_url,
)

BASE_ALERT = {
    "title": "Laptop Lenovo V15 G4 AMN AMD Ryzen 5 7520U 16GB 512GB SSD",
    "site": "emag",
    "url": "https://www.emag.ro/some-product/pd/ABC123/",
    "seller": "eMAG",
    "stock_status": "in_stock",
    "old_price": 100.0,
    "new_price": 80.0,
    "thirty_day_low": 90.0,
    "all_time_low": 75.0,
    "discount_vs_old_pct": 20.0,
    "discount_vs_30d_pct": 11.11,
    "verdict": "GENUINE_DEAL",
    "verdict_score": 9,
    "summary": "Merită cumpărat <acum>.",
    "is_recommended": True,
}


def test_format_telegram_message_renders_html_without_escaping_syntax_errors():
    message = format_telegram_message(BASE_ALERT)
    assert "<b>OFERTĂ REALĂ</b>" in message
    assert "<b>Laptop Lenovo" in message
    # The summary's raw "<acum>" must be escaped, not left as a stray tag.
    assert "&lt;acum&gt;" in message
    assert "<acum>" not in message


def test_format_telegram_message_handles_missing_seller_gracefully():
    alert = {**BASE_ALERT, "seller": None}
    message = format_telegram_message(alert)
    assert "Vânzător: Neverificat" in message


def test_format_telegram_message_all_verdict_badges():
    expected = {
        "GENUINE_DEAL": "OFERTĂ REALĂ",
        "FALSE_DISCOUNT": "REDUCERE FALSĂ",
        "INFLATED_REFERENCE": "PREȚ DE REFERINȚĂ UMFLAT",
        "NORMAL_DROP": "SCĂDERE DE PREȚ",
        "INSUFFICIENT_HISTORY": "ISTORIC NOU / INSUFICIENT",
    }
    for verdict, label in expected.items():
        alert = {**BASE_ALERT, "verdict": verdict}
        message = format_telegram_message(alert)
        assert f"<b>{label}</b>" in message


def test_build_inline_keyboard_produces_valid_urls():
    keyboard = build_inline_keyboard(BASE_ALERT)
    buttons = keyboard["inline_keyboard"][0]
    assert buttons[0]["url"] == BASE_ALERT["url"]
    assert buttons[0]["text"].startswith("\U0001F6D2")
    assert "EMAG" in buttons[0]["text"]

    compari_url = buttons[1]["url"]
    assert compari_url.startswith("https://www.compari.ro/CategorySearch.php?st=")
    assert "Laptop+Lenovo+V15+G4" in compari_url
    assert "AMN" not in compari_url


def test_generate_quickchart_url_returns_none_below_three_points():
    history = [
        {"date": "2026-09-01", "price": 100.0},
        {"date": "2026-09-02", "price": 95.0},
    ]
    assert generate_quickchart_url(history, "Some Product") is None


def test_generate_quickchart_url_returns_valid_url_for_three_or_more_points():
    history = [
        {"date": "2026-09-01", "price": 100.0},
        {"date": "2026-09-02", "price": 95.0},
        {"date": "2026-09-03", "price": 90.0},
    ]
    url = generate_quickchart_url(history, "Some Product", verdict="GENUINE_DEAL")
    assert url.startswith("https://quickchart.io/chart?")
    assert "c=" in url
