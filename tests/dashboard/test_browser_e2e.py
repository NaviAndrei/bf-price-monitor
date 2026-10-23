"""End-to-end browser tests: the automated twin of
``docs/qa/dashboard-e2e-test-cases.md``. Test names carry the case IDs (TC-nn).

Each test drives real Chromium against a live dashboard server and a scratch git
origin, and checks both what the page shows and what reached the origin.
Opt-in: ``.venv/Scripts/python.exe -m pytest -m browser``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator

import pytest
from playwright.sync_api import Locator, Page, expect
from scratch_repo import TOKEN, Scratch, other_edit_watchlist

pytestmark = pytest.mark.browser
pytest_plugins = ["browser_fixtures"]

Dashboard = tuple[Scratch, str]
LENOVO = "laptop lenovo v15"
VIVOBOOK = "laptop asus vivobook"


@pytest.fixture(autouse=True)
def console_errors(page: Page) -> Iterator[list[str]]:
    """TC-24: no uncaught script errors. A refused request is logged by the
    browser as a failed resource load; the page reports those in a banner."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on(
        "console",
        lambda msg: (
            errors.append(msg.text)
            if msg.type == "error" and "Failed to load resource" not in msg.text
            else None
        ),
    )
    yield errors
    assert errors == []


def open_dashboard(page: Page, base: str, token: str | None = TOKEN) -> None:
    page.goto(base + (f"/#token={token}" if token else "/"))
    expect(page.locator("#clone-status")).to_contain_text("clone")
    page.locator("#watch-rows tr").first.wait_for()


def row(page: Page, query: str) -> Locator:
    return page.locator("#watch-rows tr", has_text=query)


def cell(page: Page, query: str, column: int) -> Locator:
    return row(page, query).locator("td").nth(column)


def wait_for_subject(scratch: Scratch, prefix: str) -> str:
    for _ in range(150):
        subject = scratch.origin_subjects()[0]
        if subject.startswith(prefix):
            return subject
        time.sleep(0.1)
    raise AssertionError(f"no commit starting {prefix!r}: {scratch.origin_subjects()}")


def add_watch(
    page: Page,
    query: str,
    *,
    price: str = "",
    drop: str = "",
    channels: tuple[str, ...] = (),
) -> None:
    form = page.locator("#add-form")
    if not form.locator("[name=query]").is_visible():
        page.locator("#add-section summary").click()
    form.locator("[name=query]").fill(query)
    form.locator("[name=target_price]").fill(price)
    form.locator("[name=min_drop_percent]").fill(drop)
    for name in channels:
        form.locator(f"#add-channels input[value={name}]").check()
    form.get_by_role("button", name="Add").click()


def banner_texts(page: Page) -> Locator:
    return page.locator("#banners .banner")


# --- access and token ------------------------------------------------------


def test_tc01_token_link_loads_and_hides_token(
    running_dashboard: Dashboard, page: Page
):
    _, base = running_dashboard
    open_dashboard(page, base)
    expect(page.locator("#watch-rows tr")).to_have_count(2)
    expect(page.locator("#clone-status")).to_have_text(
        re.compile(r"clone ok \(.+ @ [0-9a-f]+\)")
    )
    assert "token=" not in page.url
    expect(page.locator("#token-box")).to_be_hidden()


def test_tc02_no_token_shows_box_and_reads_work(
    running_dashboard: Dashboard, page: Page
):
    _, base = running_dashboard
    open_dashboard(page, base, token=None)
    expect(page.locator("#watch-rows tr")).to_have_count(2)
    expect(page.locator("#token-box")).to_be_visible()


def test_tc03_pasted_token_allows_writes(running_dashboard: Dashboard, page: Page):
    scratch, base = running_dashboard
    open_dashboard(page, base, token=None)
    page.locator("#token-input").fill(TOKEN)
    page.get_by_role("button", name="Use token").click()
    expect(page.locator("#token-box")).to_be_hidden()
    row(page, LENOVO).get_by_role("button", name="On").click()
    wait_for_subject(scratch, "chore(watchlist): disable")


def test_tc04_reload_keeps_token(running_dashboard: Dashboard, page: Page):
    scratch, base = running_dashboard
    open_dashboard(page, base)
    page.reload()
    page.locator("#watch-rows tr").first.wait_for()
    expect(page.locator("#token-box")).to_be_hidden()
    row(page, LENOVO).get_by_role("button", name="On").click()
    wait_for_subject(scratch, "chore(watchlist): disable")


