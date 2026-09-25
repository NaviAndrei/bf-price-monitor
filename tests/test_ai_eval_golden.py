"""Golden adversarial dataset runner for T-24 (#32).

Loads tests/fixtures/ai_eval_golden.json and drives every case through the
real get_analysis() code path (network call mocked, everything downstream of
it -- extract_json, AIDealEvaluation, enforce_deterministic_invariant -- is
real). Confirms no case crashes, malformed/wrong-type cases hit the
deterministic fallback, and the final verdict/is_recommended always matches
each case's ground truth (which is itself derived from evaluate_omnibus_rule
on the case's rule_inputs, never from the fabricated LLM response). Also
reports precision/recall/F1 on the is_recommended classification and gates
on the threshold this run actually achieves.
"""

import json
from pathlib import Path

import pytest
from analyze import evaluate_omnibus_rule, get_analysis

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ai_eval_golden.json"
GOLDEN = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
CASES = GOLDEN["cases"]

# The deterministic invariant (T-23 / #30) makes final is_recommended a pure
# function of rule_inputs, regardless of what the fabricated LLM response
# claims -- so a correctly wired pipeline should score exactly 1.0 on this
# adversarial set. This is the threshold actually achieved by this run (see
# test_golden_dataset_meets_f1_threshold below); it is not a soft accuracy
# target the way a real LLM-quality benchmark would have; a drop below it
# means the invariant broke, not that the model got worse.
F1_THRESHOLD = 1.0


@pytest.fixture(autouse=True)
def redirect_ai_audit_log(monkeypatch, tmp_path):
    monkeypatch.setattr("analyze.AI_AUDIT_FILE", tmp_path / "ai_audit.jsonl")


def _run_case(monkeypatch, case: dict) -> dict:
    metrics = evaluate_omnibus_rule(**case["rule_inputs"])
    monkeypatch.setattr(
        "analyze.ask_hf",
        lambda client, prompt, _resp=case["llm_raw_response"]: (_resp, None),
    )
    return get_analysis(
        client=None,
        prompt=f"fixture:{case['id']}",
        rule_verdict=metrics["rule_verdict"],
        thirty_day_low=case["rule_inputs"]["thirty_day_low"],
    )


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_case_does_not_crash_and_matches_ground_truth(monkeypatch, case):
    result = _run_case(monkeypatch, case)
    assert result["verdict"] == case["ground_truth"]["verdict"]
    assert result["is_recommended"] == case["ground_truth"]["is_recommended"]


@pytest.mark.parametrize(
    "case",
    [c for c in CASES if c["category"] in ("malformed_json", "wrong_types")],
    ids=[c["id"] for c in CASES if c["category"] in ("malformed_json", "wrong_types")],
)
def test_golden_case_malformed_or_wrong_type_hits_fallback(monkeypatch, case):
    metrics = evaluate_omnibus_rule(**case["rule_inputs"])
    monkeypatch.setattr(
        "analyze.ask_hf",
        lambda client, prompt, _resp=case["llm_raw_response"]: (_resp, None),
    )
    result = get_analysis(
        client=None,
        prompt=f"fixture:{case['id']}",
        rule_verdict=metrics["rule_verdict"],
        thirty_day_low=case["rule_inputs"]["thirty_day_low"],
    )
    # default_analysis() always sets verdict_score to 8 (GENUINE_DEAL) or 4
    # (anything else) -- a value the fabricated fixtures deliberately never
    # use, so matching it is a reliable signal the fallback path fired
    # rather than the fixture's own (malformed/wrong-typed) score somehow
    # slipping through.
    expected_score = 8 if metrics["rule_verdict"] == "GENUINE_DEAL" else 4
    assert result["verdict_score"] == expected_score


def test_golden_dataset_meets_f1_threshold(monkeypatch):
    tp = fp = fn = tn = 0
    for case in CASES:
        result = _run_case(monkeypatch, case)
        predicted = result["is_recommended"]
        actual = case["ground_truth"]["is_recommended"]
        if predicted and actual:
            tp += 1
        elif predicted and not actual:
            fp += 1
        elif not predicted and actual:
            fn += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    print(
        f"\nGolden adversarial set ({len(CASES)} cases): "
        f"tp={tp} fp={fp} fn={fn} tn={tn} "
        f"precision={precision:.3f} recall={recall:.3f} f1={f1:.3f}"
    )
    assert f1 >= F1_THRESHOLD, (
        f"F1 {f1:.3f} dropped below the {F1_THRESHOLD} threshold this run "
        f"achieved -- the deterministic invariant is expected to make this "
        f"set 100% separable, so a drop here means enforce_deterministic_"
        f"invariant() or the schema boundary let an adversarial response "
        f"through, not that the fixtures need loosening."
    )
