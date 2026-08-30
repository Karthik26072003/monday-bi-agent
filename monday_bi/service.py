"""Data-access facade used by both the UI and the agent tools.

Fetch (monday.com, or CSV in dev mode) -> clean -> cache. Everything downstream
asks this module for `get_deals()` / `get_work_orders()` and never touches the
monday client or the CSVs directly.
"""
from __future__ import annotations

import time
from typing import Any

import pandas as pd

from config import config
from monday_bi import loader
from monday_bi.cleaning import clean_deals, clean_work_orders
from monday_bi.monday_client import MondayClient, MondayError
from monday_bi.quality import build_report

_MEM: dict[str, tuple[float, Any]] = {}


def _cache(fn):
    """Process-wide TTL cache. Deliberately not st.cache_data: it serialises return
    values (parquet/pickle) and trips over the list-valued `dq_flags` column on some
    Streamlit versions. This dict lives for the life of the server process, so it is
    still shared across sessions and reruns."""

    def wrapper(*args, **kwargs):
        key = f"{fn.__name__}:{args}:{kwargs}"
        hit = _MEM.get(key)
        if hit and time.time() - hit[0] < config.CACHE_TTL_SECONDS:
            return hit[1]
        val = fn(*args, **kwargs)
        _MEM[key] = (time.time(), val)
        return val

    return wrapper


# --------------------------------------------------------------------- raw fetch
@_cache
def _raw_deals() -> pd.DataFrame:
    if config.DATA_SOURCE == "csv":
        return loader.deals_csv(config.DEALS_CSV)
    board_id = config.MONDAY_DEALS_BOARD_ID
    if not board_id:
        raise MondayError("MONDAY_DEALS_BOARD_ID is not configured.")
    return loader.fetch_board_dataframe(board_id)


@_cache
def _raw_work_orders() -> pd.DataFrame:
    if config.DATA_SOURCE == "csv":
        return loader.work_orders_csv(config.WORK_ORDERS_CSV)
    board_id = config.MONDAY_WORK_ORDERS_BOARD_ID
    if not board_id:
        raise MondayError("MONDAY_WORK_ORDERS_BOARD_ID is not configured.")
    return loader.fetch_board_dataframe(board_id)


# --------------------------------------------------------------------- cleaned
@_cache
def get_deals() -> pd.DataFrame:
    return clean_deals(_raw_deals())


@_cache
def get_work_orders() -> pd.DataFrame:
    return clean_work_orders(_raw_work_orders())


@_cache
def get_quality_reports() -> dict[str, Any]:
    return {
        "deals": build_report(get_deals(), "deals"),
        "work_orders": build_report(get_work_orders(), "work_orders"),
    }


# --------------------------------------------------------------------- health / admin
def health_check() -> dict[str, Any]:
    info: dict[str, Any] = {"data_source": config.DATA_SOURCE, "model": config.LLM_MODEL}
    if config.DATA_SOURCE == "csv":
        info["monday"] = "bypassed (dev CSV mode)"
    else:
        try:
            me = MondayClient().ping()
            info["monday"] = f"connected as {me.get('name')} <{me.get('email')}>"
        except MondayError as exc:
            info["monday"] = f"ERROR: {exc}"
            info["ok"] = False
            return info
    try:
        d, w = get_deals(), get_work_orders()
        info["deals_rows"] = len(d)
        info["work_orders_rows"] = len(w)
        info["ok"] = True
    except Exception as exc:  # noqa: BLE001 - surface anything to the UI
        import traceback

        info["ok"] = False
        info["error"] = f"{type(exc).__name__}: {exc}"
        info["traceback"] = traceback.format_exc()
    return info


def clear_cache() -> None:
    _MEM.clear()
