"""T-21 (#29): browser-reuse lifecycle tests.

These exercise scrape._BrowserState directly, using fakes for
playwright_stealth.Stealth / playwright.sync_api.sync_playwright so no real
Chromium process is launched. Fakes stand in one level below the public
Playwright API scrape.py calls (chromium.launch, browser.new_context,
context.new_page, page.goto/content/close), so the real fetch_with_browser
code path runs unmodified against them.
"""

import pytest
import scrape


class FakeResponse:
    def __init__(self, status):
        self.status = status


class FakeLocator:
    # Mirrors the one-and-only real Playwright Locator call scrape.py makes
    # (page.locator(...).first.wait_for(state=..., timeout=...)), not the
    # full Locator API. `behavior` controls what wait_for() does, matching
    # the real contract: it raises scrape.PlaywrightTimeoutError when the
    # requested state never arrives within timeout, and a real page
    # navigating away mid-wait surfaces as scrape.PlaywrightError instead.
    #   - "absent": nothing matches the selector -- wait_for(visible) times out
    #   - "resolves": becomes visible, then hidden -- a challenge that clears
    #   - "never_clears": becomes visible, stays visible -- wait_for(hidden) times out
    #   - "frame_detached": becomes visible, then the page navigates away
    #     while waiting for hidden (Playwright raises Error, not Timeout)
    def __init__(self, behavior="absent"):
        self.behavior = behavior
        self.wait_for_calls = []

    @property
    def first(self):
        return self

    def wait_for(self, state, timeout):
        self.wait_for_calls.append((state, timeout))
        if self.behavior == "absent":
            raise scrape.PlaywrightTimeoutError("no matching element")
        if state == "visible":
            return None
        if self.behavior == "resolves":
            return None
        if self.behavior == "never_clears":
            raise scrape.PlaywrightTimeoutError("still visible")
        if self.behavior == "frame_detached":
            raise scrape.PlaywrightError("frame was detached")
        return None


class FakePage:
    def __init__(self, context):
        self.context = context
        self.closed = False
        self.goto_error = None
        self.content_results = ["<html>ok</html>"]
        self._content_calls = 0
        self.locator_behavior = "absent"
        self.locator_calls = []

    def set_default_navigation_timeout(self, ms):
        pass

    def route(self, pattern, handler):
        pass

    def goto(self, url, timeout, wait_until):
        if self.goto_error is not None:
            raise self.goto_error
        return FakeResponse(status=200)

    def content(self):
        idx = min(self._content_calls, len(self.content_results) - 1)
        result = self.content_results[idx]
        self._content_calls += 1
        if isinstance(result, Exception):
            raise result
        return result

    def locator(self, selector):
        self.locator_calls.append(selector)
        self.last_locator = FakeLocator(self.locator_behavior)
        return self.last_locator

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, browser, name, user_agent=None):
        self.browser = browser
        self.name = name
        self.user_agent = user_agent
        self.pages = []
        self.cookies = {}
        self.closed = False

    def new_page(self):
        # Mirrors the real playwright.sync_api.BrowserContext.new_page(),
        # which takes no arguments -- the user agent is configured once at
        # new_context() time, not per page. Deliberately strict (no **kwargs
        # passthrough) so a caller passing user_agent= here fails loudly
        # instead of the fake silently accepting an invalid real API call.
        page = FakePage(self)
        self.pages.append(page)
        return page

    def add_cookie(self, key, value):
        self.cookies[key] = value

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.contexts = []
        self.new_context_calls = 0
        self.closed = False

    def new_context(self, **kwargs):
        self.new_context_calls += 1
        ctx = FakeContext(
            self, f"ctx{self.new_context_calls}", user_agent=kwargs.get("user_agent")
        )
        self.contexts.append(ctx)
        return ctx

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self):
        self.launch_calls = 0
        self.last_browser = None

    def launch(self, headless=True):
        self.launch_calls += 1
        self.last_browser = FakeBrowser()
        return self.last_browser


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()


class FakePlaywrightContextManager:
    def __init__(self):
        self.pw = FakePlaywright()
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self.pw

    def __exit__(self, *args):
        self.exited = True


class FakeStealth:
    # Mirrors playwright_stealth.Stealth().use_sync(ctx): a passthrough is
    # sufficient here since the goal is testing scrape.py's lifecycle
    # management, not playwright_stealth's own patching (verified separately
    # by reading its source: it patches the browser instance in place, so
    # reuse across new_context() calls is safe).
    def use_sync(self, ctx):
        return ctx


def install_fakes(monkeypatch):
    fake_cm = FakePlaywrightContextManager()
    monkeypatch.setattr(scrape, "sync_playwright", lambda: fake_cm)
    monkeypatch.setattr(scrape, "Stealth", FakeStealth)
    return fake_cm


