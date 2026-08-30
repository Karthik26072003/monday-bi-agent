"""Reusable, tested business-intelligence calculations.

Every function takes a *cleaned* frame (from cleaning.py) and returns plain
dict/list structures that serialise cleanly into a tool result. The agent can
also go off-script with the query_dataframe tool, but these give reliable,
consistent answers for the common founder questions.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from monday_bi.cleaning import ENERGY_SECTORS
from monday_bi.quality import default_analysis_frame


def _round(x) -> float | None:
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return None
    return round(float(x), 2)


def _sector_filter(df: pd.DataFrame, sector) -> pd.DataFrame:
    if sector is None or sector == "":
        return df
    s = str(sector).strip().lower()
    if s in {"", "all", "any"}:
        return df
    if s in {"energy", "energy sector", "power", "power & energy", "renewables + powerline"}:
        return df[df["sector"].isin(ENERGY_SECTORS)]
    return df[df["sector"].astype(str).str.lower() == s]


def _quarter_filter(df: pd.DataFrame, quarter, col: str) -> pd.DataFrame:
    if quarter is None or quarter == "":
        return df
    q = str(quarter).strip().upper().replace("-", " ")
    if q in {"", "ALL", "ALL TIME", "ANY"}:
        return df
    fy_col = col.replace("_date", "_fy_quarter") if col.endswith("_date") else f"{col}_fy_quarter"
    cal_col = col.replace("_date", "_cal_quarter") if col.endswith("_date") else f"{col}_cal_quarter"
    masks = []
    if fy_col in df.columns:
        masks.append(df[fy_col].fillna("").str.upper() == q)
    if cal_col in df.columns:
        masks.append(df[cal_col].fillna("").str.upper() == q)
    if not masks:
        return df.iloc[0:0]
    combined = masks[0]
    for m in masks[1:]:
        combined = combined | m
    return df[combined]


# --------------------------------------------------------------------- pipeline
def pipeline_health(deals: pd.DataFrame, sector: str | None = None,
                    quarter: str | None = None) -> dict[str, Any]:
    df = default_analysis_frame(deals, "deals")
    df = _sector_filter(df, sector)
    open_df = df[df["is_open"]]
    if quarter:
        open_df = _quarter_filter(open_df, quarter, "close")

    by_stage = (
        open_df.groupby("stage")
        .agg(deals=("deal_name", "size"),
             value=("deal_value", "sum"),
             weighted_value=("weighted_value", "sum"))
        .sort_values("value", ascending=False)
        .reset_index()
    )
    by_stage = [
        {"stage": r.stage, "deals": int(r.deals),
         "value": _round(r.value), "weighted_value": _round(r.weighted_value)}
        for r in by_stage.itertuples()
    ]

    return {
        "scope": {"sector": sector or "all", "quarter": quarter or "all (open pipeline)"},
        "open_deals": int(len(open_df)),
        "open_pipeline_value": _round(open_df["deal_value"].sum()),
        "weighted_pipeline_value": _round(open_df["weighted_value"].sum()),
        "deals_missing_value": int(open_df["deal_value"].isna().sum()),
        "by_stage": by_stage,
        "by_probability": [
            {"band": band, "deals": int(g.shape[0]), "value": _round(g["deal_value"].sum())}
            for band, g in open_df.groupby(open_df["probability_band"].fillna("Unspecified"))
        ],
        "note": "Values are masked INR. weighted_value = deal value x probability "
                "(High=0.8, Medium=0.5, Low=0.2). Deals with no value are counted but not summed.",
    }


def win_rate(deals: pd.DataFrame, sector: str | None = None,
             quarter: str | None = None) -> dict[str, Any]:
    df = default_analysis_frame(deals, "deals")
    df = _sector_filter(df, sector)
    if quarter:
        df = _quarter_filter(df, quarter, "close")
    won = df[df["is_won"]]
    lost = df[df["is_lost"]]
    decided = len(won) + len(lost)
    return {
        "scope": {"sector": sector or "all", "quarter": quarter or "all time"},
        "won": int(len(won)),
        "lost": int(len(lost)),
        "win_rate_by_count": _round(len(won) / decided) if decided else None,
        "won_value": _round(won["deal_value"].sum()),
        "lost_value": _round(lost["deal_value"].sum()),
        "win_rate_by_value": (
            _round(won["deal_value"].sum() /
                   (won["deal_value"].sum() + lost["deal_value"].sum()))
            if (won["deal_value"].sum() + lost["deal_value"].sum()) else None
        ),
        "note": "'Lost' includes stages L/N/O and status 'Dead'. Open deals excluded.",
    }


# --------------------------------------------------------------------- revenue / delivery
def revenue_summary(work_orders: pd.DataFrame, sector: str | None = None,
                    quarter: str | None = None) -> dict[str, Any]:
    df = default_analysis_frame(work_orders, "work_orders")
    df = _sector_filter(df, sector)
    if quarter:
        df = _quarter_filter(df, quarter, "po")
    return {
        "scope": {"sector": sector or "all", "quarter": quarter or "all time",
                  "period_basis": "PO/LOI date"},
        "work_orders": int(len(df)),
        "order_book_value_inc_gst": _round(df["order_value_inc_gst"].sum()),
        "billed_inc_gst": _round(df["billed_inc_gst"].sum()),
        "collected_inc_gst": _round(df["collected_inc_gst"].sum()),
        "still_to_bill_inc_gst": _round(df["to_be_billed_inc_gst"].sum()),
        "outstanding_receivable": _round(df["receivable"].sum()),
        "collection_rate": _round(
            df["collected_inc_gst"].sum() / df["billed_inc_gst"].sum()
            if df["billed_inc_gst"].sum() else None
        ),
        "work_orders_missing_value": int(df["order_value_ex_gst"].isna().sum()),
        "note": "Masked INR, GST-inclusive. 'billed'/'collected' are lifetime-to-date on "
                "each work order, filtered by PO date when a quarter is given.",
    }


def sector_performance(deals: pd.DataFrame, work_orders: pd.DataFrame) -> dict[str, Any]:
    d = default_analysis_frame(deals, "deals")
    w = default_analysis_frame(work_orders, "work_orders")

    pipe = (
        d[d["is_open"]].groupby("sector")
        .agg(open_deals=("deal_name", "size"), open_value=("deal_value", "sum"),
             weighted_value=("weighted_value", "sum"))
    )
    wonv = d[d["is_won"]].groupby("sector").agg(won_deals=("deal_name", "size"),
                                                won_value=("deal_value", "sum"))
    rev = w.groupby("sector").agg(work_orders=("serial", "size"),
                                  billed=("billed_inc_gst", "sum"),
                                  collected=("collected_inc_gst", "sum"),
                                  receivable=("receivable", "sum"))
    allsec = sorted(set(pipe.index) | set(wonv.index) | set(rev.index))
    rows = []
    for s in allsec:
        rows.append({
            "sector": s,
            "open_deals": int(pipe["open_deals"].get(s, 0)),
            "open_pipeline_value": _round(pipe["open_value"].get(s, np.nan)),
            "weighted_pipeline_value": _round(pipe["weighted_value"].get(s, np.nan)),
            "won_deals": int(wonv["won_deals"].get(s, 0)),
            "won_value": _round(wonv["won_value"].get(s, np.nan)),
            "work_orders": int(rev["work_orders"].get(s, 0)),
            "billed": _round(rev["billed"].get(s, np.nan)),
            "collected": _round(rev["collected"].get(s, np.nan)),
            "outstanding_receivable": _round(rev["receivable"].get(s, np.nan)),
        })
    rows.sort(key=lambda r: (r["billed"] or 0) + (r["open_pipeline_value"] or 0), reverse=True)
    return {
        "sectors": rows,
        "energy_sector_is": list(ENERGY_SECTORS),
        "note": "Deals and Work Orders are separate boards joined only by sector here, "
                "not row-by-row. 'Energy' = Renewables + Powerline.",
    }


def accounts_receivable(work_orders: pd.DataFrame, sector: str | None = None) -> dict[str, Any]:
    df = default_analysis_frame(work_orders, "work_orders")
    df = _sector_filter(df, sector)
    df = df[df["receivable"].notna() & (df["receivable"] > 0)]
    today = pd.Timestamp.today().normalize()
    age = (today - df["last_invoice_date"]).dt.days

    buckets = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0, "no invoice date": 0.0}
    for rec, a in zip(df["receivable"], age):
        if pd.isna(a):
            buckets["no invoice date"] += rec
        elif a <= 30:
            buckets["0-30"] += rec
        elif a <= 60:
            buckets["31-60"] += rec
        elif a <= 90:
            buckets["61-90"] += rec
        else:
            buckets["90+"] += rec

    top = (
        df.sort_values("receivable", ascending=False)
        .head(10)[["serial", "deal_name", "sector", "receivable", "last_invoice_date", "ar_priority"]]
    )
    return {
        "scope": {"sector": sector or "all"},
        "total_outstanding": _round(df["receivable"].sum()),
        "accounts_with_receivable": int(len(df)),
        "priority_accounts_outstanding": _round(df[df["ar_priority"]]["receivable"].sum()),
        "aging_buckets": {k: _round(v) for k, v in buckets.items()},
        "top_outstanding": [
            {"serial": r.serial, "deal_name": r.deal_name, "sector": r.sector,
             "receivable": _round(r.receivable),
             "last_invoice_date": None if pd.isna(r.last_invoice_date) else r.last_invoice_date.date().isoformat(),
             "priority": bool(r.ar_priority)}
            for r in top.itertuples()
        ],
        "note": "Aging is measured from 'Last invoice date' to today. Masked INR.",
    }


def operations_summary(work_orders: pd.DataFrame, sector: str | None = None) -> dict[str, Any]:
    df = default_analysis_frame(work_orders, "work_orders")
    df = _sector_filter(df, sector)
    status_mix = df["execution_status"].value_counts().to_dict()
    dur = df["duration_days"].dropna()
    dur = dur[dur >= 0]
    return {
        "scope": {"sector": sector or "all"},
        "total_work_orders": int(len(df)),
        "execution_status_mix": {k: int(v) for k, v in status_mix.items()},
        "completed": int(df["is_completed"].sum()),
        "active": int(df["is_active"].sum()),
        "completion_rate": _round(df["is_completed"].mean()),
        "median_duration_days": _round(dur.median()) if len(dur) else None,
        "contract_type_mix": {k: int(v) for k, v in df["nature_of_work"].value_counts().items()},
        "platform_attached_share": _round(df["uses_platform"].mean()),
    }


METRICS = {
    "pipeline_health": pipeline_health,
    "win_rate": win_rate,
    "revenue_summary": revenue_summary,
    "sector_performance": sector_performance,
    "accounts_receivable": accounts_receivable,
    "operations_summary": operations_summary,
}
