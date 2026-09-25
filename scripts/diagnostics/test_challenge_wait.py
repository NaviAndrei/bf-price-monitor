"""One-off diagnostic for T-22/#31: exercises the real challenge-wait code
path (scripts.scrape.fetch_with_browser -> is_challenge_page ->
_wait_out_challenge) against https://nowsecure.nl, a public Cloudflare
JS-challenge test page, instead of waiting for a retailer to serve one.

Not part of the production scrape path: does not touch data/watchlist.json,
does not call scrape.py's main(), and is not wired into any CI workflow.
Run manually, then delete or leave here (not picked up by pytest -- no
test_*.py naming inside a package pytest collects, and no test_ functions).
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import scrape  # noqa: E402

TARGET_URL = "https://nowsecure.nl"
SITE_NAME = "diagnostic-nowsecure"


def run_once(run_number: int) -> None:
    print(f"\n=== Run {run_number} ===")
    scrape._run_state.reset(f"diag-{run_number}")

    start = time.monotonic()
    html = scrape.fetch_with_browser(TARGET_URL, SITE_NAME)
    elapsed = time.monotonic() - start

    detected = scrape._run_state.challenge_counts.get(SITE_NAME, 0) > 0
    wait_entered = scrape._run_state.challenge_wait_entered_counts.get(SITE_NAME, 0) > 0
    final_is_challenge = scrape.is_challenge_page(html) if html is not None else None

    print(f"elapsed_seconds={elapsed:.2f}")
    print(f"fetch_with_browser returned html: {html is not None}")
    print(f"still-blocked-after-wait (challenge_counts): {detected}")
    print(f"bounded wait entered (challenge_wait_entered_counts): {wait_entered}")
    print(f"final page still shows challenge markup: {final_is_challenge}")
    if html is not None:
        print(f"final html length: {len(html)}")
        print(f"final html head (300 chars): {html[:300]!r}")


if __name__ == "__main__":
    try:
        for i in range(1, 4):
            run_once(i)
    finally:
        scrape._browser_state.close()
