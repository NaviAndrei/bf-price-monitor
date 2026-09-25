import json

import pytest
from analyze import (
    AIDealEvaluation,
    RawAlertCandidate,
    default_analysis,
    enforce_deterministic_invariant,
    evaluate_omnibus_rule,
    extract_json,
    get_analysis,
    validate_ai_evaluation,
)
from pydantic import ValidationError

VALID_AI_OUTPUT = {
    "verdict": "GENUINE_DEAL",
    "verdict_score": 9,
    "summary": "Merita cumparat, minim istoric.",
    "is_recommended": True,
}


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


def test_raw_alert_candidate_accepts_real_scrape_alert_shape():
    # Mirrors the dict scripts/scrape.py's alerts.append() writes to
    # data/alerts.json for a real price-change candidate.
    alert = {
        "title": "Laptop Lenovo V15 G4 AMN",
        "site": "emag",
        "query": "laptop lenovo v15",
        "url": "https://www.emag.ro/laptop-lenovo-v15/pd/ABC123/",
        "old_price": 2699.0,
        "new_price": 2499.99,
        "thirty_day_low": 2599.0,
        "history_days": 45,
        "reference_price": None,
        "stock_status": "in_stock",
        "seller": None,
        "is_marketplace": None,
        "all_time_low": 2499.99,
        "all_time_high": 2999.0,
    }
    validated = RawAlertCandidate.model_validate(alert)
    assert validated.new_price == 2499.99


def test_raw_alert_candidate_rejects_missing_required_field():
    alert = {
        "title": "Laptop Lenovo V15 G4 AMN",
        "site": "emag",
        "query": "laptop lenovo v15",
        "url": "https://www.emag.ro/laptop-lenovo-v15/pd/ABC123/",
        "old_price": 2699.0,
        # "new_price" omitted
        "thirty_day_low": 2599.0,
        "history_days": 45,
        "stock_status": "in_stock",
        "all_time_low": 2499.99,
        "all_time_high": 2999.0,
    }
    with pytest.raises(ValidationError):
        RawAlertCandidate.model_validate(alert)


def test_ai_deal_evaluation_accepts_valid_output():
    validated = AIDealEvaluation.model_validate(VALID_AI_OUTPUT)
    assert validated.verdict == "GENUINE_DEAL"
    assert validated.verdict_score == 9
    assert validated.is_recommended is True


def test_ai_deal_evaluation_rejects_wrong_type_score():
    bad = {**VALID_AI_OUTPUT, "verdict_score": "nine"}
    with pytest.raises(ValidationError):
        AIDealEvaluation.model_validate(bad)


def test_ai_deal_evaluation_rejects_out_of_range_score():
    bad = {**VALID_AI_OUTPUT, "verdict_score": 15}
    with pytest.raises(ValidationError):
        AIDealEvaluation.model_validate(bad)


def test_ai_deal_evaluation_rejects_invalid_enum():
    bad = {**VALID_AI_OUTPUT, "verdict": "TOTALLY_MADE_UP"}
    with pytest.raises(ValidationError):
        AIDealEvaluation.model_validate(bad)


def test_ai_deal_evaluation_rejects_oversized_summary():
    bad = {**VALID_AI_OUTPUT, "summary": "x" * 501}
    with pytest.raises(ValidationError):
        AIDealEvaluation.model_validate(bad)


def test_ai_deal_evaluation_rejects_html_like_summary():
    # Prompt-injection guard (T-23): a model coaxed into echoing markup back
    # must not have that markup pass the validation boundary.
    bad = {**VALID_AI_OUTPUT, "summary": "Bun <script>alert(1)</script> de luat."}
    with pytest.raises(ValidationError):
        AIDealEvaluation.model_validate(bad)


def test_validate_ai_evaluation_logs_redacted_reason_and_returns_none(capsys):
    bad = {**VALID_AI_OUTPUT, "verdict_score": 999}
    result = validate_ai_evaluation(
        bad, provider="huggingface", model_name="test-model"
    )
    assert result is None
    captured = capsys.readouterr()
    assert "huggingface" in captured.err
    assert "test-model" in captured.err


