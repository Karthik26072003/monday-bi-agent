"""Build a human-readable data-quality report from a cleaned DataFrame.

The agent calls get_data_quality_report(...) and is instructed to fold the
relevant caveats into its answers instead of silently presenting numbers.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

import pandas as pd


def _flag_counts(df: pd.DataFrame) -> dict[str, int]:
    c: Counter = Counter()
    for flags in df["dq_flags"]:
        c.update(flags)
    return dict(c.most_common())


def _null_rates(df: pd.DataFrame, cols: list[str]) -> dict[str, str]:
    out = {}
    for col in cols:
        if col in df.columns:
            n = int(df[col].isna().sum())
            out[col] = f"{n}/{len(df)} ({n / len(df):.0%})"
    return out


_FLAG_EXPLANATIONS = {
    "header_row_pasted_into_data": "Rows where the sheet header was pasted into data cells; excluded from all metrics.",
    "missing_deal_value": "Deal has no value; excluded from revenue/pipeline sums but counted in deal counts.",
    "missing_close_date": "No close date; the deal cannot be placed in a quarter.",
    "sector_unmapped_or_not_an_industry": "Sector value is a deal type (e.g. 'Tender', 'DSP') not an industry; bucketed as 'Others'.",
    "suspected_bulk_import_artifact": "Large block of identical 'Won / Lead Generated' rows dated 2025-11-27 - looks like an import artefact; excluded from won/revenue metrics by default.",
    "exact_duplicate_row": "Byte-identical duplicate of an earlier row; excluded by default (deduplicated).",
    "unresolved_deal_status": "Status could not be classified as Open/Won/Lost/On Hold.",
    "very_large_value_possible_scale_error": "Deal value is >=100M (masked) - ~1000x the typical deal; may be a data-entry scale error. Included in sums but treat sector value totals with caution.",
    "amount_is_spreadsheet_error": "Amount cell contained a spreadsheet error (#VALUE!); treated as missing.",
    "amount_is_masked_placeholder": "Amount is the masked placeholder (1.2332); treated as missing, not ~1 rupee.",
    "missing_order_value": "Work order has no order value; excluded from revenue sums.",
    "missing_sector": "No sector; bucketed as 'Unknown'.",
    "missing_po_date": "No PO/LOI date; work order cannot be placed in a quarter.",
    "end_before_start": "Probable end date precedes start date; duration not computed.",
    "unresolved_execution_status": "Execution status could not be classified.",
}


def build_report(df: pd.DataFrame, board_label: str) -> dict[str, Any]:
    total = len(df)
    valid = int(df["is_valid"].sum()) if "is_valid" in df else total
    dups = int(df["is_duplicate"].sum()) if "is_duplicate" in df else 0
    artifacts = int(df["suspected_artifact"].sum()) if "suspected_artifact" in df else 0

    flag_counts = _flag_counts(df)
    caveats = []
    for flag, n in flag_counts.items():
        if flag in _FLAG_EXPLANATIONS:
            caveats.append(f"{n} row(s): {_FLAG_EXPLANATIONS[flag]}")

    if board_label == "deals":
        null_cols = ["deal_value", "close_date", "probability_pct", "sector_raw", "owner_code"]
        analysis_rows = int((df["is_valid"] & ~df["is_duplicate"] & ~df["suspected_artifact"]).sum())
    else:
        null_cols = ["order_value_ex_gst", "billed_inc_gst", "collected_inc_gst", "po_date", "sector_raw"]
        analysis_rows = int((df["is_valid"] & ~df["is_duplicate"]).sum())

    return {
        "board": board_label,
        "rows_total": total,
        "rows_structurally_valid": valid,
        "rows_used_for_analysis_by_default": analysis_rows,
        "exact_duplicates_excluded": dups,
        "suspected_import_artifacts_excluded": artifacts,
        "null_rates": _null_rates(df, null_cols),
        "flag_counts": flag_counts,
        "caveats": caveats,
    }


def default_analysis_frame(df: pd.DataFrame, board_label: str) -> pd.DataFrame:
    """The subset most questions should run against: valid, de-duplicated,
    and (for deals) without the suspected import artefact block."""
    mask = df["is_valid"] & ~df["is_duplicate"]
    if board_label == "deals" and "suspected_artifact" in df.columns:
        mask &= ~df["suspected_artifact"]
    return df[mask].copy()
