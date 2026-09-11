import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analyze import evaluate_omnibus_rule, extract_json  # noqa: E402


def test_evaluate_omnibus_rule_insufficient_history():
    # Fewer than 14 days of observed history — too short to trust any
    # baseline, regardless of how large the apparent discount is.
    result = evaluate_omnibus_rule(
        new_price=80.0,
        old_price=100.0,
        thirty_day_low=90.0,
        reference_price=None,
        history_days=10,
    )
    assert result["rule_verdict"] == "INSUFFICIENT_HISTORY"


def test_evaluate_omnibus_rule_false_discount():
    # new_price (95) is not below the 30-day low (90) — the retailer hiked
    # the price and is "discounting" back down to its own recent baseline.
    result = evaluate_omnibus_rule(
        new_price=95.0,
        old_price=120.0,
        thirty_day_low=90.0,
        reference_price=None,
        history_days=30,
    )
    assert result["rule_verdict"] == "FALSE_DISCOUNT"


def test_evaluate_omnibus_rule_inflated_reference():
    # new_price (85) beats the 30-day low (90), so it isn't a false discount,
    # but the struck-through reference_price (150) is more than 1.25x the
    # real recent low, so the advertised discount is misleading.
    result = evaluate_omnibus_rule(
        new_price=85.0,
        old_price=150.0,
        thirty_day_low=90.0,
        reference_price=150.0,
        history_days=30,
    )
    assert result["rule_verdict"] == "INFLATED_REFERENCE"


def test_evaluate_omnibus_rule_genuine_deal():
    # new_price (80) is a >=5% drop below the 30-day low (90), and the
    # reference_price is close enough to the real low to not be inflated.
    result = evaluate_omnibus_rule(
        new_price=80.0,
        old_price=100.0,
        thirty_day_low=90.0,
        reference_price=95.0,
        history_days=30,
    )
    assert result["rule_verdict"] == "GENUINE_DEAL"
    assert result["discount_vs_30d_pct"] == 11.11


def test_evaluate_omnibus_rule_normal_drop():
    # new_price (88) is below the 30-day low (90), but the drop is under 5%.
    result = evaluate_omnibus_rule(
        new_price=88.0,
        old_price=100.0,
        thirty_day_low=90.0,
        reference_price=None,
        history_days=30,
    )
    assert result["rule_verdict"] == "NORMAL_DROP"


def test_extract_json_plain():
    text = (
        '{"verdict": "GENUINE_DEAL", "verdict_score": 9, '
        '"summary": "Merita cumparat.", "is_recommended": true}'
    )
    parsed = extract_json(text)
    assert parsed == {
        "verdict": "GENUINE_DEAL",
        "verdict_score": 9,
        "summary": "Merita cumparat.",
        "is_recommended": True,
    }


def test_extract_json_markdown_fenced():
    text = (
        "Iată răspunsul:\n```json\n"
        '{"verdict": "NORMAL_DROP", "verdict_score": 4, '
        '"summary": "Reducere mica.", "is_recommended": false}'
        "\n```"
    )
    parsed = extract_json(text)
    assert parsed == {
        "verdict": "NORMAL_DROP",
        "verdict_score": 4,
        "summary": "Reducere mica.",
        "is_recommended": False,
    }


def test_extract_json_broken_text_returns_none():
    assert extract_json("Nu pot genera un JSON valid acum.") is None


def test_extract_json_missing_keys_returns_none():
    # Syntactically valid JSON but missing required keys (e.g. truncated
    # LLM output) must not be treated as a usable analysis.
    assert extract_json('{"verdict": "GENUINE_DEAL"}') is None


def test_extract_json_empty_returns_none():
    assert extract_json(None) is None
    assert extract_json("") is None
