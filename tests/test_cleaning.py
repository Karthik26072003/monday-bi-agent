"""Cleaning-layer tests against the real (messy) sample CSVs."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from monday_bi.cleaning import (
    clean_deals,
    clean_work_orders,
    indian_fiscal_quarter,
    parse_date,
    parse_money,
    parse_quantity,
)
from monday_bi.loader import deals_csv, work_orders_csv
from monday_bi.quality import build_report, default_analysis_frame

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def deals():
    return clean_deals(deals_csv(str(ROOT / "sample_data" / "deals.csv")))


@pytest.fixture(scope="module")
def work_orders():
    return clean_work_orders(work_orders_csv(str(ROOT / "sample_data" / "work_orders.csv")))


# --------------------------------------------------------------- scalar parsers
def test_parse_money_handles_mess():
    assert parse_money("1,234.5") == 1234.5
    assert np.isnan(parse_money("#VALUE!"))
    assert np.isnan(parse_money(""))
    assert np.isnan(parse_money("1.2332"))       # masked placeholder
    assert np.isnan(parse_money("1.455176"))
    assert parse_money("305850000") == 305850000.0


def test_parse_date_variants():
    assert parse_date("2025-11-27") == pd.Timestamp("2025-11-27")
    assert parse_date("NA") is pd.NaT
    assert parse_date("") is pd.NaT


def test_parse_quantity_units():
    assert parse_quantity("5360 HA") == (5360.0, "HA")
    assert parse_quantity("1,310.850")[0] == 1310.85
    val, unit = parse_quantity("NA")
    assert np.isnan(val) and unit is None


def test_fiscal_quarter():
    assert indian_fiscal_quarter(pd.Timestamp("2025-04-01")) == "FY26 Q1"
    assert indian_fiscal_quarter(pd.Timestamp("2026-01-15")) == "FY26 Q4"
    assert indian_fiscal_quarter(pd.NaT) is None


# --------------------------------------------------------------- deals
def test_deals_load(deals):
    assert len(deals) > 300
    assert {"status", "stage", "deal_value", "sector", "dq_flags"} <= set(deals.columns)


def test_deals_flags_header_junk(deals):
    junk = deals[deals["dq_flags"].map(lambda f: "header_row_pasted_into_data" in f)]
    assert len(junk) >= 1
    assert not junk["is_valid"].any()


def test_deals_status_resolved(deals):
    valid = deals[deals["is_valid"]]
    assert valid["status"].isin(["Open", "Won", "Lost", "On Hold", "Unknown"]).all()
    assert valid["is_won"].sum() > 0
    assert valid["is_lost"].sum() > 0


def test_deals_energy_sector_flag(deals):
    assert deals.loc[deals["sector"] == "Renewables", "is_energy_sector"].all()
    assert not deals.loc[deals["sector"] == "Mining", "is_energy_sector"].any()


def test_deals_dedup_and_artifact(deals):
    assert deals["is_duplicate"].sum() > 0
    assert deals["suspected_artifact"].sum() > 0
    frame = default_analysis_frame(deals, "deals")
    assert not frame["is_duplicate"].any()
    assert not frame["suspected_artifact"].any()


# --------------------------------------------------------------- work orders
def test_work_orders_load(work_orders):
    assert len(work_orders) > 150
    assert {"execution_status", "billed_inc_gst", "receivable", "sector"} <= set(work_orders.columns)


def test_work_orders_masked_money_is_null(work_orders):
    flagged = work_orders[work_orders["dq_flags"].map(lambda f: "amount_is_masked_placeholder" in f)]
    assert len(flagged) >= 1
    assert flagged["order_value_ex_gst"].isna().all()


def test_work_orders_spreadsheet_error_flagged(work_orders):
    flagged = work_orders["dq_flags"].map(lambda f: "amount_is_spreadsheet_error" in f)
    assert flagged.sum() >= 1


def test_tools_coerce_bad_arg_types(monkeypatch):
    """The model occasionally sends numbers where a string is expected; tools must
    not raise 'float/int object has no attribute lower/strip'."""
    monkeypatch.setenv("SKY_DATA_SOURCE", "csv")
    from monday_bi import tools

    for name, args in [
        ("compute_metric", {"metric": "pipeline_health", "quarter": 2026}),
        ("compute_metric", {"metric": "revenue_summary", "sector": 0}),
        ("query_dataframe", {"board": "deals", "expression": 42}),
        ("search_column_values", {"board": "deals", "column": "sector", "query": 123}),
        ("get_data_quality_report", {"board": None}),
    ]:
        out = tools.dispatch(name, args)
        assert "has no attribute 'lower'" not in out
        assert "has no attribute 'strip'" not in out


def test_quality_report_shape(deals, work_orders):
    for df, label in [(deals, "deals"), (work_orders, "work_orders")]:
        rep = build_report(df, label)
        assert rep["rows_total"] == len(df)
        assert rep["rows_used_for_analysis_by_default"] <= rep["rows_total"]
        assert isinstance(rep["caveats"], list) and rep["caveats"]