# --- 1: exactly one browser launch across multiple sites --------------------


def test_one_browser_launch_across_pcgarage_and_flanco(monkeypatch):
    fake_cm = install_fakes(monkeypatch)
    bs = scrape._BrowserState()
    bs.start()

    bs.get_context("pcgarage")
    bs.get_context("flanco")

    assert fake_cm.pw.chromium.launch_calls == 1


# --- 2: same-site context is cached ------------------------------------------


def test_get_context_is_cached_for_same_site(monkeypatch):
    install_fakes(monkeypatch)
    bs = scrape._BrowserState()
    bs.start()

    first = bs.get_context("pcgarage")
    second = bs.get_context("pcgarage")

    assert first is second
    assert bs._browser.new_context_calls == 1


# --- 3: different sites get distinct contexts --------------------------------


def test_get_context_distinct_per_site(monkeypatch):
    install_fakes(monkeypatch)
    bs = scrape._BrowserState()
    bs.start()

    pcgarage_ctx = bs.get_context("pcgarage")
    flanco_ctx = bs.get_context("flanco")

    assert pcgarage_ctx is not flanco_ctx


# --- 4: no state crosses between site contexts -------------------------------


def test_context_state_does_not_cross_sites(monkeypatch):
    install_fakes(monkeypatch)
    bs = scrape._BrowserState()
    bs.start()

    pcgarage_ctx = bs.get_context("pcgarage")
    flanco_ctx = bs.get_context("flanco")
    pcgarage_ctx.add_cookie("cf_clearance", "pcgarage-token")

    assert "cf_clearance" not in flanco_ctx.cookies


# --- regression: context.new_page() must take no arguments ------------------
# T-21 (#29) live validation on 2026-09-25 found this exact bug in production:
# fetch_with_browser passed user_agent=... into context.new_page(), but the
# real playwright.sync_api.BrowserContext.new_page() takes no arguments (the
# user agent is only configurable at new_context() time). The original
# FakeContext.new_page(self, **kwargs) silently accepted the invalid keyword
# and let this ship untested; FakeContext is now strict (see above) so this
# can't happen silently again.


def test_fetch_with_browser_context_new_page_takes_no_arguments(monkeypatch):
    install_fakes(monkeypatch)
    bs = scrape._BrowserState()
    bs.start()
    monkeypatch.setattr(scrape, "_browser_state", bs)
    monkeypatch.setattr(scrape.time, "sleep", lambda *_: None)

    html = scrape.fetch_with_browser("https://example.test/x", "pcgarage")

    assert html == "<html>ok</html>"
    ctx = bs.get_context("pcgarage")
    # The page came from the existing per-site context, not a fresh one.
    assert len(ctx.pages) == 1
    page = ctx.pages[0]
    assert page.context is ctx
    # The context, not the page, carries the configured user agent.
    assert ctx.user_agent == scrape.HEADERS["User-Agent"]
    assert page.closed is True


# --- 5: every fetch attempt closes its page in finally -----------------------


def test_fetch_with_browser_closes_page_on_success(monkeypatch):
    install_fakes(monkeypatch)
    bs = scrape._BrowserState()
    bs.start()
    monkeypatch.setattr(scrape, "_browser_state", bs)
    monkeypatch.setattr(scrape.time, "sleep", lambda *_: None)

    html = scrape.fetch_with_browser("https://example.test/x", "pcgarage")

    assert html == "<html>ok</html>"
    ctx = bs.get_context("pcgarage")
    assert len(ctx.pages) == 1
    assert ctx.pages[0].closed is True


def test_fetch_with_browser_closes_page_when_goto_raises(monkeypatch):
    install_fakes(monkeypatch)
    bs = scrape._BrowserState()
    bs.start()
    monkeypatch.setattr(scrape, "_browser_state", bs)
    monkeypatch.setattr(scrape.time, "sleep", lambda *_: None)

    ctx = bs.get_context("pcgarage")
    original_new_page = ctx.new_page

    def new_page_with_goto_error(**kwargs):
        page = original_new_page(**kwargs)
        page.goto_error = RuntimeError("navigation failed")
        return page

    monkeypatch.setattr(ctx, "new_page", new_page_with_goto_error)

    html = scrape.fetch_with_browser("https://example.test/x", "pcgarage")

    assert html is None
    assert len(ctx.pages) == 1
    assert ctx.pages[0].closed is True


