"""Deterministic cleaning / normalisation layer.

Design choice: cleaning is code, not LLM. It is testable, reproducible, and every
transformation that could hide a data problem also records a caveat that the agent
surfaces to the user (see quality.py).

Input:  a "raw" DataFrame (string cells, source-header column names).
Output: a canonical DataFrame with typed columns + `dq_flags` (list[str]) per row.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd
from dateutil import parser as dtparser

# --------------------------------------------------------------------- constants
_NULLISH = {
    "", "na", "n/a", "none", "nan", "null", "<na>", "-", "--", "tbd", "tba", "pending",
    "not available", "#n/a", "#value!", "#ref!", "#div/0!",
}

# In the Work Orders sheet, masked/withheld money is stored as this placeholder
# (1.2332 ex-GST, *1.18 = 1.455176 inc-GST). Treat as "unknown", not as ~1 rupee.
_MASKED_MONEY_SENTINELS = {1.2332, 1.455176}

_PROBABILITY_PCT = {"high": 0.80, "medium": 0.50, "low": 0.20}

# Deal Stage codes -> (clean name, is_won, is_lost, is_open)
_STAGE_MAP = {
    "A": ("Lead Generated", False, False, True),
    "B": ("Sales Qualified Lead", False, False, True),
    "C": ("Demo Done", False, False, True),
    "D": ("Feasibility", False, False, True),
    "E": ("Proposal / Commercials Sent", False, False, True),
    "F": ("Negotiations", False, False, True),
    "G": ("Project Won", True, False, False),
    "H": ("Work Order Received", True, False, False),
    "I": ("POC", False, False, True),
    "J": ("Invoice Sent", True, False, False),
    "K": ("Amount Accrued", True, False, False),
    "L": ("Project Lost", False, True, False),
    "M": ("Projects On Hold", False, False, False),
    "N": ("Not Relevant (for now)", False, True, False),
    "O": ("Not Relevant At All", False, True, False),
}

_SECTOR_CANON = {
    "mining": "Mining",
    "renewables": "Renewables",
    "renewable": "Renewables",
    "powerline": "Powerline",
    "power line": "Powerline",
    "powerlines": "Powerline",
    "railways": "Railways",
    "railway": "Railways",
    "construction": "Construction",
    "manufacturing": "Manufacturing",
    "aviation": "Aviation",
    "security and surveillance": "Security & Surveillance",
    "security & surveillance": "Security & Surveillance",
    "others": "Others",
    "other": "Others",
}
# values that describe a deal *type*, not an industry
_SECTOR_NOT_AN_INDUSTRY = {"tender", "dsp"}

# "Energy" is not a value in the data. Founders mean this:
ENERGY_SECTORS = ("Renewables", "Powerline")

_EXEC_STATUS_CANON = {
    "completed": "Completed",
    "executed until current month": "Ongoing",
    "ongoing": "Ongoing",
    "partial completed": "Partially Completed",
    "partially completed": "Partially Completed",
    "not started": "Not Started",
    "pause / struck": "On Hold",
    "pause/struck": "On Hold",
    "paused": "On Hold",
    "struck": "On Hold",
    "details pending from client": "Blocked (client)",
}

_BILLING_STATUS_CANON = {
    "billed": "Billed",
    "biilled": "Billed",
    "partially billed": "Partially Billed",
    "partial billed": "Partially Billed",
    "not billable": "Not Billable",
    "not billed yet": "Not Billed",
    "update required": "Needs Update",
    "stuck": "Stuck",
    "struck": "Stuck",
}


# --------------------------------------------------------------------- scalar parsers
def is_nullish(v) -> bool:
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return True
    except (TypeError, ValueError):
        pass
    return str(v).strip().lower() in _NULLISH


def as_text(v) -> str:
    """Null/NaN/anything -> a safe string. Use before .lower()/.strip() on raw cells."""
    return "" if is_nullish(v) else str(v)


def clean_text(v) -> str | None:
    if is_nullish(v):
        return None
    return re.sub(r"\s+", " ", str(v).strip())


def parse_money(v) -> float:
    if is_nullish(v):
        return np.nan
    s = str(v).strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("INR", "")
    s = s.strip()
    try:
        f = float(s)
    except ValueError:
        return np.nan
    if f in _MASKED_MONEY_SENTINELS or (0 < abs(f) < 2):
        return np.nan  # masked placeholder
    return f


def parse_date(v):
    if is_nullish(v):
        return pd.NaT
    s = str(v).strip()
    # plain ISO first (the common case) - fast path
    try:
        return pd.Timestamp(dtparser.isoparse(s)).normalize()
    except (ValueError, TypeError):
        pass
    for dayfirst in (False, True):
        try:
            return pd.Timestamp(dtparser.parse(s, dayfirst=dayfirst, fuzzy=False)).normalize()
        except (ValueError, TypeError, OverflowError):
            continue
    return pd.NaT


_NUM_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def parse_quantity(v) -> tuple[float, str | None]:
    """'5360 HA' -> (5360.0, 'HA'); '1,310.850' -> (1310.85, None); 'NA' -> (nan, None)."""
    if is_nullish(v):
        return np.nan, None
    s = str(v).strip()
    m = _NUM_RE.search(s)
    val = float(m.group().replace(",", "")) if m else np.nan
    unit = _NUM_RE.sub("", s).strip(" .:-") or None
    return val, unit


def canon_sector(v) -> tuple[str, bool]:
    """Returns (canonical_sector, is_reliable)."""
    t = clean_text(v)
    if t is None:
        return "Unknown", False
    key = t.lower()
    if key in _SECTOR_CANON:
        return _SECTOR_CANON[key], True
    if key in _SECTOR_NOT_AN_INDUSTRY:
        return "Others", False
    return t.title(), False


def _stage_code(v) -> str | None:
    t = clean_text(v)
    if not t:
        return None
    m = re.match(r"\s*([A-Oa-o])[.\)]", t)
    return m.group(1).upper() if m else None


def indian_fiscal_quarter(ts) -> str | None:
    """India FY starts 1 April. 'FY26 Q1' = Apr-Jun 2025."""
    if ts is None or pd.isna(ts):
        return None
    ts = pd.Timestamp(ts)
    m, y = ts.month, ts.year
    if m >= 4:
        fy = y + 1
        q = (m - 4) // 3 + 1
    else:
        fy = y
        q = (m + 8) // 3 + 1
    return f"FY{str(fy)[-2:]} Q{q}"


def calendar_quarter(ts) -> str | None:
    if ts is None or pd.isna(ts):
        return None
    ts = pd.Timestamp(ts)
    return f"{ts.year} Q{(ts.month - 1) // 3 + 1}"


# --------------------------------------------------------------------- deals
def clean_deals(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy()

    def col(name: str) -> pd.Series:
        if name in df.columns:
            # loader guarantees plain-object str cells; be defensive anyway
            return df[name].astype(object)
        return pd.Series([None] * len(df), index=df.index, dtype=object)

    out = pd.DataFrame(index=df.index)
    # monday stores the deal name as the item name, not a column, when imported via
    # our script - so fall back to __item_name.
    out["deal_name"] = col("Deal Name").map(clean_text)
    out["deal_name"] = out["deal_name"].where(out["deal_name"].notna(),
                                              col("__item_name").map(clean_text))
    out["owner_code"] = col("Owner code").map(clean_text)
    out["client_code"] = col("Client Code").map(clean_text)

    out["status_raw"] = col("Deal Status").map(clean_text)
    out["stage_raw"] = col("Deal Stage").map(clean_text)
    out["stage_code"] = col("Deal Stage").map(_stage_code)

    stage_info = out["stage_code"].map(lambda c: _STAGE_MAP.get(c))
    out["stage"] = [
        (info[0] if info else (as_text(raw_) or "Unknown"))
        for info, raw_ in zip(stage_info, out["stage_raw"])
    ]

    def resolve_status(row_status, info, stage_raw):
        s = as_text(row_status).lower()
        stage_raw = as_text(stage_raw)
        if info:
            _, won, lost, is_open = info
            if won:
                return "Won"
            if lost:
                return "Lost"
            if info[0] == "Projects On Hold":
                return "On Hold"
        if "won" in s:
            return "Won"
        if "dead" in s or "lost" in s:
            return "Lost"
        if "hold" in s:
            return "On Hold"
        if "open" in s:
            return "Open"
        if stage_raw and "complete" in stage_raw.lower():
            return "Won"
        return "Unknown"

    out["status"] = [
        resolve_status(s, i, sr)
        for s, i, sr in zip(out["status_raw"], stage_info, out["stage_raw"])
    ]
    out["is_won"] = out["status"].eq("Won")
    out["is_lost"] = out["status"].eq("Lost")
    out["is_open"] = out["status"].eq("Open")

    prob = col("Closure Probability").map(clean_text)
    out["probability_band"] = prob.map(lambda p: p.title() if isinstance(p, str) and p else None)
    out["probability_pct"] = prob.map(lambda p: _PROBABILITY_PCT.get(as_text(p).lower(), np.nan))

    out["deal_value"] = col("Masked Deal value").map(parse_money)

    out["created_date"] = col("Created Date").map(parse_date)
    out["tentative_close_date"] = col("Tentative Close Date").map(parse_date)
    out["actual_close_date"] = col("Close Date (A)").map(parse_date)
    # best available close date for period bucketing
    out["close_date"] = out["actual_close_date"].fillna(out["tentative_close_date"])
    out["close_fy_quarter"] = out["close_date"].map(indian_fiscal_quarter)
    out["close_cal_quarter"] = out["close_date"].map(calendar_quarter)
    out["created_fy_quarter"] = out["created_date"].map(indian_fiscal_quarter)

    # expected (probability-weighted) value, only meaningful for open deals
    out["weighted_value"] = np.where(
        out["is_open"], out["deal_value"] * out["probability_pct"], np.nan
    )

    out["product"] = col("Product deal").map(clean_text)
    sec = col("Sector/service").map(canon_sector)
    out["sector"] = sec.map(lambda x: x[0])
    out["sector_reliable"] = sec.map(lambda x: x[1])
    out["sector_raw"] = col("Sector/service").map(clean_text)

    out["is_energy_sector"] = out["sector"].isin(ENERGY_SECTORS)

    # ---- row-level data quality flags -------------------------------------
    flags: list[list[str]] = [[] for _ in range(len(out))]

    def flag(mask: pd.Series, tag: str):
        for i, bad in zip(out.index, mask):
            if bad:
                flags[out.index.get_loc(i)].append(tag)

    header_junk = out["status_raw"].isin(["Deal Status"]) | out["stage_raw"].isin(["Deal Stage"])
    flag(header_junk, "header_row_pasted_into_data")
    flag(out["deal_name"].isna(), "missing_deal_name")
    flag(out["deal_value"].isna() & ~header_junk, "missing_deal_value")
    flag(out["close_date"].isna() & ~header_junk, "missing_close_date")
    flag(~out["sector_reliable"] & ~header_junk, "sector_unmapped_or_not_an_industry")
    flag(out["status"].eq("Unknown") & ~header_junk, "unresolved_deal_status")
    # some values (mostly 'Tender'/'Others') are ~1000x the typical deal and look
    # mis-scaled; flag but don't drop.
    flag(out["deal_value"] >= 1e8, "very_large_value_possible_scale_error")

    # suspected bulk-import artefact: a large block of identical Won/Lead rows
    bulk = (
        out["status"].eq("Won")
        & out["stage_code"].eq("A")
        & out["created_date"].eq(pd.Timestamp("2025-11-27"))
    )
    flag(bulk, "suspected_bulk_import_artifact")
    out["suspected_artifact"] = bulk

    # exact duplicates on the business key
    dup_cols = ["deal_name", "owner_code", "client_code", "stage_raw", "deal_value",
                "tentative_close_date", "created_date", "sector_raw"]
    is_dup = out.duplicated(subset=dup_cols, keep="first")
    flag(is_dup, "exact_duplicate_row")
    out["is_duplicate"] = is_dup

    out["dq_flags"] = flags
    out["is_valid"] = ~header_junk
    return out


# --------------------------------------------------------------------- work orders
def _canon_from_map(v, mapping: dict[str, str], default_title=True):
    t = clean_text(v)
    if t is None:
        return None
    return mapping.get(t.lower(), t.title() if default_title else t)


def clean_work_orders(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy()

    def col(name: str) -> pd.Series:
        s = df[name] if name in df.columns else pd.Series([None] * len(df), index=df.index)
        return s.astype(object).where(s.notna(), None)

    out = pd.DataFrame(index=df.index)
    out["deal_name"] = col("Deal name masked").map(clean_text)
    out["customer_code"] = col("Customer Name Code").map(clean_text)
    # Serial # is the monday item name when imported via our script.
    out["serial"] = col("Serial #").map(clean_text)
    out["serial"] = out["serial"].where(out["serial"].notna(),
                                        col("__item_name").map(clean_text))
    out["owner_code"] = col("BD/KAM Personnel code").map(clean_text)

    out["nature_of_work"] = col("Nature of Work").map(clean_text)
    out["type_of_work"] = col("Type of Work").map(clean_text)

    out["execution_status_raw"] = col("Execution Status").map(clean_text)
    out["execution_status"] = col("Execution Status").map(
        lambda v: _canon_from_map(v, _EXEC_STATUS_CANON) or "Unknown"
    )
    out["is_completed"] = out["execution_status"].eq("Completed")
    out["is_active"] = out["execution_status"].isin(["Ongoing", "Partially Completed"])

    sec = col("Sector").map(canon_sector)
    out["sector"] = sec.map(lambda x: x[0])
    out["sector_reliable"] = sec.map(lambda x: x[1])
    out["sector_raw"] = col("Sector").map(clean_text)
    out["is_energy_sector"] = out["sector"].isin(ENERGY_SECTORS)

    platform = col("Is any Skylark software platform part of the client deliverables in this deal?").map(clean_text)
    out["platform_raw"] = platform
    out["uses_platform"] = platform.map(
        lambda p: bool(p) and p.strip().upper() not in {"NONE", "NO", "N"}
    )

    # dates
    out["po_date"] = col("Date of PO/LOI").map(parse_date)
    out["data_delivery_date"] = col("Data Delivery Date").map(parse_date)
    out["start_date"] = col("Probable Start Date").map(parse_date)
    out["end_date"] = col("Probable End Date").map(parse_date)
    out["last_invoice_date"] = col("Last invoice date").map(parse_date)
    out["collection_date"] = col("Collection Date").map(parse_date)
    out["po_fy_quarter"] = out["po_date"].map(indian_fiscal_quarter)
    out["po_cal_quarter"] = out["po_date"].map(calendar_quarter)

    # money (INR, masked). ex/inc GST pairs kept separately.
    out["order_value_ex_gst"] = col("Amount in Rupees (Excl of GST) (Masked)").map(parse_money)
    out["order_value_inc_gst"] = col("Amount in Rupees (Incl of GST) (Masked)").map(parse_money)
    out["billed_ex_gst"] = col("Billed Value in Rupees (Excl of GST.) (Masked)").map(parse_money)
    out["billed_inc_gst"] = col("Billed Value in Rupees (Incl of GST.) (Masked)").map(parse_money)
    out["collected_inc_gst"] = col("Collected Amount in Rupees (Incl of GST.) (Masked)").map(parse_money)
    out["to_be_billed_ex_gst"] = col("Amount to be billed in Rs. (Exl. of GST) (Masked)").map(parse_money)
    out["to_be_billed_inc_gst"] = col("Amount to be billed in Rs. (Incl. of GST) (Masked)").map(parse_money)
    out["receivable"] = col("Amount Receivable (Masked)").map(parse_money)
    out["ar_priority"] = col("AR Priority account").map(
        lambda v: (clean_text(v) or "").lower() == "priority"
    )

    # quantities (free text with units)
    for src, dst in [
        ("Quantity by Ops", "qty_ops"),
        ("Quantities as per PO", "qty_po"),
        ("Quantity billed (till date)", "qty_billed"),
        ("Balance in quantity", "qty_balance"),
    ]:
        parsed = col(src).map(parse_quantity)
        out[f"{dst}_value"] = parsed.map(lambda x: x[0])
        out[f"{dst}_unit"] = parsed.map(lambda x: x[1])
        out[f"{dst}_raw"] = col(src).map(clean_text)

    out["invoice_status"] = col("Invoice Status").map(clean_text)
    out["billing_status"] = col("Billing Status").map(
        lambda v: _canon_from_map(v, _BILLING_STATUS_CANON)
    )
    out["wo_status"] = col("WO Status (billed)").map(clean_text)
    out["collection_status"] = col("Collection status").map(clean_text)
    out["last_invoice_no"] = col("latest invoice no.").map(clean_text)

    # derived: project duration (days) and simple collection ratio
    out["duration_days"] = (out["end_date"] - out["start_date"]).dt.days
    with np.errstate(invalid="ignore", divide="ignore"):
        out["collection_ratio"] = out["collected_inc_gst"] / out["billed_inc_gst"]

    # ---- data quality flags --------------------------------------------
    flags: list[list[str]] = [[] for _ in range(len(out))]

    def flag(mask: pd.Series, tag: str):
        for pos, bad in enumerate(mask):
            if bad:
                flags[pos].append(tag)

    raw_amount = col("Amount in Rupees (Excl of GST) (Masked)").map(lambda v: str(v).strip())
    flag(raw_amount.map(lambda s: s.startswith("#") or s.upper() in {"#VALUE!", "#REF!", "#N/A"}),
         "amount_is_spreadsheet_error")
    flag(
        col("Amount in Rupees (Excl of GST) (Masked)").map(
            lambda v: str(v).strip() in {"1.2332", "1.455176"}
        ),
        "amount_is_masked_placeholder",
    )
    flag(out["order_value_ex_gst"].isna(), "missing_order_value")
    flag(out["sector"].eq("Unknown"), "missing_sector")
    flag(out["po_date"].isna(), "missing_po_date")
    flag(out["execution_status"].eq("Unknown"), "unresolved_execution_status")
    flag((out["duration_days"].notna()) & (out["duration_days"] < 0), "end_before_start")
    flag(out["deal_name"].isna(), "missing_deal_name")

    dup_cols = ["serial", "deal_name", "customer_code", "po_date", "order_value_ex_gst"]
    is_dup = out.duplicated(subset=dup_cols, keep="first")
    flag(is_dup, "exact_duplicate_row")
    out["is_duplicate"] = is_dup

    out["dq_flags"] = flags
    out["is_valid"] = out["serial"].notna() | out["deal_name"].notna()
    return out
