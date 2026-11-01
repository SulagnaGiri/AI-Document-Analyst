"""LLM client supporting Google Gemini and NVIDIA NIM chat completions in JSON mode.

Supports multi-key rotation and failover across multiple API keys (e.g., 10 Gemini keys)
to distribute rate limits and handle quota exhaustion automatically.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# --- NVIDIA configuration ---
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY")
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3.5-super-vl-120b-a12b")
NVIDIA_API_URL = os.environ.get("NVIDIA_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions")

# --- Gemini configuration ---
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_URL = os.environ.get("GEMINI_API_URL", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")

log = logging.getLogger(__name__)

_key_lock = threading.Lock()
_key_index = 0


def get_gemini_keys() -> list[str]:
    """Retrieve all configured Gemini keys from single, comma-separated, numbered variables,
    or multiple repeated GEMINI_API_KEY lines in .env.
    """
    keys: list[str] = []

    # 1. Parse .env file directly to catch multiple repeated GEMINI_API_KEY= lines
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.isfile(env_path):
        try:
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#"):
                        continue
                    if re.match(r"^(?:GEMINI|GOOGLE)_API_KEY(?:_\d+)?\s*=", line):
                        val = line.split("=", 1)[1].strip().strip("\"'")
                        for k in re.split(r"[\s,;]+", val):
                            k = k.strip()
                            if k and k not in keys:
                                keys.append(k)
                    elif line.startswith("GEMINI_API_KEYS=") or line.startswith("GOOGLE_API_KEYS="):
                        val = line.split("=", 1)[1].strip().strip("\"'")
                        for k in re.split(r"[\s,;]+", val):
                            k = k.strip()
                            if k and k not in keys:
                                keys.append(k)
        except Exception:
            pass

    # 2. Comma/space/semicolon/newline separated keys in environment
    raw_keys = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GOOGLE_API_KEYS") or ""
    for key in re.split(r"[\s,;]+", raw_keys):
        k = key.strip()
        if k and k not in keys:
            keys.append(k)

    # 3. Single key in environment
    single = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if single and single not in keys:
        keys.append(single)

    # 4. Numbered keys: GEMINI_API_KEY_1 through GEMINI_API_KEY_30
    for idx in range(1, 31):
        num_key = os.environ.get(f"GEMINI_API_KEY_{idx}", "").strip()
        if num_key and num_key not in keys:
            keys.append(num_key)

    return keys


def get_provider() -> str:
    """Return active provider ('gemini' or 'nvidia')."""
    explicit = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if explicit in {"gemini", "nvidia"}:
        return explicit
    if NVIDIA_API_KEY == "test-token":
        return "nvidia"
    if get_gemini_keys():
        return "gemini"
    if NVIDIA_API_KEY:
        return "nvidia"
    return "gemini"


def get_model() -> str:
    """Return active model name."""
    if get_provider() == "gemini":
        return os.environ.get("GEMINI_MODEL", GEMINI_MODEL)
    return os.environ.get("NVIDIA_MODEL", NVIDIA_MODEL)


def configured() -> bool:
    """Return True if at least one API key is available for the active provider."""
    provider = get_provider()
    if provider == "gemini":
        return bool(get_gemini_keys())
    return bool(NVIDIA_API_KEY)


def _next_key(keys: list[str]) -> str:
    """Thread-safe round-robin selection from a key pool."""
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


_disabled_keys: set[str] = set()


def call_llm(system: str, prompt: str, required_keys: tuple[str, ...] = (), attempts: int = 2) -> dict | None:
    """Return the model's JSON object with multi-key rotation and failover.

    If an API key encounters HTTP 429 (rate limit) or quota errors, it automatically
    switches to the next available key in the pool.
    """
    provider = get_provider()
    model = get_model()

    if provider == "gemini":
        keys = get_gemini_keys()
        api_url = os.environ.get("GEMINI_API_URL", GEMINI_API_URL)
    else:
        keys = [NVIDIA_API_KEY] if NVIDIA_API_KEY else []
        api_url = os.environ.get("NVIDIA_API_URL", NVIDIA_API_URL)

    if not keys:
        return None

    active_keys = [k for k in keys if k not in _disabled_keys] or keys

    # Number of tries: give each key at least a chance if there are multiple keys
    total_tries = max(attempts, min(len(active_keys) * 2, 12))
    failed_keys = set()

    for attempt in range(1, total_tries + 1):
        candidate_keys = [k for k in active_keys if k not in failed_keys] or active_keys
        current_key = _next_key(candidate_keys)

        try:
            payload = json.dumps({
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 8192,
                "response_format": {"type": "json_object"},
            }).encode("utf-8")

            request = Request(
                api_url,
                data=payload,
                headers={"Authorization": f"Bearer {current_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=180) as response:
                data = json.loads(response.read().decode("utf-8"))

            content = data["choices"][0]["message"]["content"] or ""
            parsed = json.loads(_clean_json_text(content))
            if isinstance(parsed, dict) and all(key in parsed for key in required_keys):
                return parsed
            log.warning("%s returned incomplete JSON (attempt %s/%s)", provider, attempt, total_tries)
        except json.JSONDecodeError:
            log.warning("%s returned invalid JSON (attempt %s/%s)", provider, attempt, total_tries)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code == 429:
                log.warning("%s key hit rate limit (HTTP 429). Rotating to next key... Details: %s", provider, detail)
                failed_keys.add(current_key)
            else:
                log.warning("%s request failed: HTTP %s for model %s: %s", provider, exc.code, model, detail)
                if exc.code in {400, 401, 403}:
                    _disabled_keys.add(current_key)
                    failed_keys.add(current_key)
                if len(failed_keys) >= len(active_keys):
                    return None
        except Exception as exc:
            log.warning("%s request failed (%s: %s)", provider, type(exc).__name__, exc)
            return None
    return None

