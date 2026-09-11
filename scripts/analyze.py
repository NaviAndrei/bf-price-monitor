# scripts/analyze.py
import json
import os
import re
from pathlib import Path

import requests
from huggingface_hub import InferenceClient

ALERTS_FILE = Path("data/alerts.json")
FORMATTED_FILE = Path("data/formatted_alerts.json")

HF_MODEL = os.getenv("HF_MODEL", "Qwen/Qwen2.5-Coder-32B-Instruct")
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen3:8b"

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
        "  \"is_recommended\": <true dacă este GENUINE_DEAL și merită cumpărat, altfel false>\n"
        "}"
    )


def ask_hf(client: InferenceClient, prompt: str) -> str:
    completion = client.chat.completions.create(
        model=HF_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=LLM_MAX_TOKENS,
    )
    return completion.choices[0].message.content


def ask_ollama(prompt: str) -> str:
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
    return r.json()["message"]["content"]


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


def default_analysis(rule_verdict: str, thirty_day_low: float | None) -> dict:
    summaries = {
        "GENUINE_DEAL": f"Prețul este sub minimul ultimelor 30 de zile ({thirty_day_low} RON).",
        "FALSE_DISCOUNT": f"Prețul nou nu este sub minimul ultimelor 30 de zile ({thirty_day_low} RON) — posibilă majorare artificială înaintea reducerii.",
        "INFLATED_REFERENCE": "Prețul de referință afișat este exagerat față de minimul real recent.",
        "NORMAL_DROP": "Scăderea de preț este sub 5% față de minimul ultimelor 30 de zile.",
        "INSUFFICIENT_HISTORY": "Istoricul de preț este prea scurt pentru a confirma o reducere reală.",
    }
    return {
        "verdict": rule_verdict,
        "verdict_score": 8 if rule_verdict == "GENUINE_DEAL" else 4,
        "summary": summaries.get(rule_verdict, "Verdict necunoscut."),
        "is_recommended": rule_verdict == "GENUINE_DEAL",
    }


def get_analysis(
    client: InferenceClient, prompt: str, rule_verdict: str, thirty_day_low: float | None
) -> dict:
    raw = None
    try:
        raw = ask_hf(client, prompt)
    except Exception as e:
        print(f"HF inference failed ({e.__class__.__name__}), falling back to Ollama")
        try:
            raw = ask_ollama(prompt)
        except Exception as e2:
            print(
                f"Ollama fallback also failed ({e2.__class__.__name__}), using default template"
            )
            raw = None

    parsed = extract_json(raw)
    if parsed is None:
        return default_analysis(rule_verdict, thirty_day_low)
    return parsed


def main():
    alerts = json.load(open(ALERTS_FILE, encoding="utf-8"))
    if not alerts:
        json.dump([], open(FORMATTED_FILE, "w", encoding="utf-8"))
        print("No alerts to analyze")
        return

    client = InferenceClient(api_key=os.environ.get("HF_TOKEN", ""))

    formatted_alerts = []
    for a in alerts:
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
    print(f"Analyzed {len(alerts)} alert(s) into {len(formatted_alerts)} formatted record(s)")


if __name__ == "__main__":
    main()