def test_fetch_with_browser_closes_page_when_content_raises(monkeypatch):
    install_fakes(monkeypatch)
    bs = scrape._BrowserState()
    bs.start()
    monkeypatch.setattr(scrape, "_browser_state", bs)
    monkeypatch.setattr(scrape.time, "sleep", lambda *_: None)

    ctx = bs.get_context("pcgarage")
    original_new_page = ctx.new_page

    def new_page_with_content_error(**kwargs):
        page = original_new_page(**kwargs)
        page.content_results = [RuntimeError("content failed")]
        return page

    monkeypatch.setattr(ctx, "new_page", new_page_with_content_error)

    html = scrape.fetch_with_browser("https://example.test/x", "pcgarage")

    assert html is None
    assert len(ctx.pages) == 1
    assert ctx.pages[0].closed is True


def test_fetch_with_browser_closes_page_when_challenge_polling_raises(monkeypatch):
    install_fakes(monkeypatch)
    bs = scrape._BrowserState()
    bs.start()
    monkeypatch.setattr(scrape, "_browser_state", bs)
    monkeypatch.setattr(scrape.time, "sleep", lambda *_: None)

    ctx = bs.get_context("pcgarage")
    original_new_page = ctx.new_page

    def new_page_with_polling_error(**kwargs):
        page = original_new_page(**kwargs)
        # First content() call returns a challenge page (triggers the poll
        # loop); the second raises mid-poll.
        page.content_results = [
            "<html>Just a moment...</html>",
            RuntimeError("poll failed"),
        ]
        return page

    monkeypatch.setattr(ctx, "new_page", new_page_with_polling_error)

    html = scrape.fetch_with_browser("https://example.test/x", "pcgarage")

    assert html is None
    assert len(ctx.pages) == 1
    assert ctx.pages[0].closed is True


# --- 6/7: scraper-level retry resets only the failed site's context ---------


def test_scraper_level_retry_resets_only_failed_site_context(monkeypatch):
    install_fakes(monkeypatch)
    monkeypatch.setattr(scrape.time, "sleep", lambda *_: None)
    bs = scrape._BrowserState()
    bs.start()
    monkeypatch.setattr(scrape, "_browser_state", bs)

    pcgarage_before = bs.get_context("pcgarage")
    flanco_before = bs.get_context("flanco")

    calls = {"n": 0}

    def flaky(query):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return ["ok"]

    wrapped = scrape.with_retry(flaky, site_name="pcgarage")
    result = wrapped("query")

    assert result == ["ok"]
    assert calls["n"] == 2
    # Failed site: old context discarded and closed, a fresh one created.
    assert pcgarage_before.closed is True
    assert bs.get_context("pcgarage") is not pcgarage_before
    # Untouched site: same context instance, never closed.
    assert bs.get_context("flanco") is flanco_before
    assert flanco_before.closed is False


def test_scraper_level_retry_without_site_name_does_not_touch_contexts(monkeypatch):
    # emag/altex pass no site_name to with_retry since they never call
    # fetch_with_browser; retrying them must not touch any browser context.
    install_fakes(monkeypatch)
    monkeypatch.setattr(scrape.time, "sleep", lambda *_: None)
    bs = scrape._BrowserState()
    bs.start()
    monkeypatch.setattr(scrape, "_browser_state", bs)

    pcgarage_before = bs.get_context("pcgarage")

    calls = {"n": 0}

    def flaky(query):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return ["ok"]

    wrapped = scrape.with_retry(flaky)
    result = wrapped("query")

    assert result == ["ok"]
    assert bs.get_context("pcgarage") is pcgarage_before
    assert pcgarage_before.closed is False


# --- 8: run-level cleanup closes everything, even on failure ----------------


def test_browser_state_close_closes_all_contexts_and_browser(monkeypatch):
    fake_cm = install_fakes(monkeypatch)
    bs = scrape._BrowserState()
    bs.start()
    pcgarage_ctx = bs.get_context("pcgarage")
    flanco_ctx = bs.get_context("flanco")
    browser = fake_cm.pw.chromium.last_browser

    bs.close()

    assert pcgarage_ctx.closed is True
    assert flanco_ctx.closed is True
    assert browser.closed is True
    assert fake_cm.exited is True


