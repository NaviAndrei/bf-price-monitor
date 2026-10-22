"""Browser smoke test: the dashboard page run in real Chromium.

The other dashboard tests check the page's source text and call the API
directly. This one loads the page, follows the token link, toggles a watch and
checks the commit that reaches the scratch origin, so it catches a broken
script that the source checks cannot see.

Opt-in: excluded from the default run (see ``addopts`` in pyproject.toml).
Run it with ``.venv/Scripts/python.exe -m pytest -m browser``. It needs the
Chromium build that ``playwright install chromium`` installs.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page
from scratch_repo import TOKEN, Scratch

pytestmark = pytest.mark.browser
pytest_plugins = ["browser_fixtures"]


def test_page_loads_from_the_token_link_and_toggles_a_watch(
    running_dashboard: tuple[Scratch, str], page: Page
):
    scratch, base = running_dashboard
    page.goto(f"{base}/#token={TOKEN}")

    rows = page.locator("#watch-rows tr")
    rows.first.wait_for()
    assert rows.count() == 2
    # The token leaves the address bar once the page has read it.
    assert "token=" not in page.url

    rows.first.get_by_role("button", name="On").click()
    # The save is a push to the scratch origin; wait for that commit to land.
    for _ in range(100):
        subjects = scratch.origin_subjects()
        if subjects[0].startswith("chore(watchlist): disable"):
            break
        page.wait_for_timeout(100)
    assert subjects[0].startswith("chore(watchlist): disable"), subjects
    doc = scratch.origin_doc()
    assert sum(1 for watch in doc["watches"] if watch["enabled"]) == 1