def test_tc05_wrong_token_refused_and_forgotten(
    running_dashboard: Dashboard, page: Page
):
    scratch, base = running_dashboard
    before = scratch.origin_subjects()
    open_dashboard(page, base, token="wrong")
    row(page, LENOVO).get_by_role("button", name="On").click()
    expect(banner_texts(page)).to_have_count(1)
    expect(banner_texts(page)).to_contain_text("Token missing or wrong")
    expect(page.locator("#token-box")).to_be_visible()
    assert scratch.origin_subjects() == before
    page.reload()
    page.locator("#watch-rows tr").first.wait_for()
    expect(page.locator("#token-box")).to_be_visible()


# --- enable and disable ----------------------------------------------------


def test_tc06_tc07_toggle_off_and_on(running_dashboard: Dashboard, page: Page):
    scratch, base = running_dashboard
    open_dashboard(page, base)
    target = row(page, LENOVO)
    target.get_by_role("button", name="On").click()
    expect(target.get_by_role("button", name="Off")).to_be_visible()
    expect(target).to_have_class("off")
    assert wait_for_subject(scratch, "chore(watchlist): disable").endswith(
        f'emag "{LENOVO}"'
    )
    target.get_by_role("button", name="Off").click()
    expect(target.get_by_role("button", name="On")).to_be_visible()
    wait_for_subject(scratch, "chore(watchlist): enable")


# --- add -------------------------------------------------------------------


def test_tc08_add_watch_with_all_fields(running_dashboard: Dashboard, page: Page):
    scratch, base = running_dashboard
    open_dashboard(page, base)
    add_watch(page, "qa monitor 27", price="999.5", drop="10", channels=("telegram",))
    expect(cell(page, "qa monitor 27", 2)).to_have_text("999.5")
    expect(cell(page, "qa monitor 27", 3)).to_have_text("10")
    expect(cell(page, "qa monitor 27", 4)).to_have_text("telegram")
    expect(page.locator("#add-form [name=query]")).to_have_value("")
    assert (
        wait_for_subject(scratch, "chore(watchlist): add")
        == 'chore(watchlist): add emag "qa monitor 27"'
    )


def test_tc09_duplicate_add_refused(running_dashboard: Dashboard, page: Page):
    scratch, base = running_dashboard
    open_dashboard(page, base)
    add_watch(page, "qa monitor 27")
    wait_for_subject(scratch, "chore(watchlist): add")
    add_watch(page, "QA  Monitor 27")
    expect(page.locator("#add-error")).to_contain_text("already watching")
    expect(page.locator("#watch-rows tr")).to_have_count(3)
    # The refusal is shown once, on the form; it must not reappear as a
    # second notice under "Edits that could not be applied" (defect F-01).
    expect(page.locator("#conflicts-section")).to_be_hidden()
    assert len(scratch.origin_subjects()) == 2


def test_tc10_blank_query_refused(running_dashboard: Dashboard, page: Page):
    scratch, base = running_dashboard
    open_dashboard(page, base)
    add_watch(page, "   ")
    expect(page.locator("#add-error")).to_have_text("query must not be empty")
    assert scratch.origin_subjects() == ["base"]


def test_tc11_markup_in_query_is_text(running_dashboard: Dashboard, page: Page):
    scratch, base = running_dashboard
    open_dashboard(page, base)
    text = "<img src=x onerror=alert(1)>"
    add_watch(page, text)
    expect(cell(page, "onerror", 1)).to_have_text(text)
    assert page.locator("#watch-rows img").count() == 0
    wait_for_subject(scratch, "chore(watchlist): add")


# --- edit ------------------------------------------------------------------


def open_edit(page: Page, query: str) -> Locator:
    row(page, query).get_by_role("button", name="Edit").click()
    dialog = page.locator("#edit-dialog")
    expect(dialog).to_be_visible()
    return dialog


def test_tc12_edit_price_and_channels(running_dashboard: Dashboard, page: Page):
    scratch, base = running_dashboard
    open_dashboard(page, base)
    dialog = open_edit(page, LENOVO)
    expect(dialog.locator("[name=target_price]")).to_have_value("2500")
    expect(dialog.locator("#edit-channels input[value=telegram]")).to_be_checked()
    dialog.locator("[name=target_price]").fill("850")
    dialog.locator("#edit-channels input[value=email]").check()
    dialog.get_by_role("button", name="Save").click()
    expect(dialog).to_be_hidden()
    expect(cell(page, LENOVO, 2)).to_have_text("850")
    expect(cell(page, LENOVO, 4)).to_have_text("telegram, email")
    wait_for_subject(scratch, "chore(watchlist): update")


