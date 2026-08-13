"""ai.py - Qwen chat client (OpenAI-compatible), per AI_LOCAL_NOTES contract.

- POST {LLM_API_URL}, Bearer {LLM_API_KEY}
- Omit the `model` field for the GGUF backend (set LLM_MODEL only for public providers)
- No tool-calling; the model returns JSON in message.content, which we extract + parse
- TLS verified by default; pin with LLM_CA_BUNDLE, opt out with LLM_VERIFY_SSL_DISABLE=1
- ~262k context; estimate tokens as chars/2.6 before sending
"""
import json
import os
import re

import requests


class LLMError(Exception):
    pass


def _verify():
    if str(os.environ.get("LLM_VERIFY_SSL_DISABLE", "")).lower() in ("1", "true", "yes"):
        return False
    return os.environ.get("LLM_CA_BUNDLE") or True


def estimate_tokens(text):
    """JSON/Turkish-heavy text measures ~2.6 chars/token (per AI_LOCAL_NOTES)."""
    return int(len(text or "") / 2.6)


def chat(system, user, temperature=0.2, max_tokens=2048, timeout=120):
    """Single-turn chat. Returns (content_str, finish_reason)."""
    url = os.environ.get("LLM_API_URL")
    if not url:
        raise LLMError("LLM_API_URL not set (fail-closed)")
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("LLM_API_KEY")
    if key:
        headers["Authorization"] = "Bearer " + key
    body = {
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    model = os.environ.get("LLM_MODEL")
    if model:  # omit for the GGUF backend (it 400s if present)
        body["model"] = model
    try:
        r = requests.post(url, json=body, headers=headers, verify=_verify(), timeout=timeout)
    except requests.RequestException as exc:
        raise LLMError(f"request failed: {exc}") from exc
    if r.status_code != 200:
        raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        choice = r.json()["choices"][0]
        return choice["message"]["content"], choice.get("finish_reason")
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"unexpected response shape: {r.text[:200]}") from exc


def parse_json(content):
    """Strip ```json fences and extract the first {...} object; raise on failure."""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.S)
    text = m.group(1) if m else None
    if text is None:
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise LLMError("no JSON object found in model output")
        text = content[start:end + 1]
    return json.loads(text)


class FakeLLM:
    """Test double for unit tests only - returns canned, schema-valid JSON without the
    network. The real pipeline always uses chat(); this is NOT an offline run mode."""

    def __init__(self, payload=None):
        self.payload = payload

    def chat(self, system, user, **kwargs):
        obj = self.payload or {
            "headline": "Fake headline for tests",
            "key_points": [{"point": "point one", "evidence": "quoted evidence"}],
            "instruments": ["USD/JPY"],
            "stance": "neutral",
            "risk_flags": ["test risk"],
            "one_paragraph": "A fake one-paragraph abstract used only in tests.",
        }
        return "```json\n" + json.dumps(obj) + "\n```", "stop"


if __name__ == "__main__":  # smoke: one tiny live call
    try:
        content, finish = chat("You reply with one word.", "Reply with exactly: OK", max_tokens=8)
        print("finish:", finish, "| content:", repr(content[:80]))
    except LLMError as e:
        print("LLM error:", e)
