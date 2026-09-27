# scripts/report.py
"""T-27 (#34): static, self-contained HTML report for one monitor run.

Renders watches with per-product inline SVG price charts (30-day reference
line included), store health (T-09), and this run's alerts with their
reasons into a single index.html: inline CSS and SVG only, no <script>, no
external CDN, font or image -- it opens offline in any browser.

PRIVACY: the report contains the owner's watchlist and price data. It is
published ONLY as a short-lived workflow artifact (monitor.yml), never via
GitHub Pages. Note that this repository is public and artifact downloads
require only repository read access, so "artifact-only" means not indexed,
not served at a stable public URL, and expiring -- not access-controlled.

Data sources (all read-only): data/price_history.json (the live source of
truth scrape.py derives thirty_day_low and history_30d from),
data/watchlist.json, data/scrape_health.jsonl, data/scrape_health_alerts.json
and data/formatted_alerts.json. data/price_history.db is deliberately NOT
read: it is git-ignored, so on the self-hosted runner actions/checkout's
clean step wipes it every run, and it would never hold more than one run.

The 30-day reference wording is never restated here: alert cards embed
notify._thirty_day_low_line() verbatim, and per-product rows build the same
alert-shaped dict scrape.py builds and run it through analyze's
build_deal_stats()/thirty_day_window_provenance() before calling that same
notify function -- so the report cannot contradict what an alert said.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import analyze
import notify
import scrape

from bf_price_monitor.config import load_watchlist

DEFAULT_DATA_DIR = Path("data")
DEFAULT_OUTPUT = Path("report/index.html")

# Blueprint deal-badge taxonomy (static CSS badges; no client-side filtering).
BADGE_ATL = "GENUINE ALL-TIME LOW"
BADGE_OMNIBUS = "OMNIBUS VERIFIED"
BADGE_PRE_HIKE = "PRE-HIKE DETECTED"
BADGE_LIMITED = "LIMITED STOCK"

SPARK_WIDTH = 260
SPARK_HEIGHT = 56
SPARK_PAD = 4

_CSS = """
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;
padding:1.5rem;background:#f6f7f9;color:#1d2330;line-height:1.4}
h1{margin:0 0 .25rem}h2{margin-top:2rem;border-bottom:2px solid #d5d9e0}
.muted{color:#5b6475;font-size:.9rem}
table{border-collapse:collapse;width:100%;background:#fff;margin:.5rem 0 1rem}
th,td{padding:.4rem .6rem;border-bottom:1px solid #e3e6eb;text-align:left;
vertical-align:middle;font-size:.9rem}
th{background:#eef0f4}
.watch{background:#fff;border:1px solid #e3e6eb;border-radius:6px;
padding:.5rem 1rem;margin:1rem 0}
.alert-card{background:#fff;border:1px solid #e3e6eb;border-left:6px solid #8a93a6;
border-radius:6px;padding:.75rem 1rem;margin:1rem 0}
.alert-card.v-GENUINE_DEAL{border-left-color:#1f9d55}
.alert-card.v-FALSE_DISCOUNT{border-left-color:#d64545}
.alert-card.v-INFLATED_REFERENCE{border-left-color:#d4a017}
.alert-card.v-NORMAL_DROP{border-left-color:#2f6fd6}
.alert-card h3{margin:.4rem 0}
.alert-card ul{margin:.4rem 0;padding-left:1.2rem}
.reason{font-style:italic}
.badge{display:inline-block;font-size:.72rem;font-weight:700;padding:.1rem .45rem;
border-radius:3px;margin:0 .25rem .2rem 0;background:#e3e6eb;color:#1d2330}
.b-atl{background:#1f9d55;color:#fff}.b-omnibus{background:#2f6fd6;color:#fff}
.b-prehike{background:#d64545;color:#fff}.b-limited{background:#d4a017;color:#1d2330}
.s-ok{color:#1f9d55;font-weight:700}.s-warn{color:#b7791f;font-weight:700}
.s-bad{color:#d64545;font-weight:700}
svg.spark{display:block}
.spark .price{fill:none;stroke:#2f6fd6;stroke-width:1.5}
.spark .ref-line{stroke:#d64545;stroke-width:1;stroke-dasharray:4 3}
.spark .drop{fill:#d64545}.spark .last{fill:#1d2330}
"""


# --------------------------------------------------------------------------
# Loading (tolerant: a missing or malformed file degrades to "no data")
# --------------------------------------------------------------------------


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("store"), str):
            records.append(rec)
    return records


def load_report_data(data_dir: Path) -> dict[str, Any]:
    """Read every report input from data_dir without writing anything."""
    watches: list[dict[str, Any]] = []
    watchlist_error = None
    watchlist_path = data_dir / "watchlist.json"
    if watchlist_path.exists():
        try:
            watches = load_watchlist(watchlist_path)
        except (OSError, ValueError) as exc:
            watchlist_error = str(exc).splitlines()[0]

    history = _read_json(data_dir / "price_history.json", {})
    products = history.get("products", history) if isinstance(history, dict) else {}
    products = {
        k: v
        for k, v in products.items()
        if isinstance(v, dict) and isinstance(v.get("history"), list)
    }

    alerts = _read_json(data_dir / "formatted_alerts.json", [])
    health_alerts = _read_json(data_dir / "scrape_health_alerts.json", [])
    return {
        "watches": watches,
        "watchlist_error": watchlist_error,
        "products": products,
        "health_records": _read_jsonl(data_dir / "scrape_health.jsonl"),
        "health_alerts": [m for m in health_alerts if isinstance(m, str)]
        if isinstance(health_alerts, list)
        else [],
        "alerts": [a for a in alerts if isinstance(a, dict)]
        if isinstance(alerts, list)
        else [],
    }


# --------------------------------------------------------------------------
# Derivation
# --------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def build_product_rows(products: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per tracked product, with its 30-day reference computed the
    way scrape.py computes an alert's: the prior window excludes the latest
    observation, and an empty window falls back to the previous price."""
    rows = []
    for url, entry in products.items():
        history = [
            h
            for h in entry["history"]
            if isinstance(h, dict)
            and _is_number(h.get("price"))
            and _parse_date(h.get("date")) is not None
        ]
        row: dict[str, Any] = {
            "url": url,
            "title": str(entry.get("title", url)),
            "site": str(entry.get("site", "")),
            "prices": [float(h["price"]) for h in history],
            "first_date": history[0]["date"] if history else None,
            "last_date": history[-1]["date"] if history else None,
            "latest_price": None,
            "stock_status": None,
            "all_time_low": entry.get("all_time_low"),
            "thirty_day_low": None,
            "provenance": None,
            "deal_stats": {},
            "reference_label_html": None,
        }
        rows.append(row)
        if not history:
            continue

        latest, prior = history[-1], history[:-1]
        as_of = _parse_date(latest["date"])
        assert as_of is not None
        cutoff = as_of - timedelta(days=30)
        window = [
            {"date": h["date"], "price": h["price"]}
            for h in prior
            if (d := _parse_date(h["date"])) is not None and d >= cutoff
        ]
        prev_price = prior[-1]["price"] if prior else None
        pseudo_alert = {
            "new_price": latest["price"],
            "old_price": prev_price,
            "reference_price": entry.get("reference_price")
            if _is_number(entry.get("reference_price"))
            else None,
            "history_30d": window,
            "observed_at": as_of.isoformat(),
        }
        stats = analyze.build_deal_stats(pseudo_alert)
        provenance = analyze.thirty_day_window_provenance(pseudo_alert)
        row["latest_price"] = float(latest["price"])
        row["stock_status"] = latest.get("stock_status")
        row["deal_stats"] = stats
        row["provenance"] = provenance
        if prev_price is None:
            continue  # first sighting: no reference price exists yet
        if provenance == analyze.WINDOW_GENUINE:
            row["thirty_day_low"] = stats["reference_price_30d"]
        else:
            row["thirty_day_low"] = prev_price
        row["reference_label_html"] = notify._thirty_day_low_line(
            {"thirty_day_low": row["thirty_day_low"], "deal_stats": stats}
        )
    rows.sort(key=lambda r: (r["site"], r["title"].lower()))
    return rows


def _deal_badges(
    price: Any, all_time_low: Any, deal_stats: dict[str, Any], stock_status: Any
) -> list[tuple[str, str]]:
    badges = []
    if _is_number(price) and _is_number(all_time_low) and price <= all_time_low:
        badges.append(("b-atl", BADGE_ATL))
    # is_legal_discount is only True when the prior 30-day window was
    # nonempty and the price beats its minimum -- i.e. a genuine Omnibus low.
    if deal_stats.get("is_legal_discount") is True:
        badges.append(("b-omnibus", BADGE_OMNIBUS))
    if deal_stats.get("pre_sale_hike_detected") is True:
        badges.append(("b-prehike", BADGE_PRE_HIKE))
    if stock_status == "limited_stock":
        badges.append(("b-limited", BADGE_LIMITED))
    return badges


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _money(value: Any) -> str:
    return f"{value:,.2f} RON" if _is_number(value) else "—"


def _safe_href(url: Any) -> str | None:
    url = str(url or "")
    return url if url.lower().startswith(("https://", "http://")) else None


def _link(url: Any, text: str) -> str:
    href = _safe_href(url)
    if href is None:
        return _e(text)
    return f'<a href="{_e(href)}" rel="noopener noreferrer">{_e(text)}</a>'


def _badges_html(badges: Sequence[tuple[str, str]]) -> str:
    return "".join(f'<span class="badge {cls}">{_e(t)}</span>' for cls, t in badges)


def sparkline_svg(
    prices: Sequence[float], reference: float | None, caption: str
) -> str:
    """Inline SVG price chart: price polyline, dashed 30-day reference line,
    red dots at each observed drop, dark dot on the latest price."""
    if len(prices) < 2:
        return '<span class="muted">single observation</span>'
    values = [*prices, *([reference] if reference is not None else [])]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = (SPARK_WIDTH - 2 * SPARK_PAD) / (len(prices) - 1)

    def x(i: int) -> float:
        return SPARK_PAD + i * step

    def y(v: float) -> float:
        return SPARK_PAD + (hi - v) / span * (SPARK_HEIGHT - 2 * SPARK_PAD)

    points = " ".join(f"{x(i):.1f},{y(p):.1f}" for i, p in enumerate(prices))
    parts = [
        f'<svg class="spark" width="{SPARK_WIDTH}" height="{SPARK_HEIGHT}" '
        f'viewBox="0 0 {SPARK_WIDTH} {SPARK_HEIGHT}" role="img" '
        f'aria-label="{_e(caption)}">',
        f"<title>{_e(caption)}</title>",
    ]
    if reference is not None:
        ry = f"{y(reference):.1f}"
        parts.append(
            f'<line class="ref-line" x1="{SPARK_PAD}" x2="{SPARK_WIDTH - SPARK_PAD}" '
            f'y1="{ry}" y2="{ry}" />'
        )
    parts.append(f'<polyline class="price" points="{points}" />')
    for i in range(1, len(prices)):
        if prices[i] < prices[i - 1]:
            parts.append(
                f'<circle class="drop" cx="{x(i):.1f}" cy="{y(prices[i]):.1f}" r="2" />'
            )
    last = len(prices) - 1
    parts.append(
        f'<circle class="last" cx="{x(last):.1f}" cy="{y(prices[last]):.1f}" r="2.5" />'
    )
    parts.append("</svg>")
    return "".join(parts)


def _render_alert(alert: dict[str, Any]) -> str:
    verdict = str(alert.get("verdict", ""))
    emoji, label = notify.VERDICT_BADGES.get(verdict, ("⚪", "VERDICT NECUNOSCUT"))
    deal_stats = alert.get("deal_stats") or {}
    try:
        reference_line = notify._thirty_day_low_line(alert)
    except (KeyError, TypeError, ValueError):
        reference_line = "⚖️ Preț de referință: —"
    detail_lines = notify._omnibus_detail_lines(deal_stats)
    badges = _deal_badges(
        alert.get("new_price"),
        alert.get("all_time_low"),
        deal_stats,
        alert.get("stock_status"),
    )
    items = [
        f"<li>Preț nou: <b>{_money(alert.get('new_price'))}</b></li>",
        f"<li>Preț anterior: {_money(alert.get('old_price'))}</li>",
        f"<li>{reference_line}</li>",
        *(f"<li>{line}</li>" for line in detail_lines),
        f"<li>Record minim istoric: {_money(alert.get('all_time_low'))}</li>",
    ]
    site = scrape.STORE_DISPLAY_NAMES.get(str(alert.get("site")), alert.get("site"))
    return (
        f'<article class="alert-card v-{_e(verdict)}">'
        f'<div><span class="badge">{_e(emoji)} {_e(label)}</span>'
        f"{_badges_html(badges)}"
        f'<span class="muted">Scor: {_e(alert.get("verdict_score", "?"))}/10 · '
        f"rule verdict: {_e(alert.get('rule_verdict', '—'))}</span></div>"
        f"<h3>{_link(alert.get('url'), str(alert.get('title', '')))}</h3>"
        f'<p class="muted">{_e(site)} · Vânzător: '
        f"{_e(alert.get('seller') or 'Neverificat')} · Stoc: "
        f"{_e(alert.get('stock_status', 'unknown'))} · "
        f"{_e(alert.get('observed_at', ''))}</p>"
        f"<ul>{''.join(items)}</ul>"
        f'<p class="reason">{_e(alert.get("summary", ""))}</p>'
        "</article>"
    )


def _render_alerts(alerts: list[dict[str, Any]]) -> str:
    body = (
        "".join(_render_alert(a) for a in alerts)
        if alerts
        else '<p class="muted">No alerts in this run.</p>'
    )
    return (
        f'<section id="alerts"><h2>Recent alerts ({len(alerts)})</h2>{body}</section>'
    )


def _render_product_table(rows: list[dict[str, Any]]) -> str:
    out = [
        "<table><thead><tr><th>Product</th><th>Latest</th>"
        "<th>30-day reference</th><th>History</th></tr></thead><tbody>"
    ]
    for row in rows:
        caption = (
            f"{row['title']}: {len(row['prices'])} observations "
            f"{row['first_date']} to {row['last_date']}"
        )
        badges = _deal_badges(
            row["latest_price"],
            row["all_time_low"],
            row["deal_stats"],
            row["stock_status"],
        )
        out.append(
            f"<tr><td>{_link(row['url'], row['title'])}<br>{_badges_html(badges)}"
            f'<span class="muted">{_e(row["site"])}</span></td>'
            f"<td>{_money(row['latest_price'])}<br>"
            f'<span class="muted">{_e(row["last_date"] or "—")}</span></td>'
            f"<td>{row['reference_label_html'] or '—'}</td>"
            f"<td>{sparkline_svg(row['prices'], row['thirty_day_low'], caption)}</td>"
            "</tr>"
        )
    out.append("</tbody></table>")
    return "".join(out)


def _render_watches(data: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    out = [f'<section id="watches"><h2>Watches ({len(data["watches"])})</h2>']
    if data["watchlist_error"]:
        out.append(
            f'<p class="s-bad">Watchlist could not be loaded: '
            f"{_e(data['watchlist_error'])}</p>"
        )
    matched: set[str] = set()
    for watch in data["watches"]:
        site, query = str(watch.get("site", "")), str(watch.get("query", ""))
        watch_rows = [
            r
            for r in rows
            # A fan-out watch spans every retailer, so match on query alone.
            if site in (r["site"], scrape.FANOUT_SITE)
            and scrape.title_matches_query(r["title"], query)
        ]
        matched.update(r["url"] for r in watch_rows)
        rules = []
        if _is_number(watch.get("target_price")):
            rules.append(f"target {_money(watch['target_price'])}")
        if _is_number(watch.get("min_drop_percent")):
            rules.append(f"min drop {watch['min_drop_percent']}%")
        if watch.get("enabled") is False:
            rules.append("disabled")
        store = scrape.STORE_DISPLAY_NAMES.get(site, site)
        out.append(
            f'<div class="watch"><h3>{_e(store)}: “{_e(query)}”</h3>'
            f'<p class="muted">{_e(" · ".join(rules) or "no extra rules")} · '
            f"{len(watch_rows)} matching product(s)</p>"
        )
        out.append(
            _render_product_table(watch_rows)
            if watch_rows
            else '<p class="muted">No tracked products match this watch.</p>'
        )
        out.append("</div>")
    others = [r for r in rows if r["url"] not in matched]
    if others:
        out.append(
            '<div class="watch"><h3>Other tracked products</h3>'
            f'<p class="muted">{len(others)} product(s) in price history not '
            "matched to a current watch.</p>"
        )
        out.append(_render_product_table(others))
        out.append("</div>")
    if not rows:
        out.append('<p class="muted">No price history recorded.</p>')
    out.append("</section>")
    return "".join(out)


def _render_health(data: dict[str, Any]) -> str:
    records = [
        r
        for r in data["health_records"]
        if "run_id" in r and _is_number(r.get("products_parsed"))
    ]
    out = ['<section id="health"><h2>Store health</h2>']
    for message in data["health_alerts"]:
        out.append(f'<p class="s-bad">{_e(message)}</p>')
    if not records:
        out.append('<p class="muted">No store health records for this run.</p>')
        out.append("</section>")
        return "".join(out)

    quarantined = scrape._quarantined_stores(records)
    stores = list(dict.fromkeys(r["store"] for r in records))
    out.append(
        "<table><thead><tr><th>Store</th><th>Status</th><th>Last run (UTC)</th>"
        "<th>Products parsed</th><th>Matched</th><th>Parse failures</th>"
        "<th>Challenge</th><th>Latency (s)</th><th>Last known good (UTC)</th>"
        "</tr></thead><tbody>"
    )
    for store in stores:
        rec = scrape._previous_record(records, store)
        assert rec is not None
        if store in quarantined:
            status = ("s-bad", "QUARANTINED")
        elif rec["products_parsed"] == 0:
            status = ("s-warn", "NO PRODUCTS")
        elif rec.get("challenge_detected"):
            status = ("s-warn", "CHALLENGED")
        else:
            status = ("s-ok", "OK")
        challenge = (
            "blocked"
            if rec.get("challenge_detected")
            else "cleared"
            if rec.get("challenge_wait_entered")
            else "none"
        )
        out.append(
            f"<tr><td>{_e(scrape.STORE_DISPLAY_NAMES.get(store, store))}</td>"
            f'<td class="{status[0]}">{status[1]}</td>'
            f"<td>{_e(rec.get('run_started_utc', '—'))}</td>"
            f"<td>{_e(rec['products_parsed'])}</td>"
            f"<td>{_e(rec.get('matched_count', '—'))}</td>"
            f"<td>{_e(rec.get('parse_failures', '—'))}</td>"
            f"<td>{challenge}</td>"
            f"<td>{_e(rec.get('latency_seconds', '—'))}</td>"
            f"<td>{_e(rec.get('last_known_good_utc') or '—')}</td></tr>"
        )
    out.append("</tbody></table></section>")
    return "".join(out)


def build_report_html(data: dict[str, Any], generated_at: datetime) -> str:
    """Render the whole report as one self-contained HTML string."""
    rows = build_product_rows(data["products"])
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>BF Price Monitor report</title>"
        f"<style>{_CSS}</style></head><body>"
        "<header><h1>BF Price Monitor report</h1>"
        f'<p class="muted">Generated {_e(generated_at.isoformat())} · '
        f"{len(data['alerts'])} alert(s) · {len(rows)} tracked product(s) · "
        "static page, no scripts or network access needed. Contains personal "
        "watchlist and price data: shared as a workflow artifact only, never "
        "GitHub Pages.</p></header>"
        f"{_render_alerts(data['alerts'])}"
        f"{_render_watches(data, rows)}"
        f"{_render_health(data)}"
        "</body></html>\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    data = load_report_data(args.data_dir)
    page = build_report_html(data, datetime.now(UTC))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(page, encoding="utf-8")
    print(
        f"Wrote {args.output} ({len(page):,} bytes, {len(data['alerts'])} alert(s), "
        f"{len(data['products'])} product(s))"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