def test_tc13_edit_without_change(running_dashboard: Dashboard, page: Page):
    scratch, base = running_dashboard
    open_dashboard(page, base)
    dialog = open_edit(page, LENOVO)
    dialog.get_by_role("button", name="Save").click()
    expect(page.locator("#edit-error")).to_have_text("Nothing changed.")
    expect(dialog).to_be_visible()
    assert scratch.origin_subjects() == ["base"]


def test_tc14_edit_cancel(running_dashboard: Dashboard, page: Page):
    scratch, base = running_dashboard
    open_dashboard(page, base)
    dialog = open_edit(page, LENOVO)
    dialog.locator("[name=target_price]").fill("1")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()
    expect(cell(page, LENOVO, 2)).to_have_text("2500")
    assert scratch.origin_subjects() == ["base"]


def test_tc15_clear_target_price(running_dashboard: Dashboard, page: Page):
    scratch, base = running_dashboard
    open_dashboard(page, base)
    dialog = open_edit(page, LENOVO)
    dialog.locator("[name=target_price]").fill("")
    dialog.get_by_role("button", name="Save").click()
    expect(dialog).to_be_hidden()
    expect(cell(page, LENOVO, 2)).to_have_text("–")
    wait_for_subject(scratch, "chore(watchlist): update")
    assert "target_price" not in scratch.origin_doc()["watches"][0]


def test_tc16_out_of_range_refused_with_the_right_reason(
    running_dashboard: Dashboard, page: Page
):
    scratch, base = running_dashboard
    open_dashboard(page, base)
    dialog = open_edit(page, VIVOBOOK)
    dialog.locator("[name=min_drop_percent]").fill("0")
    dialog.get_by_role("button", name="Save").click()
    expect(page.locator("#edit-error")).to_have_text(
        "min_drop_percent must be between 0 and 100"
    )
    dialog.locator("[name=min_drop_percent]").fill("5")
    dialog.locator("[name=target_price]").fill("20000000")
    dialog.get_by_role("button", name="Save").click()
    # defect F-03: the old text said only "greater than 0" for a too-large price
    expect(page.locator("#edit-error")).to_contain_text("at most 10,000,000")
    expect(dialog).to_be_visible()
    assert scratch.origin_subjects() == ["base"]


# --- remove ----------------------------------------------------------------


def test_tc17_tc18_remove_cancel_then_confirm(running_dashboard: Dashboard, page: Page):
    scratch, base = running_dashboard
    open_dashboard(page, base)
    page.once("dialog", lambda dialog: dialog.dismiss())
    row(page, VIVOBOOK).get_by_role("button", name="Remove").click()
    expect(page.locator("#watch-rows tr")).to_have_count(2)
    assert scratch.origin_subjects() == ["base"]
    page.once("dialog", lambda dialog: dialog.accept())
    row(page, VIVOBOOK).get_by_role("button", name="Remove").click()
    expect(page.locator("#watch-rows tr")).to_have_count(1)
    wait_for_subject(scratch, "chore(watchlist): remove")


# --- failure handling ------------------------------------------------------


def test_tc19_tc20_offline_save_then_retry(running_dashboard: Dashboard, page: Page):
    scratch, base = running_dashboard
    open_dashboard(page, base)
    scratch.take_origin_offline()
    row(page, LENOVO).get_by_role("button", name="On").click()
    expect(banner_texts(page).first).to_contain_text("Saved locally, not pushed")
    expect(page.locator("#pending-section")).to_be_visible()
    expect(page.locator("#pending-list li")).to_have_count(1)
    scratch.bring_origin_back()
    assert scratch.origin_subjects() == ["base"]
    banner_texts(page).get_by_role("button", name="Retry push").first.click()
    expect(banner_texts(page)).to_have_count(0)
    expect(page.locator("#pending-section")).to_be_hidden()
    wait_for_subject(scratch, "chore(watchlist): disable")


