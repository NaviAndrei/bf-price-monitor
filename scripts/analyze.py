# scripts/analyze.py
import hashlib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import requests
from huggingface_hub import InferenceClient
from pydantic import BaseModel, Field, ValidationError, field_validator

ALERTS_FILE = Path("data/alerts.json")
FORMATTED_FILE = Path("data/formatted_alerts.json")
AI_AUDIT_FILE = Path("data/ai_audit.jsonl")
# T-33 (#44): same handoff file scrape.py's Critical Selector Drift / dead-man
# alerts already use (T-09) — analyze.py has no TELEGRAM_BOT_TOKEN/CHAT_ID in
# monitor.yml (only HF_TOKEN), so it can't send Telegram directly either.
# Reusing this file lets notify.py's existing _send_health_alerts deliver it
# through the operational stream instead of a second bot integration.
SCRAPE_HEALTH_ALERTS_FILE = Path("data/scrape_health_alerts.json")


class RawAlertCandidate(BaseModel):
    """Structure-only validation boundary (T-01) for a price-change candidate
    as scrape.py's should_alert() writes it to data/alerts.json — before this
    module decides a rule_verdict, so it doesn't fit AlertDecision (which
    requires that verdict to already exist) or Watch (no watchlist fields are
    present at this point). Not one of the six canonical domain models; local
    to this boundary only.
    """

    title: str
    site: str
    query: str
    url: str
    old_price: float | None
    new_price: float
    thirty_day_low: float
    history_days: int
    reference_price: float | None = None
    stock_status: str
    seller: str | None = None
    is_marketplace: bool | None = None
    all_time_low: float
    all_time_high: float


_HTML_LIKE = re.compile(r"<[^>]*>|javascript:", re.IGNORECASE)

# The five rule_verdict values evaluate_omnibus_rule() can produce (T-23):
# kept as its own tuple so the LLM's "verdict" field can't validate to a
# value the deterministic engine could never emit.
_RULE_VERDICTS = (
    "GENUINE_DEAL",
    "FALSE_DISCOUNT",
    "INFLATED_REFERENCE",
    "NORMAL_DROP",
    "INSUFFICIENT_HISTORY",
)


class AIDealEvaluation(BaseModel):
    """Schema for the LLM's JSON response to build_omnibus_prompt() (T-23).

    verdict and is_recommended are validated here for shape only — even a
    schema-valid response never reaches the output as-is, since
    enforce_deterministic_invariant() always overwrites both from the
    rule engine's rule_verdict afterward (T-23 / #30). This model exists to
    reject malformed verdict_score/summary before they're trusted for
    display, not to let the model decide genuineness.
    """

    verdict: Literal[_RULE_VERDICTS]
    verdict_score: int = Field(ge=1, le=10)
    summary: str = Field(min_length=1, max_length=500)
    is_recommended: bool

    @field_validator("summary")
    @classmethod
    def reject_html_like_content(cls, v: str) -> str:
        if _HTML_LIKE.search(v):
            raise ValueError("summary contains HTML/script-like content")
        return v


HF_MODEL = os.getenv("HF_MODEL", "Qwen/Qwen2.5-Coder-32B-Instruct")
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen3:8b"

# Bumped whenever build_omnibus_prompt()'s instructions or requested fields
# change, so data/ai_audit.jsonl records can be grouped by which prompt
# shape actually produced them (T-24 / #32).
PROMPT_TEMPLATE_VERSION = "omnibus-v1"

# The JSON payload (verdict + verdict_score + summary + is_recommended) needs
# more room than a one-sentence verdict did; 250 keeps the Romanian summary
# from being cut off mid-string, which would otherwise fail JSON parsing.
LLM_MAX_TOKENS = 250

REQUIRED_ANALYSIS_KEYS = {"verdict", "verdict_score", "summary", "is_recommended"}


