---
name: run-tests
description: Run the pytest suite under tests/ and report real pass/fail output. Reports "no tests collected" plainly rather than treating an empty run as a pass.
allowed-tools: Bash, Read, Glob
---

# Run Tests

Run `python -m pytest -v` from the repo root and report the actual output.

## Handling an empty or missing suite

`tests/` exists and holds a large suite (`pytest --collect-only` reports several
hundred tests). If pytest ever reports "no tests ran" / exit code 5, that is a
**factual state to report**, not a failure to paper over and not a success to
claim: say "pytest collected no tests" and check `pytest.ini` (`testpaths`) and
the working directory rather than reporting `run-tests` as passing.

## Procedure

1. Confirm `pytest` is importable (`python -c "import pytest"`); if not,
   report that `pip install -r requirements.txt` needs to run first rather
   than silently failing.
2. Run `python -m pytest -v` and capture full output.
3. Report: number collected, passed, failed, skipped -- verbatim from
   pytest's own summary line, not a paraphrase.
4. On failure, show the actual assertion/traceback for each failing test,
   not just "N tests failed."
