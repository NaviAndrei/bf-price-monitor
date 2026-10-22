"""Guards for the Claude Code harness config under .claude/ (settings and docs).

These catch the mistakes that cost real time in past sessions: a hook that the
harness kills before its own work finishes, hook paths that depend on the
working directory, and instructions that send Claude to the system Python.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))

_spec = importlib.util.spec_from_file_location(
    "verify_completion", ROOT / ".claude" / "hooks" / "verify_completion.py"
)
verify_completion = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_completion)


def _hook_entries():
    for event, groups in SETTINGS["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                yield event, hook


def test_every_hook_script_exists_and_uses_the_project_dir_variable():
    for event, hook in _hook_entries():
        command = hook["command"]
        assert "$CLAUDE_PROJECT_DIR" in command, (event, command)
        match = re.search(r"\.claude/hooks/(\w+\.py)", command)
        assert match, command
        assert (ROOT / ".claude" / "hooks" / match.group(1)).is_file(), command


def test_the_background_test_hook_outlives_pytest_own_timeout():
    tests_hooks = [
        hook
        for event, hook in _hook_entries()
        if event == "Stop" and hook["command"].rstrip().endswith("tests")
    ]
    assert len(tests_hooks) == 1
    hook = tests_hooks[0]
    assert hook.get("asyncRewake") is True
    assert hook["timeout"] > verify_completion.PYTEST_TIMEOUT_SECONDS


def test_the_blocking_stop_hook_stays_fast():
    progress = [
        hook
        for event, hook in _hook_entries()
        if event == "Stop" and hook["command"].rstrip().endswith("progress")
    ]
    assert len(progress) == 1
    assert progress[0]["timeout"] <= 60
    assert not progress[0].get("asyncRewake")


def test_pushes_and_pulls_ask_and_dangerous_git_is_denied_in_both_shells():
    permissions = SETTINGS["permissions"]
    for tool in ("Bash", "PowerShell"):
        assert f"{tool}(git push *)" in permissions["ask"]
        assert f"{tool}(git pull *)" in permissions["ask"]
        denied = " ".join(r for r in permissions["deny"] if r.startswith(tool + "("))
        for risky in (
            "git reset --hard",
            "git push --force",
            "git push -f",
            "git rebase",
        ):
            assert risky in denied, (tool, risky)


def test_venv_guard_runs_next_to_branch_guard():
    pre = SETTINGS["hooks"]["PreToolUse"]
    shell_group = next(g for g in pre if "Bash" in g["matcher"])
    commands = " ".join(h["command"] for h in shell_group["hooks"])
    assert "branch_guard.py" in commands and "venv_guard.py" in commands


DOCS = [
    ROOT / "CLAUDE.md",
    *(ROOT / ".claude").glob("rules/*.md"),
    *(ROOT / ".claude").glob("skills/**/*.md"),
    *(ROOT / ".claude").glob("commands/*.md"),
    *(ROOT / ".claude").glob("agents/*.md"),
]
BARE = re.compile(
    r"(?<![\w./\\-])(python -m pytest|python -m mypy|python scripts/)"
    r"|(^\s*|`)pytest (-|tests)"
)


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_instructions_never_send_claude_to_the_system_python(path):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if "plain `python -m pytest`" in line:  # the sentence that explains the trap
            continue
        assert not BARE.search(line), f"{path.name}:{number}: {line.strip()}"


def test_browser_tests_are_opt_in_and_the_marker_is_registered():
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_config = config["tool"]["pytest"]["ini_options"]
    assert "-m 'not browser'" in pytest_config["addopts"]
    assert any(marker.startswith("browser:") for marker in pytest_config["markers"])


def test_qa_verifier_is_read_only():
    text = (ROOT / ".claude" / "agents" / "qa-verifier.md").read_text(encoding="utf-8")
    frontmatter = text.split("---", 2)[1]
    assert "disallowedTools: Edit, Write" in frontmatter
    tools = next(line for line in frontmatter.splitlines() if line.startswith("tools:"))
    assert "Edit" not in tools and "Write" not in tools


def test_manual_qa_skill_is_only_run_by_hand():
    text = (ROOT / ".claude" / "skills" / "manual-qa" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "disable-model-invocation: true" in text
