---
paths:
  - "tests/**"
---

# Testing rules

- **Pre-commit verification:** Run `ruff check .` and `.venv/Scripts/python.exe -m pytest -v`. Never commit code that breaks existing tests or drops test coverage.
- **Ruff auto-fix can hide broken edits:** The ruff auto-fix hook silently strips import-only edits added before their usage lands in the same session. Verify with a real test run, not just a clean diff.
- **No live network in tests:** mock `requests`, Playwright, Hugging Face/Ollama and Telegram calls. Retailer HTML comes from `tests/fixtures/`, never from a live fetch.
- **Three layers, one gate:** unit and contract tests run in the default suite. Browser tests carry `@pytest.mark.browser` and are excluded by default (`addopts` in `pyproject.toml`). Run them with `.venv/Scripts/python.exe -m pytest -m browser` when a change touches `bf_price_monitor/dashboard/static/` or the dashboard server. Manual checks stay in a runbook and run through the `manual-qa` skill.
- **Source-text tests are not UI tests:** `tests/dashboard/test_server_ui.py` checks the page's files, not what a browser does. A change to the page's behaviour needs a browser test or a manual step.
- **Prove, do not assert:** a check counts only when a run showed it. Quote the pytest summary line or the command output in the report.
- **`tmp_path` is inside the repo:** locally, `--basetemp=.pytest_tmp` puts it under this git checkout, so git walks up into the repo's history. A test that needs a folder outside any repo (for example `test_not_a_git_repo_is_ignored`) must use `tempfile.TemporaryDirectory()`.
- **Independent check before "done":** for a change to a dashboard, pipeline stage, or hook, run the `qa-verifier` agent against the spec. It is read-only and reads the spec fresh.
