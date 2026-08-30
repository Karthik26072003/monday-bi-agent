"""Central configuration.

Values are read from (in order): Streamlit secrets, then environment variables.
Nothing sensitive is hardcoded. See .streamlit/secrets.toml.example and README.md.
"""
from __future__ import annotations

import os
from functools import lru_cache


def _from_streamlit_secrets(key: str):
    try:
        import streamlit as st  # imported lazily so non-Streamlit callers (tests) work
    except Exception:
        return None
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        # st.secrets raises if no secrets file exists at all
        return None
    return None


def get(key: str, default=None):
    val = _from_streamlit_secrets(key)
    if val is None or val == "":
        val = os.environ.get(key)
    if val is None or val == "":
        return default
    return val


class Config:
    # --- LLM (OpenAI-compatible chat API) ---
    # Default: Groq (free tier, no card, good tool-calling). Swap base_url + model
    # for Google Gemini, OpenRouter, Cerebras, a local Ollama, or OpenAI.
    #   Groq     : https://api.groq.com/openai/v1              llama-3.3-70b-versatile
    #   Gemini   : https://generativelanguage.googleapis.com/v1beta/openai/   gemini-2.5-flash
    #   OpenRouter: https://openrouter.ai/api/v1              (many :free models)
    LLM_API_KEY = property(
        lambda self: get("LLM_API_KEY") or get("GROQ_API_KEY") or get("OPENAI_API_KEY")
    )
    LLM_BASE_URL = property(lambda self: get("LLM_BASE_URL", "https://api.groq.com/openai/v1"))
    LLM_MODEL = property(lambda self: get("LLM_MODEL", "openai/gpt-oss-120b"))

    # --- monday.com ---
    MONDAY_API_TOKEN = property(lambda self: get("MONDAY_API_TOKEN"))
    MONDAY_API_URL = property(lambda self: get("MONDAY_API_URL", "https://api.monday.com/v2"))
    MONDAY_API_VERSION = property(lambda self: get("MONDAY_API_VERSION", "2024-10"))
    MONDAY_DEALS_BOARD_ID = property(lambda self: get("MONDAY_DEALS_BOARD_ID"))
    MONDAY_WORK_ORDERS_BOARD_ID = property(lambda self: get("MONDAY_WORK_ORDERS_BOARD_ID"))

    # --- behaviour ---
    CACHE_TTL_SECONDS = property(lambda self: int(get("CACHE_TTL_SECONDS", "300")))
    MAX_AGENT_STEPS = property(lambda self: int(get("MAX_AGENT_STEPS", "10")))

    # dev-only escape hatch: "monday" (default) or "csv". CSV mode is for local
    # development / tests only and is clearly flagged in the UI. The hosted
    # prototype always runs against monday.com.
    DATA_SOURCE = property(lambda self: get("SKY_DATA_SOURCE", "monday").lower())
    DEALS_CSV = property(lambda self: get("SKY_DEALS_CSV", "sample_data/deals.csv"))
    WORK_ORDERS_CSV = property(lambda self: get("SKY_WORK_ORDERS_CSV", "sample_data/work_orders.csv"))


config = Config()


@lru_cache(maxsize=1)
def currency_label() -> str:
    return "INR (masked)"