def test_main_closes_browser_state_even_when_run_raises(monkeypatch, tmp_path):
    watchlist_file = tmp_path / "watchlist.json"
    watchlist_file.write_text(
        __import__("json").dumps([{"site": "emag", "query": "q"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(scrape, "WATCHLIST_FILE", watchlist_file)
    monkeypatch.setattr(scrape, "HISTORY_FILE", tmp_path / "price_history.json")
    monkeypatch.setattr(scrape, "ALERTS_FILE", tmp_path / "alerts.json")
    monkeypatch.setattr(scrape, "SCRAPE_HEALTH_FILE", tmp_path / "scrape_health.jsonl")
    monkeypatch.setattr(
        scrape, "SCRAPE_HEALTH_ALERTS_FILE", tmp_path / "scrape_health_alerts.json"
    )
    monkeypatch.setattr(scrape, "DB_FILE", tmp_path / "price_history.db")
    monkeypatch.setattr(scrape.time, "sleep", lambda *_: None)

    calls = {"close": 0}

    class TrackingBrowserState:
        def close(self):
            calls["close"] += 1

    monkeypatch.setattr(scrape, "_browser_state", TrackingBrowserState())

    def boom(*args, **kwargs):
        raise RuntimeError("save failed")

    monkeypatch.setattr(scrape, "save_history", boom)

    with pytest.raises(RuntimeError, match="save failed"):
        scrape.main()

    # This watchlist is emag-only, so the browser is never even started
    # (lazy start on first get_context() call) — the point of this test is
    # that close() still runs unconditionally in finally, whether or not
    # anything was ever started.
    assert calls["close"] == 1


# --- 9: T-22 (#31) bounded challenge wait replaces the old sleep-poll loop --
# fetch_with_browser() used to poll page.content() on a fixed 1-second
# interval until is_challenge_page() went false or a deadline passed. It now
# calls _wait_out_challenge(), which waits on a Playwright locator for the
# actual challenge markup to disappear -- these tests exercise that helper
# directly, then confirm fetch_with_browser()'s challenge path never calls
# time.sleep() at all.


def test_wait_out_challenge_returns_immediately_when_no_challenge_element():
    page = FakePage(context=None)
    page.locator_behavior = "absent"

    scrape._wait_out_challenge(page, timeout_ms=5000)

    assert page.locator_calls == [scrape.CLOUDFLARE_CHALLENGE_SELECTOR]
    assert page.last_locator.wait_for_calls == [("visible", 1500)]


def test_wait_out_challenge_waits_for_hidden_once_challenge_appears():
    page = FakePage(context=None)
    page.locator_behavior = "resolves"

    scrape._wait_out_challenge(page, timeout_ms=5000)

    assert page.last_locator.wait_for_calls == [
        ("visible", 1500),
        ("hidden", 5000),
    ]


def test_wait_out_challenge_swallows_timeout_when_challenge_never_clears():
    page = FakePage(context=None)
    page.locator_behavior = "never_clears"

    # Must not raise -- the caller's own is_challenge_page() re-check on the
    # re-fetched HTML is the authoritative pass/fail signal, not this wait.
    scrape._wait_out_challenge(page, timeout_ms=5000)

    assert page.last_locator.wait_for_calls == [
        ("visible", 1500),
        ("hidden", 5000),
    ]


def test_wait_out_challenge_swallows_frame_detached_error():
    page = FakePage(context=None)
    page.locator_behavior = "frame_detached"

    # A challenge clearing by navigating the page away entirely raises
    # Playwright's generic Error, not TimeoutError -- also must not raise.
    scrape._wait_out_challenge(page, timeout_ms=5000)


def test_fetch_with_browser_clears_challenge_without_sleeping(monkeypatch):
    install_fakes(monkeypatch)
    bs = scrape._BrowserState()
    bs.start()
    monkeypatch.setattr(scrape, "_browser_state", bs)

    def sleep_should_not_be_called(*_):
        raise AssertionError(
            "fetch_with_browser's challenge path must use the bounded "
            "locator wait, not time.sleep()"
        )

    monkeypatch.setattr(scrape.time, "sleep", sleep_should_not_be_called)

    ctx = bs.get_context("pcgarage")
    original_new_page = ctx.new_page

    def new_page_with_clearing_challenge(**kwargs):
        page = original_new_page(**kwargs)
        page.content_results = [
            "<html>Just a moment...</html>",
            "<html>real listing</html>",
        ]
        page.locator_behavior = "resolves"
        return page

    monkeypatch.setattr(ctx, "new_page", new_page_with_clearing_challenge)

    html = scrape.fetch_with_browser("https://example.test/x", "pcgarage")

    assert html == "<html>real listing</html>"


def test_fetch_with_browser_still_challenge_after_bounded_wait_times_out(
    monkeypatch,
):
    install_fakes(monkeypatch)
    bs = scrape._BrowserState()
    bs.start()
    monkeypatch.setattr(scrape, "_browser_state", bs)
    monkeypatch.setattr(scrape.time, "sleep", lambda *_: None)

    ctx = bs.get_context("pcgarage")
    original_new_page = ctx.new_page

    def new_page_with_stuck_challenge(**kwargs):
        page = original_new_page(**kwargs)
        page.content_results = [
            "<html>Just a moment...</html>",
            "<html>Just a moment...</html>",
        ]
        page.locator_behavior = "never_clears"
        return page

    monkeypatch.setattr(ctx, "new_page", new_page_with_stuck_challenge)

    html = scrape.fetch_with_browser("https://example.test/x", "pcgarage")

    # Still blocked after the bounded wait -- caller reports it clearly and
    # continues (no hang, no crash), same as the old sleep-poll's timeout path.
    assert html is None
