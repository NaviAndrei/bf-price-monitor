---
paths:
  - "tests/**"
---

# Testing rules

- **Pre-commit verification:** Run `ruff check .` and `pytest -v`. Never commit code that breaks existing tests or drops test coverage.
- **Ruff auto-fix can hide broken edits:** The ruff auto-fix hook silently strips import-only edits added before their usage lands in the same session. Verify with a real test run, not just a clean diff.
- **No live network in tests:** mock `requests`, Playwright, Hugging Face/Ollama and Telegram calls. Retailer HTML comes from `tests/fixtures/`, never from a live fetch.
