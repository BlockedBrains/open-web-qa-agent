from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from .config import Settings


def fix_json(raw: str) -> str:
    raw = re.sub(r"```json|```", "", str(raw or "")).strip()
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    return raw


def extract_json_array(raw: str) -> list[dict[str, Any]] | None:
    cleaned = fix_json(raw)
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    wrapper = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if wrapper:
        try:
            obj = json.loads(wrapper.group())
            if isinstance(obj, dict) and isinstance(obj.get("results"), list):
                return obj["results"]
        except Exception:
            pass
    return None


def extract_json_object(raw: str) -> dict[str, Any] | None:
    cleaned = fix_json(raw)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def llm_log(settings: Settings, msg: str, raw: str = "", force: bool = False) -> None:
    if not (settings.llm_debug or force):
        return
    try:
        with open(settings.llm_debug_file, "a", encoding="utf-8") as handle:
            handle.write(msg.strip() + "\n")
            if raw:
                compact = raw[:2000].replace("\n", "\\n")
                handle.write(f"RAW: {compact}\n")
            handle.write("-" * 40 + "\n")
    except Exception:
        pass


def resolve_provider(settings: Settings) -> str:
    provider = (settings.llm_provider or "").strip().lower()
    if provider and provider != "auto":
        return provider
    url = (settings.llm_url or settings.ollama_url or "").lower()
    if "ollama" in url or "11434" in url:
        return "ollama"
    return "openai_compatible"


def resolve_model(settings: Settings, purpose: str = "analysis") -> str:
    purpose = (purpose or "analysis").lower()
    if purpose == "report" and settings.llm_report_model:
        return settings.llm_report_model
    if purpose == "analysis" and settings.llm_analysis_model:
        return settings.llm_analysis_model
    return settings.llm_model or settings.ollama_model


def resolve_chat_url(settings: Settings) -> str:
    raw = (settings.llm_url or settings.ollama_url or "").rstrip("/")
    if not raw:
        raise ValueError("LLM URL is not configured")
    if raw.startswith("http://") or raw.startswith("https://"):
        without_scheme = raw.split("://", 1)[1]
        if "/" not in without_scheme:
            return raw + "/v1/chat/completions"
    if raw.endswith("/chat/completions"):
        return raw
    if raw.endswith("/v1"):
        return raw + "/chat/completions"
    if raw.endswith("/openai"):
        return raw + "/chat/completions"
    return raw


def call_chat(
    settings: Settings,
    messages: list[dict[str, Any]],
    *,
    purpose: str = "analysis",
    model: str = "",
    json_mode: bool = False,
    temperature: float = 0.0,
    max_tokens: int = 2048,
) -> str:
    if settings.llm_preflight_ok is False:
        raise RuntimeError(settings.llm_last_error or "LLM preflight failed")

    provider = resolve_provider(settings)
    selected_model = model or resolve_model(settings, purpose)
    if not selected_model:
        raise ValueError("LLM model is not configured")

    payload: dict[str, Any] = {
        "model": selected_model,
        "messages": messages,
        "stream": False,
    }
    if provider == "ollama":
        payload["options"] = {
            "temperature": temperature,
            "num_predict": max_tokens,
        }
        if json_mode:
            payload["format"] = "json"
    else:
        payload["temperature"] = temperature
        payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

    headers = {"Content-Type": "application/json"}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        resolve_chat_url(settings),
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.llm_timeout_seconds) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail[:300]}") from exc

    try:
        content = result["choices"][0]["message"]["content"]
    except Exception as exc:
        raise RuntimeError(f"Unexpected LLM response shape: {result}") from exc

    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
        return "\n".join(text_parts).strip()
    return str(content or "")


def preflight_llm(settings: Settings) -> tuple[bool, str]:
    if not settings.llm_preflight:
        settings.llm_preflight_ok = None
        settings.llm_last_error = ""
        return True, "skipped"

    provider = resolve_provider(settings)
    model = resolve_model(settings, "analysis")
    try:
        url = resolve_chat_url(settings)
    except Exception as exc:
        url = settings.llm_url or settings.ollama_url or ""
        message = f"{type(exc).__name__}: {exc}"
        settings.llm_preflight_ok = False
        settings.llm_last_error = message
        llm_log(
            settings,
            f"[LLM][ERROR] Preflight failed provider={provider} model={model} url={url}: {message}",
            force=True,
        )
        return False, message
    try:
        raw = call_chat(
            settings,
            [
                {"role": "system", "content": "Reply with READY only."},
                {"role": "user", "content": "healthcheck"},
            ],
            purpose="analysis",
            model=model,
            json_mode=False,
            temperature=0,
            max_tokens=16,
        )
        text = str(raw or "").strip()
        if not text:
            raise RuntimeError("LLM returned an empty response")
        settings.llm_preflight_ok = True
        settings.llm_last_error = ""
        llm_log(
            settings,
            f"[LLM] Preflight OK provider={provider} model={model} url={url}",
            raw=text,
        )
        return True, text[:80]
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        settings.llm_preflight_ok = False
        settings.llm_last_error = message
        llm_log(
            settings,
            f"[LLM][ERROR] Preflight failed provider={provider} model={model} url={url}: {message}",
            force=True,
        )
        return False, message
