"""Minimal client for NVIDIA's OpenAI-compatible chat API, returning parsed JSON.

NVIDIA's free hosted endpoints are shared: a request can sit in a queue for minutes, and models
get withdrawn (we have seen HTTP 410 and 404). So each call has a per-attempt time limit, is
then tried on a second model, then once more on the first.
"""

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
# Nemotron 3.5 Super is a reasoning model: it thinks before answering (returned separately
# as reasoning_content), which is what lets it read tables and do arithmetic.
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3.5-super-vl-120b-a12b")
# Used when the main model stalls or is unavailable. Set to "" to disable.
NVIDIA_FALLBACK_MODEL = os.environ.get("NVIDIA_FALLBACK_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
NVIDIA_API_URL = os.environ.get("NVIDIA_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions")
# Seconds to wait for one reply. A normal analysis takes ~10-25 s; a reply that takes longer is
# usually stuck in NVIDIA's queue, and a fresh request often returns in seconds. 3 attempts x 50 s
# stays inside Vercel's 300 s function limit.
NVIDIA_TIMEOUT = float(os.environ.get("NVIDIA_TIMEOUT", "50"))

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


def _plan() -> list[str]:
    """Models to try, in order: main, fallback, main.

    A stalled request rarely recovers by being re-sent to the same busy model straight away, so the
    fallback goes second; a stall then costs one timeout (~50 s), not two.
    """
    if NVIDIA_FALLBACK_MODEL and NVIDIA_FALLBACK_MODEL != NVIDIA_MODEL:
        return [NVIDIA_MODEL, NVIDIA_FALLBACK_MODEL, NVIDIA_MODEL]
    return [NVIDIA_MODEL, NVIDIA_MODEL]


def call_llm(system: str, prompt: str, required_keys: tuple[str, ...] = ()) -> dict | None:
    """Return the model's JSON object, or None if no model returns valid, complete JSON in time.

    A reply that parses but lacks `required_keys` (we have seen bare {"document_type": "unknown"})
    is retried like invalid JSON. The returned dict has a "_model" key naming the model that answered.
    """
    if not NVIDIA_API_KEY:
        return None
    plan = _plan()
    skip = set()
    for attempt, model in enumerate(plan, 1):
        if model in skip:
            continue
        try:
            payload = json.dumps({
                "model": model,
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
            with urlopen(request, timeout=NVIDIA_TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"] or ""
            parsed = json.loads(_clean_json_text(content))
            if isinstance(parsed, dict) and all(key in parsed for key in required_keys):
                parsed["_model"] = model
                return parsed
            log.warning("NVIDIA returned incomplete JSON from %s (attempt %s/%s)", model, attempt, len(plan))
        except json.JSONDecodeError:
            log.warning("NVIDIA returned invalid JSON from %s (attempt %s/%s)", model, attempt, len(plan))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            log.warning("NVIDIA request failed: HTTP %s for model %s: %s", exc.code, model, detail)
            if exc.code in (401, 403):
                return None  # bad key: another model will not help
            if exc.code in (404, 410):
                skip.add(model)  # model withdrawn: go straight to the fallback
        except Exception as exc:  # timeouts and network errors: try again
            log.warning("NVIDIA request to %s failed (%s: %s), attempt %s/%s", model, type(exc).__name__, exc, attempt, len(plan))
    return None