def test_tc21_conflict_not_overwritten(running_dashboard: Dashboard, page: Page):
    scratch, base = running_dashboard
    open_dashboard(page, base)
    other_edit_watchlist(
        scratch, lambda doc: doc["watches"][0].update(target_price=2222.0)
    )
    row(page, LENOVO).get_by_role("button", name="On").click()
    expect(banner_texts(page)).to_contain_text("changed somewhere else")
    assert scratch.origin_subjects()[0] == "other edit"
    assert scratch.origin_doc()["watches"][0]["target_price"] == 2222.0
    banner_texts(page).get_by_role("button", name="Reload").click()
    expect(cell(page, LENOVO, 2)).to_have_text("2222")


def test_tc22_refresh_shows_remote_edit(running_dashboard: Dashboard, page: Page):
    scratch, base = running_dashboard
    open_dashboard(page, base)
    other_edit_watchlist(
        scratch, lambda doc: doc["watches"][1].update(min_drop_percent=7.0)
    )
    page.get_by_role("button", name="Refresh from GitHub").click()
    expect(cell(page, VIVOBOOK, 3)).to_have_text("7")


# --- display ---------------------------------------------------------------


def test_tc23_readonly_details(running_dashboard: Dashboard, page: Page):
    _, base = running_dashboard
    open_dashboard(page, base)
    details = cell(page, LENOVO, 6)
    expect(details).to_contain_text("seller_policy: any")
    details.locator("summary").click()
    expect(details).to_contain_text("cadence_minutes: 120")
    expect(details).to_contain_text("owner: NaviAndrei")
    assert details.locator("input").count() == 0


# --- double submit ---------------------------------------------------------


def test_tc25_double_press_save_makes_one_commit(
    running_dashboard: Dashboard, page: Page
):
    """Both presses reach the server (the page has no busy state, F-02); the
    second finds the edit already applied, so there is one commit, no queued
    copy and no conflict entry."""
    scratch, base = running_dashboard
    updates: list[int] = []
    page.on(
        "response",
        lambda r: updates.append(r.status) if r.url.endswith("/update") else None,
    )
    open_dashboard(page, base)
    dialog = open_edit(page, LENOVO)
    dialog.locator("[name=target_price]").fill("850")
    dialog.get_by_role("button", name="Save").dblclick()
    expect(dialog).to_be_hidden()
    wait_for_subject(scratch, "chore(watchlist): update")
    page.wait_for_timeout(2000)  # let a second request, if any, finish
    assert updates == [200, 200]
    updates_on_origin = [
        s for s in scratch.origin_subjects() if s.startswith("chore(watchlist): update")
    ]
    assert len(updates_on_origin) == 1
    expect(page.locator("#conflicts-section")).to_be_hidden()
    expect(page.locator("#pending-section")).to_be_hidden()
    expect(banner_texts(page)).to_have_count(0)
    expect(cell(page, LENOVO, 2)).to_have_text("850")


def test_tc26_changed_price_between_two_presses_of_save(
    running_dashboard: Dashboard, page: Page
):
    """The second press carries the old row, so the server refuses it (409):
    the first value wins, the second is not applied and is not queued. The
    refusal is shown twice (red banner and the conflicts list); see F-07."""
    scratch, base = running_dashboard
    statuses: list[int] = []
    page.on(
        "response",
        lambda r: statuses.append(r.status) if r.url.endswith("/update") else None,
    )
    open_dashboard(page, base)
    dialog = open_edit(page, LENOVO)
    dialog.locator("[name=target_price]").fill("850")
    dialog.get_by_role("button", name="Save").click()
    dialog.locator("[name=target_price]").fill("900")
    dialog.get_by_role("button", name="Save").click()
    expect(dialog).to_be_hidden()
    wait_for_subject(scratch, "chore(watchlist): update")
    expect(page.locator("#conflicts-list li")).to_have_count(1)
    page.wait_for_timeout(1000)
    assert statuses == [200, 409]
    updates_on_origin = [
        s for s in scratch.origin_subjects() if s.startswith("chore(watchlist): update")
    ]
    assert len(updates_on_origin) == 1
    assert scratch.origin_doc()["watches"][0]["target_price"] == 850
    expect(cell(page, LENOVO, 2)).to_have_text("850")
    expect(banner_texts(page)).to_have_count(1)
    expect(banner_texts(page).first).to_contain_text("changed somewhere else")
    expect(page.locator("#pending-section")).to_be_hidden()
