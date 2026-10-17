"""#74: persist_history.py merges price history by data, never by text.

Every test builds a throwaway bare "origin" plus clones under tmp_path and
drives real git against them -- no network, no hooks. A clone sitting on an
older commit stands in for a queued monitor run that scraped from a stale base.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import persist_history
import pytest

HISTORY = Path("data") / "price_history.json"
KEY = "https://emag.ro/laptop/pd/ABC123"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _obs(observed_at: str, price: float) -> dict[str, Any]:
    return {
        "date": observed_at[:10],
        "observed_at": observed_at,
        "price": price,
        "stock_status": "in_stock",
    }


def _doc(*observations: dict[str, Any]) -> dict[str, Any]:
    prices = [o["price"] for o in observations]
    return {
        "schema_version": 1,
        "products": {
            KEY: {
                "title": "Laptop",
                "site": "emag",
                "history": list(observations),
                "all_time_low": min(prices),
                "all_time_high": max(prices),
                "first_seen": observations[0]["date"],
            }
        },
    }


def _write(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def _origin_doc(origin: Path) -> dict[str, Any]:
    return json.loads(_git(origin, "show", f"master:{HISTORY.as_posix()}"))


def _history(doc: dict[str, Any]) -> list[str]:
    return [h["observed_at"] for h in doc["products"][KEY]["history"]]


BASE_OBS = _obs("2026-10-14T07:00:00+00:00", 2999.0)
A_OBS = _obs("2026-10-14T09:00:00+00:00", 2899.0)
B_OBS = _obs("2026-10-14T16:57:00+00:00", 2799.0)


@pytest.fixture
def repos(tmp_path: Path) -> dict[str, Any]:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/master")
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", str(seed))
    _write(seed / HISTORY, _doc(BASE_OBS))
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "base")
    _git(seed, "push", "-q", str(origin), "HEAD:master")
    clones = {}
    for name in ("a", "b"):
        _git(tmp_path, "clone", "-q", str(origin), name)
        clones[name] = tmp_path / name
    return {
        "origin": origin,
        "a": clones["a"],
        "b": clones["b"],
        "base": _git(origin, "rev-parse", "master"),
        "incoming": tmp_path / "incoming",
    }


def _push_run_a(repos: dict[str, Any]) -> None:
    _write(repos["a"] / HISTORY, _doc(BASE_OBS, A_OBS))
    _git(repos["a"], "commit", "-q", "-am", "run A")
    _git(repos["a"], "push", "-q", "origin", "HEAD:master")


def _push_extra_from_a(repos: dict[str, Any], observed_at: str) -> None:
    """Simulate another run landing on origin while B is mid-persist."""
    _git(repos["a"], "pull", "-q", "--ff-only", "origin", "master")
    path = repos["a"] / HISTORY
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["products"][KEY]["history"].append(_obs(observed_at, 2500.0))
    _write(path, doc)
    _git(repos["a"], "commit", "-q", "-am", f"run at {observed_at}")
    _git(repos["a"], "push", "-q", "origin", "HEAD:master")


def _persist_run_b(
    repos: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    base: str | None = None,
    incoming: dict[str, Any] | str | None = None,
    **kwargs: Any,
) -> int:
    path = repos["incoming"] / "price_history.json"
    if isinstance(incoming, str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(incoming, encoding="utf-8")
    else:
        _write(path, incoming if incoming is not None else _doc(BASE_OBS, B_OBS))
    monkeypatch.chdir(repos["b"])
    argv = ["--incoming", str(repos["incoming"])]
    argv += ["--base", repos["base"] if base is None else base]
    return persist_history.main([*argv, "--require-base"], **kwargs)


def test_stale_base_persist_keeps_both_runs(repos, monkeypatch):
    _push_run_a(repos)

    exit_code = _persist_run_b(repos, monkeypatch, sleep_fn=lambda _: None)

    assert exit_code == 0
    assert _history(_origin_doc(repos["origin"])) == [
        BASE_OBS["observed_at"],
        A_OBS["observed_at"],
        B_OBS["observed_at"],
    ]


def test_rejected_first_push_is_retried_from_a_fresh_fetch(repos, monkeypatch):
    sleeps: list[float] = []
    landed: list[str] = []

    def other_run_lands_once() -> None:
        if not landed:
            landed.append("2026-10-14T16:58:00+00:00")
            _push_extra_from_a(repos, landed[0])

    exit_code = _persist_run_b(
        repos, monkeypatch, sleep_fn=sleeps.append, before_push=other_run_lands_once
    )

    assert exit_code == 0
    assert sleeps == [5]
    assert _history(_origin_doc(repos["origin"])) == [
        BASE_OBS["observed_at"],
        B_OBS["observed_at"],
        landed[0],
    ]


def test_nothing_to_commit_exits_zero_without_a_new_commit(repos, monkeypatch):
    before = _git(repos["origin"], "rev-parse", "master")

    exit_code = _persist_run_b(repos, monkeypatch, incoming=_doc(BASE_OBS))

    assert exit_code == 0
    assert _git(repos["origin"], "rev-parse", "master") == before


def test_corrupt_incoming_exits_2_without_retry_or_push(repos, monkeypatch):
    before = _git(repos["origin"], "rev-parse", "master")
    sleeps: list[float] = []

    exit_code = _persist_run_b(
        repos, monkeypatch, incoming="{not json", sleep_fn=sleeps.append
    )

    assert exit_code == 2
    assert sleeps == []
    assert _git(repos["origin"], "rev-parse", "master") == before


@pytest.mark.parametrize("bad_base", ["", "   ", "HEAD", "abc123", "Z" * 40])
def test_invalid_or_empty_base_exits_2_before_any_git_call(
    repos, monkeypatch, bad_base
):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        persist_history, "_git", lambda *args, **kwargs: calls.append(args)
    )

    exit_code = _persist_run_b(repos, monkeypatch, base=bad_base)

    assert exit_code == 2
    assert calls == []


@pytest.mark.parametrize("padding", ["\r\n", "\n", "  ", "\t"])
def test_base_with_surrounding_whitespace_or_crlf_is_accepted(
    repos, monkeypatch, padding
):
    _push_run_a(repos)

    exit_code = _persist_run_b(
        repos, monkeypatch, base=f"{padding}{repos['base']}{padding}"
    )

    assert exit_code == 0
    assert B_OBS["observed_at"] in _history(_origin_doc(repos["origin"]))


def test_every_push_rejected_exits_1_with_backoff(repos, monkeypatch):
    sleeps: list[float] = []
    counter = iter(range(10))

    def another_run_lands() -> None:
        _push_extra_from_a(repos, f"2026-10-14T17:0{next(counter)}:00+00:00")

    exit_code = _persist_run_b(
        repos, monkeypatch, sleep_fn=sleeps.append, before_push=another_run_lands
    )

    assert exit_code == 1
    assert sleeps == [5, 10]
    assert B_OBS["observed_at"] not in _history(_origin_doc(repos["origin"]))
