"""Minimal client for NVIDIA's OpenAI-compatible chat API, returning parsed JSON."""

from __future__ import annotations

import json
import logging
import os
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY")
# Nemotron 3 Super is a reasoning model: it thinks before answering (returned separately
# as reasoning_content), which is what lets it read tables and do arithmetic.
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3-super-120b-a12b")
NVIDIA_API_URL = os.environ.get("NVIDIA_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions")

log = logging.getLogger(__name__)


def _clean_json_text(raw: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.S | re.I).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.I)
    if not cleaned.startswith("{"):
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if match:
            cleaned = match.group(0)
    return cleaned.strip()


def configured() -> bool:
    return bool(NVIDIA_API_KEY)


def call_llm(system: str, prompt: str, required_keys: tuple[str, ...] = (), attempts: int = 2) -> dict | None:
    """Return the model's JSON object, or None if the API is unavailable or never returns valid, complete JSON.

    A reply that parses but lacks `required_keys` (we have seen bare {"document_type": "unknown"})
    is retried like invalid JSON.
    """
    if not NVIDIA_API_KEY:
        return None
    for attempt in range(1, attempts + 1):
        try:
            payload = json.dumps({
                "model": NVIDIA_MODEL,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 8192,
                # JSON mode: without it, quotes inside documents (e.g. 48" fan sizes) can break the reply.
                "response_format": {"type": "json_object"},
            }).encode("utf-8")
            request = Request(
                NVIDIA_API_URL,
                data=payload,
                headers={"Authorization": f"Bearer {NVIDIA_API_KEY}", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=180) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"] or ""
            parsed = json.loads(_clean_json_text(content))
            if isinstance(parsed, dict) and all(key in parsed for key in required_keys):
                return parsed
            log.warning("NVIDIA returned incomplete JSON (attempt %s/%s)", attempt, attempts)
        except json.JSONDecodeError:
            log.warning("NVIDIA returned invalid JSON (attempt %s/%s)", attempt, attempts)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            log.warning("NVIDIA request failed: HTTP %s for model %s: %s", exc.code, NVIDIA_MODEL, detail)
            return None
        except Exception as exc:
            log.warning("NVIDIA request failed (%s: %s)", type(exc).__name__, exc)
            return None
    return None
