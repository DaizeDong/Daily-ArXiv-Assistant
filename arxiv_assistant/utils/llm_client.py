"""Shared LLM + agent backend resolution for BOTH pipelines (paper digest + hotspots)."""
from __future__ import annotations
from arxiv_assistant.utils.models import DEFAULT_AGENT_MODEL

import json
import os
from typing import Any

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_LLM_MODEL = "gpt-5.4"
_DEFAULT_AGENT_MODEL = DEFAULT_AGENT_MODEL


def resolve_openai_config(*, api_key: str | None = None, base_url: str | None = None) -> tuple[str, str]:
    """Resolve the OpenAI ``(api_key, base_url)`` -- the ONE place env defaults live.

    ``api_key`` defaults to ``$OPENAI_API_KEY`` (empty string if unset, matching the
    historical ``enrich`` behaviour); ``base_url`` defaults to ``$OPENAI_BASE_URL`` then
    ``https://api.openai.com/v1``, trailing slash stripped.
    """
    key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
    url = (base_url or os.environ.get("OPENAI_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
    return key, url


def resolve_extra_headers(raw: str | None = None) -> dict[str, str]:
    """Headers to send alongside every request, from ``$OPENAI_EXTRA_HEADERS``.

    The SDK authenticates with ``Authorization: Bearer``, which is the one thing
    a gateway in front of a model does not have to accept. The gateway this was
    written for is Azure API Management: it wants
    ``Ocp-Apim-Subscription-Key`` and answers a Bearer request with 401 and
    "Access denied due to missing subscription key". Measured, not assumed --
    the same request with the right header returns 200.

    Malformed JSON raises. A gateway credential that quietly failed to parse
    would look exactly like a wrong key, one 401 at a time, and the header is
    the whole reason this host can reach a model at all.
    """
    text = (raw if raw is not None else os.environ.get("OPENAI_EXTRA_HEADERS", "")).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ValueError(
            "OPENAI_EXTRA_HEADERS is not valid JSON: %s. Expected an object of "
            "header name to value." % exc
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError("OPENAI_EXTRA_HEADERS must be a JSON object of headers.")
    return {str(k): str(v) for k, v in parsed.items()}


def get_openai_client(*, api_key: str | None = None, base_url: str | None = None) -> Any:
    """The SINGLE place the ``openai.OpenAI`` client is constructed."""
    from openai import OpenAI  # lazy: keep import cost off modules that only resolve config

    key, url = resolve_openai_config(api_key=api_key, base_url=base_url)
    headers = resolve_extra_headers()
    # The SDK refuses to construct without an api_key even when the gateway
    # ignores it, so a placeholder stands in rather than making every caller
    # invent one. The headers carry the real credential in that case.
    if headers and not key:
        key = "unused-the-gateway-authenticates-by-header"
    return OpenAI(api_key=key, base_url=url, default_headers=headers or None)


def _section_get(config: Any, section: str, key: str) -> str | None:
    """``config[section].get(key)`` that tolerates a missing section/key and blank values."""
    try:
        value = config[section].get(key)
    except (KeyError, TypeError):
        return None
    value = (value or "").strip() if isinstance(value, str) else value
    return value or None


def resolve_llm_model(config: Any, *, override: str | None = None, fallback: str = _DEFAULT_LLM_MODEL) -> str:
    """OpenAI model id: explicit ``override`` (legacy per-section key) wins, then ``[LLM] model``, then fallback."""
    if override and str(override).strip():
        return str(override).strip()
    return _section_get(config, "LLM", "model") or fallback


def resolve_agent_model(config: Any, *, override: str | None = None, fallback: str = _DEFAULT_AGENT_MODEL) -> str:
    """``claude -p`` agent model id: explicit ``override`` (legacy per-section key) wins, then ``[AGENT] model``, then fallback."""
    if override and str(override).strip():
        return str(override).strip()
    return _section_get(config, "AGENT", "model") or fallback