def evaluate_omnibus_rule(
    new_price: float,
    old_price: float,
    thirty_day_low: float,
    reference_price: float | None,
    history_days: int,
) -> dict:
    discount_vs_old_pct = round(((old_price - new_price) / old_price) * 100, 2)
    discount_vs_30d_pct = round(
        ((thirty_day_low - new_price) / thirty_day_low) * 100, 2
    )

    if history_days < 14:
        # Too little observed history to prove what the "real" baseline
        # price even is, regardless of how big the drop looks.
        rule_verdict = "INSUFFICIENT_HISTORY"
    elif new_price >= thirty_day_low:
        # The retailer's own 30-day low is at or below today's "discounted"
        # price — a hallmark of hiking the price shortly before "cutting" it.
        rule_verdict = "FALSE_DISCOUNT"
    elif reference_price is not None and reference_price > thirty_day_low * 1.25:
        # The struck-through reference price is inflated well past the real
        # recent low, making the advertised discount look bigger than it is.
        rule_verdict = "INFLATED_REFERENCE"
    elif discount_vs_30d_pct >= 5.0:
        rule_verdict = "GENUINE_DEAL"
    else:
        rule_verdict = "NORMAL_DROP"

    return {
        "discount_vs_old_pct": discount_vs_old_pct,
        "discount_vs_30d_pct": discount_vs_30d_pct,
        "rule_verdict": rule_verdict,
    }


def build_omnibus_prompt(alert: dict, metrics: dict) -> str:
    return (
        "Ești un auditor de protecție a consumatorului (Directiva Omnibus / OUG 58/2022).\n"
        f"Produs: {alert['title']} ({alert['site']}, Vânzător: {alert.get('seller') or 'Neverificat'})\n"
        f"Preț Nou: {alert['new_price']} RON | Preț Anterior: {alert['old_price']} RON\n"
        f"Cel mai mic preț din ultimele 30 zile: {alert.get('thirty_day_low')} RON\n"
        f"Preț de Referință (tăiat): {alert.get('reference_price') or 'N/A'} RON\n"
        f"Zile de istoric observate: {alert.get('history_days', 0)}\n"
        f"Verdict Matematic Calculat: {metrics['rule_verdict']}\n\n"
        "Returnează DOAR un JSON valid (fără text introductiv):\n"
        "{\n"
        f'  "verdict": "{metrics["rule_verdict"]}",\n'
        '  "verdict_score": <notă 1-10 în funcție de cât de atractivă și reală este oferta>,\n'
        '  "summary": "<1-2 propoziții în română: explică dacă merită cumpărat sau e o capcană de marketing>",\n'
        '  "is_recommended": <true dacă este GENUINE_DEAL și merită cumpărat, altfel false>\n'
        "}"
    )


def ask_hf(client: InferenceClient, prompt: str) -> tuple[str, dict | None]:
    completion = client.chat.completions.create(
        model=HF_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=LLM_MAX_TOKENS,
    )
    usage_obj = getattr(completion, "usage", None)
    usage = (
        {
            "prompt_tokens": getattr(usage_obj, "prompt_tokens", None),
            "completion_tokens": getattr(usage_obj, "completion_tokens", None),
            "total_tokens": getattr(usage_obj, "total_tokens", None),
        }
        if usage_obj is not None
        else None
    )
    return completion.choices[0].message.content, usage


def ask_ollama(prompt: str) -> tuple[str, dict | None]:
    r = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        },
        timeout=180,  # qwen3:8b is a "thinking" model; ~90s to reason before answering on this hardware
    )
    r.raise_for_status()
    body = r.json()
    # Ollama's non-streaming /api/chat response exposes token counts under
    # these names (not "usage") when the model reports them.
    usage = (
        {
            "prompt_tokens": body.get("prompt_eval_count"),
            "completion_tokens": body.get("eval_count"),
            "total_tokens": (
                (body.get("prompt_eval_count") or 0) + (body.get("eval_count") or 0)
            ),
        }
        if "prompt_eval_count" in body or "eval_count" in body
        else None
    )
    return body["message"]["content"], usage


def extract_json(text: str | None) -> dict | None:
    # Models routinely wrap JSON in ```json fences or add stray prose before/
    # after it despite being told not to — this pulls the JSON object out of
    # either shape rather than trusting the response is a bare JSON string.
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    if not fenced:
        brace_match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if brace_match:
            candidate = brace_match.group(0)
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or not REQUIRED_ANALYSIS_KEYS.issubset(parsed):
        return None
    return parsed


# Same convention as notify.py's _redact_secrets (T-17/#25): strip known
# secret shapes before any AI-provider failure reason reaches a log line.
_SECRET_PATTERNS = [
    re.compile(r"bot[0-9]{5,16}:[A-Za-z0-9_-]{34,36}"),
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    ),
]


