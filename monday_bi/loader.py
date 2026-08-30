"""Turn a monday.com board (or a CSV, for dev/tests) into a raw pandas DataFrame.

"Raw" means: one row per record, columns named exactly as the source headers,
every value a string or NaN. All interpretation happens later in cleaning.py, so
this layer stays dumb and identical regardless of where the bytes came from.
"""
from __future__ import annotations

import csv
import io

import pandas as pd

from monday_bi.monday_client import BoardData, MondayClient


def _norm_frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    """Every cell a plain Python str (never NaN, pd.NA, or an Arrow scalar), every
    column plain `object` dtype. The cleaning layer relies on this - Arrow-backed
    string columns (which some pandas builds infer) leave NaN in `.map()` results."""
    df = pd.DataFrame(rows, columns=columns, dtype=object)
    for c in df.columns:
        vals = [
            "" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v)
            for v in df[c].tolist()
        ]
        df[c] = pd.Series(vals, index=df.index, dtype=object)  # dtype=object beats infer_string
    return df.reset_index(drop=True)


def board_to_dataframe(board: BoardData) -> pd.DataFrame:
    """monday items -> DataFrame keyed by column *title* (matches the CSV headers)."""
    rows: list[dict[str, str]] = []
    col_titles = [c.title for c in board.columns]
    for item in board.items:
        cvs = item.get("column_values", []) or []
        by_title = {
            (cv.get("column") or {}).get("title", ""): (cv.get("text") or "")
            for cv in cvs
        }
        row = {"__item_id": str(item.get("id") or ""), "__item_name": str(item.get("name") or "")}
        for title in col_titles:
            row[title] = by_title.get(title, "")
        rows.append(row)
    return _norm_frame(rows, ["__item_id", "__item_name", *col_titles])


def fetch_board_dataframe(board_id: str | int, client: MondayClient | None = None) -> pd.DataFrame:
    client = client or MondayClient()
    return board_to_dataframe(client.fetch_board(board_id))


# --------------------------------------------------------------------- CSV (dev only)
def _detect_header_row(path: str, markers: tuple[str, ...]) -> int:
    """Some exports have blank/garbage leading rows. Find the real header."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        for idx, row in enumerate(reader):
            joined = ",".join(cell.strip() for cell in row)
            if any(m.lower() in joined.lower() for m in markers):
                return idx
    return 0


def csv_to_dataframe(path: str, header_markers: tuple[str, ...]) -> pd.DataFrame:
    """Load a source CSV into the same 'raw' shape as board_to_dataframe.

    DEV/TEST ONLY. The hosted agent never calls this - it always queries monday.
    """
    skip = _detect_header_row(path, header_markers)
    df = pd.read_csv(
        path,
        skiprows=skip,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )
    df.columns = [c.strip() for c in df.columns]
    # drop fully-empty columns that trailing commas create
    df = df.loc[:, [c for c in df.columns if c != ""]]
    df.insert(0, "__item_name", "")
    df.insert(0, "__item_id", "")
    return df.reset_index(drop=True)


def deals_csv(path: str) -> pd.DataFrame:
    return csv_to_dataframe(path, ("Deal Name", "Deal Stage"))


def work_orders_csv(path: str) -> pd.DataFrame:
    return csv_to_dataframe(path, ("Serial #", "Nature of Work", "Deal name masked"))
