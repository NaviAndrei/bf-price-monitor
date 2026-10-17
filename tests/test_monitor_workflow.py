"""#74: contract tests for monitor.yml's checkout refs and persist step.

Sliced by job from the raw text (as test_runtime_state.py does) because the
project declares no YAML parser dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "monitor.yml"
PERSIST_SCRIPT = REPO / "scripts" / "persist_history.py"

# Captured from monitor.yml before #74; this change must not touch them.
CONCURRENCY_BLOCK = "concurrency:\n  group: monitor\n  cancel-in-progress: false\n"
PINNED_USES = [
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7",
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7",
    "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
]
FORBIDDEN_GIT = [
    r"reset\s+--hard",
    r"\brebase\b",
    r"--force\b",
    r"push\s+-f\b",
    r"checkout\s+-b",
    r"switch\s+-c",
    r"\+refs/",
]


@pytest.fixture(scope="module")
def text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def jobs(text: str) -> dict[str, str]:
    body = text[text.index("\njobs:\n") :]
    headers = list(re.finditer(r"^  ([a-z][a-z-]*):\n", body, re.MULTILINE))
    return {
        m.group(1): body[
            m.end() : (headers[i + 1].start() if i + 1 < len(headers) else None)
        ]
        for i, m in enumerate(headers)
    }


def _step(job_text: str, name: str) -> str:
    start = job_text.index(f"- name: {name}")
    nxt = job_text.find("\n      - ", start + 1)
    return job_text[start : nxt if nxt != -1 else None]


def test_ref_master_on_exactly_the_scrape_and_persist_checkouts(jobs):
    refs = {name: body.count("ref: master") for name, body in jobs.items()}
    assert refs == {
        "gate": 0,
        "golden-gate": 0,
        "scrape-analyze-notify": 1,
        "persist": 1,
    }


def test_base_commit_is_recorded_with_ascii_add_content(jobs):
    scrape_job = jobs["scrape-analyze-notify"]
    step = _step(scrape_job, "Record base commit")
    assert "id: base" in step
    assert "Add-Content -Path $env:GITHUB_OUTPUT" in step
    assert "-Encoding ascii" in step
    assert ">>" not in step
    assert "base_sha: ${{ steps.base.outputs.sha }}" in scrape_job


def test_watchlist_is_neither_uploaded_nor_persisted(jobs):
    upload = _step(jobs["scrape-analyze-notify"], "Upload updated data files")
    after = upload[upload.index("path: |") :].splitlines()[1:]
    paths = []
    for line in after:  # the block scalar ends at the first shallower line
        if not line.startswith(" " * 12):
            break
        paths.append(line.strip())
    assert paths == [
        "data/price_history.json",
        "data/scrape_health.jsonl",
    ]
    assert "watchlist.json" not in jobs["persist"]


def test_persist_downloads_outside_the_work_tree_and_calls_the_script(jobs):
    persist = jobs["persist"]
    assert "path: ${{ runner.temp }}/price-data" in persist
    step = _step(persist, "Persist price history")
    assert (
        '"C:\\Program Files\\Python312\\python.exe" scripts/persist_history.py' in step
    )
    assert "--require-base" in step
    assert "BASE_SHA: ${{ needs.scrape-analyze-notify.outputs.base_sha }}" in step
    assert "git add" not in persist
    assert "git push" not in persist


@pytest.mark.parametrize("path", [WORKFLOW, PERSIST_SCRIPT], ids=lambda p: p.name)
def test_no_history_rewriting_git_verbs(path):
    source = path.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_GIT:
        assert not re.search(pattern, source), f"{path.name} matches {pattern!r}"


def test_concurrency_block_unchanged(text):
    assert CONCURRENCY_BLOCK in text


def test_action_pins_unchanged(text):
    uses = re.findall(r"^\s*(?:- )?uses:\s*(\S+)", text, re.MULTILINE)
    assert uses == PINNED_USES