def test_enforce_deterministic_invariant_overrides_is_recommended_true():
    # The LLM claims a genuine deal worth buying; the deterministic engine
    # disagrees (FALSE_DISCOUNT). The invariant must win.
    analysis = {
        "verdict": "GENUINE_DEAL",
        "verdict_score": 10,
        "summary": "Ofertă excelentă!",
        "is_recommended": True,
    }
    result = enforce_deterministic_invariant(analysis, "FALSE_DISCOUNT")
    assert result["verdict"] == "FALSE_DISCOUNT"
    assert result["is_recommended"] is False


def test_enforce_deterministic_invariant_overrides_normal_drop():
    analysis = {
        "verdict": "GENUINE_DEAL",
        "verdict_score": 8,
        "summary": "Merita cumparat.",
        "is_recommended": True,
    }
    result = enforce_deterministic_invariant(analysis, "NORMAL_DROP")
    assert result["verdict"] == "NORMAL_DROP"
    assert result["is_recommended"] is False


def test_enforce_deterministic_invariant_preserves_genuine_deal():
    analysis = {
        "verdict": "GENUINE_DEAL",
        "verdict_score": 9,
        "summary": "Merita cumparat.",
        "is_recommended": True,
    }
    result = enforce_deterministic_invariant(analysis, "GENUINE_DEAL")
    assert result["verdict"] == "GENUINE_DEAL"
    assert result["is_recommended"] is True


def test_default_analysis_never_recommends_a_non_genuine_verdict():
    for verdict in (
        "FALSE_DISCOUNT",
        "INFLATED_REFERENCE",
        "NORMAL_DROP",
        "INSUFFICIENT_HISTORY",
    ):
        result = default_analysis(verdict, thirty_day_low=90.0)
        assert result["is_recommended"] is False
        assert result["verdict"] == verdict


def test_get_analysis_falls_back_to_rule_formula_when_llm_response_malformed(
    monkeypatch,
):
    # Malformed JSON from both providers: HF raises, Ollama returns prose
    # instead of JSON. get_analysis must fall back to default_analysis(),
    # which encodes this repo's rule-based formula.
    monkeypatch.setattr(
        "analyze.ask_hf",
        lambda client, prompt: (_ for _ in ()).throw(RuntimeError("HF down")),
    )
    monkeypatch.setattr(
        "analyze.ask_ollama", lambda prompt: "Nu pot genera un JSON valid."
    )
    result = get_analysis(
        client=None,
        prompt="irrelevant",
        rule_verdict="NORMAL_DROP",
        thirty_day_low=90.0,
    )
    assert result == default_analysis("NORMAL_DROP", 90.0)
    assert result["is_recommended"] is False


def test_get_analysis_cannot_let_manipulated_llm_output_flip_verdict(monkeypatch):
    # Concrete proof for #30: a schema-valid but adversarial LLM response
    # claims GENUINE_DEAL/is_recommended=True while the deterministic rule
    # engine says FALSE_DISCOUNT (today's price never beat the 30-day low).
    # The final result must reflect the rule engine, not the model.
    manipulated_response = json.dumps(
        {
            "verdict": "GENUINE_DEAL",
            "verdict_score": 10,
            "summary": "Cea mai buna oferta din istorie, cumpara acum!",
            "is_recommended": True,
        }
    )
    monkeypatch.setattr("analyze.ask_hf", lambda client, prompt: manipulated_response)
    result = get_analysis(
        client=None,
        prompt="irrelevant",
        rule_verdict="FALSE_DISCOUNT",
        thirty_day_low=90.0,
    )
    assert result["verdict"] == "FALSE_DISCOUNT"
    assert result["is_recommended"] is False
    # The explanation fields are still the LLM's own — they're advisory only.
    assert result["verdict_score"] == 10
