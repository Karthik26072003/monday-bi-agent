"""Tool definitions and dispatch for the BI agent.

Tools are deliberately structured (not "run arbitrary code"):
  * schema / quality inspection
  * a curated metric library (metrics.py)
  * search over distinct column values (helps map fuzzy terms like "energy")
  * a sandboxed pandas expression escape hatch for open-ended questions

All tools are READ ONLY. `query_dataframe` evaluates against a copy with no
builtins, no file/network access, and truncates output.
"""
from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd

from monday_bi import metrics as metrics_mod
from monday_bi import service
from monday_bi.leadership import leadership_brief
from monday_bi.quality import default_analysis_frame

_BOARDS = {"deals": service.get_deals, "work_orders": service.get_work_orders}

_MAX_ROWS_RETURNED = 60


def _df(board) -> pd.DataFrame:
    board = str(board).strip().lower()
    if board not in _BOARDS:
        raise ValueError(f"Unknown board '{board}'. Use one of: {list(_BOARDS)}")
    return _BOARDS[board]()


def _jsonify(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return obj.date().isoformat()
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonify(v) for v in obj]
    return obj


# --------------------------------------------------------------------- schemas
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_board_schema",
        "description": "List the cleaned columns of a board with dtypes, and for categorical "
                       "columns the distinct values. Call this before query_dataframe.",
        "input_schema": {
            "type": "object",
            "properties": {"board": {"type": "string", "enum": ["deals", "work_orders"]}},
            "required": ["board"],
        },
    },
    {
        "name": "get_data_quality_report",
        "description": "Data-quality report for a board (or both if omitted): row counts, "
                       "duplicates/artefacts excluded, null rates, and plain-English caveats "
                       "to communicate to the user.",
        "input_schema": {
            "type": "object",
            "properties": {"board": {"type": "string", "enum": ["deals", "work_orders", "both"]}},
        },
    },
    {
        "name": "compute_metric",
        "description": "Run a vetted BI calculation. Prefer this over query_dataframe when one fits. "
                       "Metrics: pipeline_health, win_rate, revenue_summary, sector_performance, "
                       "accounts_receivable, operations_summary.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": ["pipeline_health", "win_rate", "revenue_summary",
                             "sector_performance", "accounts_receivable", "operations_summary"],
                },
                "sector": {"type": "string", "description": "Sector name, or 'energy' for Renewables+Powerline. Optional."},
                "quarter": {"type": "string", "description": "e.g. 'FY26 Q2' (fiscal) or '2026 Q1' (calendar). Optional."},
            },
            "required": ["metric"],
        },
    },
    {
        "name": "leadership_brief",
        "description": "Structured data spine for a leadership/board update for a fiscal period "
                       "(default: current fiscal quarter): pipeline, wins, delivery, cash, sector "
                       "movement, and data caveats.",
        "input_schema": {
            "type": "object",
            "properties": {"period": {"type": "string", "description": "Fiscal quarter e.g. 'FY26 Q2'. Optional."}},
        },
    },
    {
        "name": "search_column_values",
        "description": "Return distinct values of a column that match a query substring, with row "
                       "counts. Use to map a user's wording to real data (sectors, owners, stages, "
                       "deal names, products).",
        "input_schema": {
            "type": "object",
            "properties": {
                "board": {"type": "string", "enum": ["deals", "work_orders"]},
                "column": {"type": "string"},
                "query": {"type": "string", "description": "Substring to match (case-insensitive). Empty = all values."},
            },
            "required": ["board", "column"],
        },
    },
    {
        "name": "query_dataframe",
        "description": (
            "Escape hatch for open-ended questions. Evaluate ONE pandas expression. "
            "`d` = cleaned, de-duplicated, valid rows (use this by default); `d_all` = every row "
            "incl. junk/duplicates/artefacts. `pd` and `np` are available. "
            "Return a DataFrame, Series, or scalar. Example: "
            "\"d[d.is_open & d.sector.eq('Renewables')].groupby('stage').deal_value.sum()\". "
            "Read only; results truncated to 60 rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "board": {"type": "string", "enum": ["deals", "work_orders"]},
                "expression": {"type": "string"},
                "purpose": {"type": "string", "description": "One line: what you're computing and why."},
            },
            "required": ["board", "expression"],
        },
    },
]


# --------------------------------------------------------------------- handlers
def _h_get_board_schema(board: str) -> dict[str, Any]:
    df = _df(board)
    valid = df[df["is_valid"]] if "is_valid" in df else df
    cols = []
    for c in df.columns:
        s = valid[c]
        if s.map(lambda v: isinstance(v, list)).any():
            cols.append({"column": c, "dtype": "list[str]", "note": "per-row data-quality flags"})
            continue
        entry = {"column": c, "dtype": str(s.dtype), "null_count": int(s.isna().sum())}
        if s.dtype == object and s.nunique(dropna=True) <= 40:
            entry["distinct_values"] = sorted(s.dropna().unique().tolist())
        cols.append(entry)
    return {
        "board": board,
        "row_count": len(df),
        "columns": cols,
        "note": "query_dataframe exposes `d` = de-duplicated valid rows; `d_all` = every row "
                "including junk/duplicates/artefacts.",
    }


