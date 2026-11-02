"""LLM client: Google Gemini first, NVIDIA NIM as backup, all in JSON mode.

Hosted LLM endpoints fail in several ways we have seen in this project: models withdrawn
(HTTP 404/410), shared free-tier queues that stall for minutes, and per-key rate limits (429).
So a call walks an ordered chain of (provider, model) steps:

    Gemini (rotating over all configured keys on 429)  ->  NVIDIA main model  ->  NVIDIA fallback model

Each request waits at most LLM_TIMEOUT seconds. The returned dict carries "_provider" and
"_model" so answers can say which model produced them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# --- Gemini (primary) ---
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_API_URL = os.environ.get("GEMINI_API_URL", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")

# --- NVIDIA (backup) ---
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY")
# Nemotron 3.5 Super is a reasoning model and scored 100% on our evaluation set.
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3.5-super-vl-120b-a12b")
# Second NVIDIA model, used when the first stalls or is unavailable. Set to "" to disable.
NVIDIA_FALLBACK_MODEL = os.environ.get("NVIDIA_FALLBACK_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
NVIDIA_API_URL = os.environ.get("NVIDIA_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions")

# Seconds to wait for one reply. A normal analysis takes ~5-25 s; longer usually means the request
# is stuck in a queue and the next step will answer sooner. 3 steps x 50 s fits Vercel's 300 s limit.
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT") or os.environ.get("NVIDIA_TIMEOUT") or "50")
# Total time one call_llm may spend across every step and key. Vercel stops the function at 300 s,
# so we stop early enough to return a clear error instead of being killed mid-request.
LLM_TOTAL_BUDGET = float(os.environ.get("LLM_TOTAL_BUDGET", "280"))

log = logging.getLogger(__name__)

_key_lock = threading.Lock()
_key_index = 0
_disabled_keys: set[str] = set()


def get_gemini_keys() -> list[str]:
    """All configured Gemini keys: GEMINI_API_KEYS (comma/space separated), GEMINI_API_KEY,
    GEMINI_API_KEY_1..30, or several GEMINI_API_KEY= lines in .env (GOOGLE_* names also work).
    """
    keys: list[str] = []

    def add(raw: str) -> None:
        for key in re.split(r"[\s,;]+", raw.strip().strip("\"'")):
            if key and key not in keys:
                keys.append(key)

    # Repeated GEMINI_API_KEY= lines in .env (python-dotenv would keep only the last one).
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.isfile(env_path):
        try:
            with open(env_path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if re.match(r"^(?:GEMINI|GOOGLE)_API_KEYS?(?:_\d+)?\s*=", line):
                        add(line.split("=", 1)[1])
        except OSError:
            pass

    add(os.environ.get("GEMINI_API_KEYS") or os.environ.get("GOOGLE_API_KEYS") or "")
    add(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "")
    for index in range(1, 31):
        add(os.environ.get(f"GEMINI_API_KEY_{index}", ""))
    return keys


def _steps() -> list[tuple[str, str]]:
    """Ordered (provider, model) chain. LLM_PROVIDER=nvidia puts NVIDIA first."""
    gemini = [("gemini", GEMINI_MODEL)] if get_gemini_keys() else []
    nvidia = []
    if NVIDIA_API_KEY:
        nvidia.append(("nvidia", NVIDIA_MODEL))
        if NVIDIA_FALLBACK_MODEL and NVIDIA_FALLBACK_MODEL != NVIDIA_MODEL:
            nvidia.append(("nvidia", NVIDIA_FALLBACK_MODEL))
    chain = nvidia + gemini if os.environ.get("LLM_PROVIDER", "").strip().lower() == "nvidia" else gemini + nvidia
    if len(chain) == 1:
        chain = chain * 2  # a single configured model still gets one retry
    return chain


def get_provider() -> str:
    """The provider tried first."""
    steps = _steps()
    return steps[0][0] if steps else "gemini"


def get_model() -> str:
    """The model tried first."""
    steps = _steps()
    return steps[0][1] if steps else GEMINI_MODEL


def configured() -> bool:
    return bool(get_gemini_keys() or NVIDIA_API_KEY)


def _next_key(keys: list[str]) -> str:
    """Thread-safe round-robin over a key pool, spreading load across Gemini keys."""
    global _key_index
    with _key_lock:
        selected = keys[_key_index % len(keys)]
        _key_index = (_key_index + 1) % len(keys)
        return selected


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


def _post(url: str, key: str, model: str, system: str, prompt: str, timeout: float) -> dict:
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 8192,
        # JSON mode: without it, quotes inside documents (e.g. 48" fan sizes) can break the reply.
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    request = Request(url, data=payload, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def call_llm(system: str, prompt: str, required_keys: tuple[str, ...] = ()) -> dict | None:
    """Return the first valid, complete JSON object from the step chain, or None.

    A reply that parses but lacks `required_keys` (we have seen bare {"document_type": "unknown"})
    counts as a failure of that step.
    """
    steps = _steps()
    total = len(steps)
    deadline = time.monotonic() + LLM_TOTAL_BUDGET
    for number, (provider, model) in enumerate(steps, 1):
        if provider == "gemini":
            pool = [key for key in get_gemini_keys() if key not in _disabled_keys]
            url = GEMINI_API_URL
        else:
            pool = [NVIDIA_API_KEY] if NVIDIA_API_KEY and NVIDIA_API_KEY not in _disabled_keys else []
            url = NVIDIA_API_URL
        tried: set[str] = set()
        while pool:
            remaining = deadline - time.monotonic()
            if remaining < 5:
                log.warning("LLM time budget of %.0f s used up; giving up", LLM_TOTAL_BUDGET)
                return None
            key = _next_key([k for k in pool if k not in tried] or pool)
            tried.add(key)
            try:
                data = _post(url, key, model, system, prompt, min(LLM_TIMEOUT, remaining))
                content = data["choices"][0]["message"]["content"] or ""
                parsed = json.loads(_clean_json_text(content))
                if isinstance(parsed, dict) and all(name in parsed for name in required_keys):
                    parsed["_provider"], parsed["_model"] = provider, model
                    return parsed
                log.warning("%s %s returned incomplete JSON (step %s/%s)", provider, model, number, total)
            except json.JSONDecodeError:
                log.warning("%s %s returned invalid JSON (step %s/%s)", provider, model, number, total)
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
                log.warning("%s request failed: HTTP %s for model %s: %s", provider, exc.code, model, detail)
                if exc.code == 429 and len(tried) < len(pool):
                    continue  # rate-limited key: rotate to the next key, same model
                if exc.code in (401, 403):
                    _disabled_keys.add(key)  # bad key: never use it again in this process
                    if len(tried) < len(pool):
                        continue
            except Exception as exc:  # timeouts and network errors
                log.warning("%s request to %s failed (%s: %s), step %s/%s", provider, model, type(exc).__name__, exc, number, total)
            break  # this step failed: move on to the next provider/model
    return None