def _redact_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _queue_dual_failure_alert(hf_reason: str, ollama_reason: str) -> None:
    """Best-effort operational signal for T-33 (#44): both AI providers failed
    on the same get_analysis() call. Appends to scrape.py's health-alerts
    handoff file (read first, not overwritten — scrape.py already wrote its
    own alerts there earlier in the same monitor.yml job) rather than sending
    Telegram directly, since analyze.py has no bot credentials. Never raises:
    this is observability, not a gate on the pricing pipeline, so a disk
    error here must not stop get_analysis() from returning a valid result.
    """
    message = (
        "⚠️ AI INFERENCE OUTAGE: Hugging Face returned "
        f"{_redact_secrets(hf_reason)[:200]} and Ollama is unreachable "
        f"({_redact_secrets(ollama_reason)[:200]}). System operating in "
        "deterministic rule-only mode."
    )
    try:
        existing = []
        if SCRAPE_HEALTH_ALERTS_FILE.exists():
            existing = json.load(open(SCRAPE_HEALTH_ALERTS_FILE, encoding="utf-8"))
        existing.append(message)
        SCRAPE_HEALTH_ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        json.dump(
            existing,
            open(SCRAPE_HEALTH_ALERTS_FILE, "w", encoding="utf-8"),
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        print(
            f"Failed to queue dual-failure operational alert ({e.__class__.__name__}); "
            "continuing with deterministic rule-only result",
            file=sys.stderr,
        )


def default_analysis(rule_verdict: str, thirty_day_low: float | None) -> dict:
    summaries = {
        "GENUINE_DEAL": f"Prețul este sub minimul ultimelor 30 de zile ({thirty_day_low} RON).",
        "FALSE_DISCOUNT": f"Prețul nou nu este sub minimul ultimelor 30 de zile ({thirty_day_low} RON) — posibilă majorare artificială înaintea reducerii.",
        "INFLATED_REFERENCE": "Prețul de referință afișat este exagerat față de minimul real recent.",
        "NORMAL_DROP": "Scăderea de preț este sub 5% față de minimul ultimelor 30 de zile.",
        "INSUFFICIENT_HISTORY": "Istoricul de preț este prea scurt pentru a confirma o reducere reală.",
    }
    analysis = {
        "verdict": rule_verdict,
        "verdict_score": 8 if rule_verdict == "GENUINE_DEAL" else 4,
        "summary": summaries.get(rule_verdict, "Verdict necunoscut."),
        "is_recommended": rule_verdict == "GENUINE_DEAL",
    }
    return enforce_deterministic_invariant(analysis, rule_verdict)


def enforce_deterministic_invariant(analysis: dict, rule_verdict: str) -> dict:
    """The rule engine's rule_verdict (T-23 / #30) is the sole source of
    truth for verdict and is_recommended. Called on every path in
    get_analysis — successful schema-valid LLM parse, failed validation, and
    provider-unavailable fallback — so the LLM's own verdict/is_recommended
    values are always discarded and replaced, never merged in. verdict_score
    and summary stay whatever the LLM (or default_analysis) produced: they
    are explanation only, per this repo's deterministic-primacy rule.
    """
    analysis["verdict"] = rule_verdict
    analysis["is_recommended"] = rule_verdict == "GENUINE_DEAL"
    return analysis


def validate_ai_evaluation(
    parsed: dict, provider: str | None, model_name: str | None
) -> AIDealEvaluation | None:
    try:
        return AIDealEvaluation.model_validate(parsed)
    except ValidationError as e:
        reason = _redact_secrets(str(e))[:500]
        print(
            f"AI output failed schema validation (provider={provider}, "
            f"model={model_name}): {reason}; using deterministic fallback",
            file=sys.stderr,
        )
        return None


def _log_audit_record(
    *,
    provider: str | None,
    model_name: str | None,
    prompt: str,
    latency_ms: float,
    usage: dict | None,
    raw: str | None,
    parsed_result: dict | None,
    fallback_path: str,
    final_verdict: str,
    final_is_recommended: bool,
) -> None:
    """Append-only per-call record for T-24 (#32), same convention as
    scrape.py's scrape_health.jsonl (mkdir + open(..., "a") + one JSON object
    per line). Never writes the raw model response itself, only a hash of it
    -- and redacts the LLM-controlled summary text (the same convention as
    T-17's _redact_secrets) before writing it, in case a prompt-injected
    response tries to echo a secret shape back.

    Known limitation (T-43 / #61, not fixed here): this file is untracked,
    and actions/checkout's git clean wipes it before the persist job's
    checkout completes -- it does not survive past the run that wrote it.
    """
    record = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "provider": provider,
        "model_name": model_name,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        # Proxy for "deterministic inputs": build_omnibus_prompt() is a pure
        # function of the rule engine's metrics and the alert fields, so
        # hashing the exact prompt sent is equivalent to hashing those inputs
        # without threading them through get_analysis() as extra parameters.
        "deterministic_inputs_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "latency_ms": latency_ms,
        "token_usage": usage,
        "raw_output_hash": (
            hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw is not None else None
        ),
        "parsed_result": (
            {**parsed_result, "summary": _redact_secrets(parsed_result["summary"])}
            if parsed_result is not None
            else None
        ),
        "fallback_path": fallback_path,
        "final_verdict": final_verdict,
        "final_is_recommended": final_is_recommended,
    }
    AI_AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(AI_AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def get_analysis(
    client: InferenceClient,
    prompt: str,
    rule_verdict: str,
    thirty_day_low: float | None,
) -> dict:
    raw = None
    usage = None
    provider = None
    model_name = None
    network_path = "huggingface"
    start = time.perf_counter()
    try:
        raw, usage = ask_hf(client, prompt)
        provider, model_name = "huggingface", HF_MODEL
    except Exception as e:
        print(f"HF inference failed ({e.__class__.__name__}), falling back to Ollama")
        network_path = "ollama"
        try:
            raw, usage = ask_ollama(prompt)
            provider, model_name = "ollama", OLLAMA_MODEL
        except Exception as e2:
            print(
                f"Ollama fallback also failed ({e2.__class__.__name__}), using default template"
            )
            network_path = "both_providers_failed"
            raw = None
            usage = None
            _queue_dual_failure_alert(str(e), str(e2))
    latency_ms = round((time.perf_counter() - start) * 1000, 1)

    validated = None
    fallback_path = network_path
    parsed = extract_json(raw)
    if parsed is None:
        if raw is not None:
            print(
                f"AI output malformed or missing keys (provider={provider}, "
                f"model={model_name}); using deterministic fallback",
                file=sys.stderr,
            )
            fallback_path = f"{network_path}_malformed_output"
        result = default_analysis(rule_verdict, thirty_day_low)
    else:
        validated = validate_ai_evaluation(parsed, provider, model_name)
        if validated is None:
            fallback_path = f"{network_path}_schema_invalid"
            result = default_analysis(rule_verdict, thirty_day_low)
        else:
            result = enforce_deterministic_invariant(
                validated.model_dump(), rule_verdict
            )

    _log_audit_record(
        provider=provider,
        model_name=model_name,
        prompt=prompt,
        latency_ms=latency_ms,
        usage=usage,
        raw=raw,
        parsed_result=validated.model_dump() if validated is not None else None,
        fallback_path=fallback_path,
        final_verdict=result["verdict"],
        final_is_recommended=result["is_recommended"],
    )
    return result


def main():
    alerts = json.load(open(ALERTS_FILE, encoding="utf-8"))
    if not alerts:
        json.dump([], open(FORMATTED_FILE, "w", encoding="utf-8"))
        print("No alerts to analyze")
        return

    client = InferenceClient(api_key=os.environ.get("HF_TOKEN", ""))

    formatted_alerts = []
    for a in alerts:
        try:
            RawAlertCandidate.model_validate(a)
        except ValidationError as e:
            print(
                f"Alert candidate validation failed for {a.get('url')}: {e}",
                file=sys.stderr,
            )
            raise

        thirty_day_low = a.get("thirty_day_low")
        metrics = evaluate_omnibus_rule(
            a["new_price"],
            a["old_price"],
            thirty_day_low,
            a.get("reference_price"),
            a.get("history_days", 0),
        )
        prompt = build_omnibus_prompt(a, metrics)
        analysis = get_analysis(client, prompt, metrics["rule_verdict"], thirty_day_low)
        formatted_alerts.append({**a, **metrics, **analysis})

    json.dump(
        formatted_alerts,
        open(FORMATTED_FILE, "w", encoding="utf-8"),
        indent=2,
        ensure_ascii=False,
    )
    print(
        f"Analyzed {len(alerts)} alert(s) into {len(formatted_alerts)} formatted record(s)"
    )


if __name__ == "__main__":
    main()