def _h_get_data_quality_report(board="both") -> dict[str, Any]:
    board = str(board).strip().lower()
    reports = service.get_quality_reports()
    if board in ("deals", "work_orders"):
        return reports[board]
    return reports


def _opt_str(v) -> str | None:
    """Models sometimes send numbers/bools for string params - coerce safely."""
    if v is None or v == "":
        return None
    return str(v)


def _h_compute_metric(metric: str, sector=None, quarter=None) -> dict[str, Any]:
    metric = str(metric)
    sector, quarter = _opt_str(sector), _opt_str(quarter)
    if metric not in metrics_mod.METRICS:
        return {"error": f"Unknown metric '{metric}'.", "available": list(metrics_mod.METRICS)}
    fn = metrics_mod.METRICS[metric]
    if metric == "sector_performance":
        return fn(service.get_deals(), service.get_work_orders())
    if metric in ("revenue_summary", "accounts_receivable", "operations_summary"):
        kw = {"sector": sector}
        if metric == "revenue_summary":
            kw["quarter"] = quarter
        return fn(service.get_work_orders(), **kw)
    return fn(service.get_deals(), sector=sector, quarter=quarter)


def _h_leadership_brief(period=None) -> dict[str, Any]:
    return leadership_brief(service.get_deals(), service.get_work_orders(), _opt_str(period))


def _h_search_column_values(board: str, column, query="") -> dict[str, Any]:
    column, query = str(column), str(query or "")
    df = _df(board)
    if column not in df.columns:
        return {"error": f"No column '{column}' on {board}.", "available_columns": list(df.columns)}
    if "is_valid" in df.columns:
        df = df[df["is_valid"]]
    counts = df[column].dropna().astype(str).value_counts()
    q = query.lower()
    matches = [{"value": str(v), "rows": int(n)} for v, n in counts.items() if q in str(v).lower()]
    return {"board": board, "column": column, "query": query, "matches": matches[:50]}


_SAFE_GLOBALS = {"__builtins__": {}, "pd": pd, "np": np}


def _h_query_dataframe(board: str, expression, purpose=None) -> dict[str, Any]:
    expression, purpose = str(expression), _opt_str(purpose)
    full = _df(board)
    d = default_analysis_frame(full, board).copy()
    d_all = full.copy()
    banned = ("__", "import", "open(", "eval(", "exec(", "os.", "sys.", "subprocess",
              "globals(", "locals(", "getattr(", "setattr(", "to_csv", "to_pickle", "read_")
    low = expression.lower()
    for b in banned:
        if b in low:
            return {"error": f"Expression rejected: contains '{b}'. This tool is read-only."}
    try:
        result = eval(expression, _SAFE_GLOBALS, {"d": d, "d_all": d_all})  # noqa: S307 - sandboxed above
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "expression": expression}

    out: dict[str, Any] = {"board": board, "expression": expression, "purpose": purpose}
    if isinstance(result, pd.DataFrame):
        out["result_type"] = "DataFrame"
        out["shape"] = list(result.shape)
        out["rows"] = _jsonify(result.head(_MAX_ROWS_RETURNED).to_dict(orient="records"))
        if len(result) > _MAX_ROWS_RETURNED:
            out["truncated"] = f"showing {_MAX_ROWS_RETURNED} of {len(result)} rows"
    elif isinstance(result, pd.Series):
        out["result_type"] = "Series"
        out["length"] = int(len(result))
        out["values"] = _jsonify(result.head(_MAX_ROWS_RETURNED).to_dict())
    else:
        out["result_type"] = "scalar"
        out["value"] = _jsonify(result)
    return out


_HANDLERS = {
    "get_board_schema": _h_get_board_schema,
    "get_data_quality_report": _h_get_data_quality_report,
    "compute_metric": _h_compute_metric,
    "leadership_brief": _h_leadership_brief,
    "search_column_values": _h_search_column_values,
    "query_dataframe": _h_query_dataframe,
}


def dispatch(name: str, tool_input: dict[str, Any]) -> str:
    """Run a tool, always returning a JSON string (errors included)."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool '{name}'."})
    try:
        result = handler(**(tool_input or {}))
    except Exception as exc:  # noqa: BLE001
        result = {"error": f"{type(exc).__name__}: {exc}"}
    return json.dumps(_jsonify(result), default=str)
