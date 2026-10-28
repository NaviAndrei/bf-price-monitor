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
    "astral-sh/setup-uv@07a047e608db84b3d8c06cd9a7c7dcc9c355c18e",
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv@07a047e608db84b3d8c06cd9a7c7dcc9c355c18e",
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


def test_base_commit_is_written_to_an_ascii_file_not_a_job_output(jobs):
    # #76: GitHub skips any job output that may contain a secret (a short
    # secret can be a substring of a commit SHA), so the SHA travels as a
    # file inside the price-data artifact instead.
    step = _step(jobs["scrape-analyze-notify"], "Record base commit")
    assert "Set-Content" in step
    assert "data/base_sha.txt" in step
    assert "-Encoding ascii" in step
    assert "-NoNewline" in step
    assert "[0-9a-f]{40}" in step
    assert "GITHUB_OUTPUT" not in step
    assert ">>" not in step


def _job_outputs(job_text: str) -> dict[str, str]:
    """Key -> value of a job's `outputs:` block ({} if it has none)."""
    out: dict[str, str] = {}
    rows = job_text.splitlines()
    for i, row in enumerate(rows):
        if row == "    outputs:":
            for entry in rows[i + 1 :]:
                if not entry.startswith(" " * 6):
                    break  # the block ends at the first shallower line
                if entry.strip().startswith("#"):
                    continue
                key, _, value = entry.strip().partition(":")
                out[key] = value.strip()
            break
    return out


def test_no_job_output_carries_a_commit_sha(text, jobs):
    # Assert on the output key and the step reference, not on the substring
    # "base_sha": data/base_sha.txt and --base-file legitimately contain it.
    assert "steps.base.outputs" not in text
    outputs = {name: _job_outputs(body) for name, body in jobs.items()}
    assert outputs == {
        "gate": {"should_run": "${{ steps.decide.outputs.should_run }}"},
        "golden-gate": {},
        "scrape-analyze-notify": {
            "restore_status": "${{ steps.restore_state.outputs.state_status }}",
            "save_status": "${{ steps.save_state.outputs.state_status }}",
        },
        "persist": {},
    }
    assert all("base_sha" not in key for body in outputs.values() for key in body)


def test_outputs_parser_sees_a_base_sha_key_if_one_is_added():
    sample = (
        "    outputs:\n      restore_status: x\n      base_sha: ${{ y }}\n    env:\n"
    )
    assert set(_job_outputs(sample)) == {"restore_status", "base_sha"}


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
        "data/base_sha.txt",
    ]
    assert "retention-days: 3" in upload
    assert "watchlist.json" not in jobs["persist"]


def test_persist_downloads_outside_the_work_tree_and_calls_the_script(jobs):
    persist = jobs["persist"]
    assert "path: ${{ runner.temp }}/price-data" in persist
    step = _step(persist, "Persist price history")
    assert (
        '"C:\\Program Files\\Python312\\python.exe" scripts/persist_history.py' in step
    )
    assert "--require-base" in step
    assert r'--base-file "${{ runner.temp }}\price-data\base_sha.txt"' in step
    assert "BASE_SHA" not in step
    assert '--base "' not in step
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
