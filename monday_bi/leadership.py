"""'Help prepare data for leadership updates.'

Interpretation (documented in DECISION_LOG.md): a leadership update is a short,
recurring board/investor-style snapshot. This builds the *data spine* of that
update - pipeline, wins, delivery, cash, sector movement, and the data caveats
that should travel with the numbers - as a structured object. The agent turns it
into prose / bullets, or the user can ask for it in a specific format.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from monday_bi import metrics
from monday_bi.cleaning import indian_fiscal_quarter
from monday_bi.quality import build_report, default_analysis_frame


def leadership_brief(deals: pd.DataFrame, work_orders: pd.DataFrame,
                     period: str | None = None) -> dict[str, Any]:
    today = pd.Timestamp.today().normalize()
    period = period or indian_fiscal_quarter(today)

    d = default_analysis_frame(deals, "deals")
    w = default_analysis_frame(work_orders, "work_orders")

    won_this_period = d[d["is_won"] & (d["close_fy_quarter"] == period)]
    created_this_period = d[d["created_fy_quarter"] == period]
    wo_this_period = w[w["po_fy_quarter"] == period]

    # trailing view so the brief is useful even if `period` has no closed activity
    by_q_won = (
        d[d["is_won"] & d["close_fy_quarter"].notna()]
        .groupby("close_fy_quarter")
        .agg(won_deals=("deal_name", "size"), won_value=("deal_value", "sum"))
        .sort_index()
        .tail(5)
    )
    recent_quarters = [
        {"quarter": q, "won_deals": int(r.won_deals), "won_value": metrics._round(r.won_value)}
        for q, r in by_q_won.iterrows()
    ]

    sector_rows = metrics.sector_performance(deals, work_orders)["sectors"]
    top_pipeline = sorted(sector_rows, key=lambda r: r["open_pipeline_value"] or 0, reverse=True)[:3]
    top_billed = sorted(sector_rows, key=lambda r: r["billed"] or 0, reverse=True)[:3]

    return {
        "period": period,
        "generated_on": today.date().isoformat(),
        "headline_metrics": {
            "open_pipeline_value": metrics.pipeline_health(deals)["open_pipeline_value"],
            "weighted_pipeline_value": metrics.pipeline_health(deals)["weighted_pipeline_value"],
            "open_deals": metrics.pipeline_health(deals)["open_deals"],
            "deals_won_in_period": int(len(won_this_period)),
            "won_value_in_period": metrics._round(won_this_period["deal_value"].sum()),
            "new_deals_created_in_period": int(len(created_this_period)),
            "work_orders_opened_in_period": int(len(wo_this_period)),
            "billed_all_time": metrics.revenue_summary(work_orders)["billed_inc_gst"],
            "collected_all_time": metrics.revenue_summary(work_orders)["collected_inc_gst"],
            "outstanding_receivable": metrics.accounts_receivable(work_orders)["total_outstanding"],
        },
        "recent_quarters_won": recent_quarters,
        "pipeline_by_stage": metrics.pipeline_health(deals)["by_stage"],
        "win_rate_all_time": metrics.win_rate(deals),
        "top_sectors_by_open_pipeline": top_pipeline,
        "top_sectors_by_billing": top_billed,
        "delivery": {
            k: metrics.operations_summary(work_orders)[k]
            for k in ("completion_rate", "active", "median_duration_days", "execution_status_mix")
        },
        "ar_aging": metrics.accounts_receivable(work_orders)["aging_buckets"],
        "data_caveats": {
            "deals": build_report(deals, "deals")["caveats"],
            "work_orders": build_report(work_orders, "work_orders")["caveats"],
        },
        "note": "Period basis: deals by close-date fiscal quarter, work orders by PO-date "
                "fiscal quarter. Billed/collected are lifetime-to-date totals. Masked INR.",
    }
