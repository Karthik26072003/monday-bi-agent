"""One-time setup: create the two monday.com boards and import the sample CSVs.

This is NOT part of the agent. The agent is read-only and queries monday
dynamically; this script just gets the sample data into monday once.

Usage:
    export MONDAY_API_TOKEN=...            # or set it in the environment / secrets
    python scripts/import_to_monday.py --workspace-id 1234567

    # then print the two board ids it created and put them in your config:
    #   MONDAY_DEALS_BOARD_ID / MONDAY_WORK_ORDERS_BOARD_ID

Re-running creates NEW boards. Delete old ones in the monday UI if you re-import.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monday_bi.loader import deals_csv, work_orders_csv  # noqa: E402
from monday_bi.monday_client import MondayClient, MondayError  # noqa: E402

# column title -> monday column_type. Anything not listed is created as "text",
# which is deliberate: the cleaning layer reads the display text of every column,
# so exact types don't matter for the agent - these are just nicer in the UI.
DEALS_TYPES = {
    "Deal Status": "status",
    "Deal Stage": "status",
    "Closure Probability": "status",
    "Sector/service": "status",
    "Masked Deal value": "numbers",
    "Close Date (A)": "date",
    "Tentative Close Date": "date",
    "Created Date": "date",
}
WORK_ORDER_TYPES = {
    "Execution Status": "status",
    "Sector": "status",
    "Nature of Work": "status",
    "Document Type": "status",
    "Invoice Status": "status",
    "Billing Status": "status",
    "AR Priority account": "status",
    "Date of PO/LOI": "date",
    "Data Delivery Date": "date",
    "Probable Start Date": "date",
    "Probable End Date": "date",
    "Last invoice date": "date",
    "Collection Date": "date",
    "Amount in Rupees (Excl of GST) (Masked)": "numbers",
    "Amount in Rupees (Incl of GST) (Masked)": "numbers",
    "Billed Value in Rupees (Excl of GST.) (Masked)": "numbers",
    "Billed Value in Rupees (Incl of GST.) (Masked)": "numbers",
    "Collected Amount in Rupees (Incl of GST.) (Masked)": "numbers",
    "Amount Receivable (Masked)": "numbers",
}

_CREATE_BOARD = """
mutation ($name: String!, $ws: ID) {
  create_board (board_name: $name, board_kind: public, workspace_id: $ws) { id }
}
"""
_CREATE_COLUMN = """
mutation ($board: ID!, $title: String!, $type: ColumnType!) {
  create_column (board_id: $board, title: $title, column_type: $type) { id title }
}
"""
ITEM_BATCH = 12  # create this many items per GraphQL request (via aliases)


def _throttle(fn, *a, **k):
    for attempt in range(6):
        try:
            return fn(*a, **k)
        except MondayError as exc:
            if "complexity" in str(exc).lower() or "429" in str(exc):
                time.sleep(2 ** attempt)
                continue
            raise
    raise MondayError("giving up after repeated throttling")


def _row_values(row, col_ids: dict[str, str], type_map: dict[str, str]) -> dict[str, object]:
    vals: dict[str, object] = {}
    for title, cid in col_ids.items():
        raw = str(row.get(title, "") or "").strip()
        if not raw:
            continue
        ctype = type_map.get(title, "text")
        if ctype == "numbers":
            try:
                vals[cid] = float(raw.replace(",", ""))
            except ValueError:
                continue
        elif ctype == "date":
            iso = _to_iso(raw)
            if iso:
                vals[cid] = {"date": iso}
        elif ctype == "status":
            vals[cid] = {"label": raw[:40]}
        else:
            vals[cid] = raw
    return vals


def _batch_create_query(n: int) -> str:
    args = ["$board: ID!"]
    body = []
    for i in range(n):
        args += [f"$n{i}: String!", f"$c{i}: JSON!"]
        body.append(
            f'  i{i}: create_item(board_id: $board, item_name: $n{i}, '
            f'column_values: $c{i}, create_labels_if_missing: true) {{ id }}'
        )
    return "mutation (" + ", ".join(args) + ") {\n" + "\n".join(body) + "\n}"


def build_board(client: MondayClient, name: str, df, type_map: dict[str, str],
                name_col: str, workspace_id: str | None) -> str:
    import json as _json

    data = _throttle(client._execute, _CREATE_BOARD, {"name": name, "ws": workspace_id})
    board_id = data["create_board"]["id"]
    print(f"  created board '{name}' -> {board_id}")

    col_ids: dict[str, str] = {}
    for title in df.columns:
        if title in ("__item_id", "__item_name") or title == name_col:
            continue
        ctype = type_map.get(title, "text")
        res = _throttle(client._execute, _CREATE_COLUMN,
                        {"board": board_id, "title": title, "type": ctype})
        col_ids[title] = res["create_column"]["id"]
    print(f"  created {len(col_ids)} columns")

    rows = list(df.iterrows())
    done = 0
    for start in range(0, len(rows), ITEM_BATCH):
        chunk = rows[start:start + ITEM_BATCH]
        variables: dict[str, object] = {"board": board_id}
        for i, (n, row) in enumerate(chunk):
            item_name = str(row.get(name_col, "") or f"Row {start + i + 1}").strip()[:255]
            variables[f"n{i}"] = item_name or f"Row {start + i + 1}"
            variables[f"c{i}"] = _json.dumps(_row_values(row, col_ids, type_map))
        _throttle(client._execute, _batch_create_query(len(chunk)), variables)
        done += len(chunk)
        print(f"    ...{done}/{len(rows)} rows")
    print(f"  imported {done} rows")
    return board_id


def _to_iso(s: str) -> str | None:
    from dateutil import parser as p
    try:
        return p.parse(s, dayfirst=False).date().isoformat()
    except (ValueError, OverflowError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace-id", default=None, help="monday workspace id (optional)")
    ap.add_argument("--deals-csv", default="sample_data/deals.csv")
    ap.add_argument("--work-orders-csv", default="sample_data/work_orders.csv")
    args = ap.parse_args()

    client = MondayClient()
    me = client.ping()
    print(f"authenticated as {me['name']} <{me['email']}>")

    deals = deals_csv(args.deals_csv)
    wos = work_orders_csv(args.work_orders_csv)
    print(f"loaded {len(deals)} deal rows, {len(wos)} work-order rows")

    deals_board = build_board(client, "BI Agent - Deals", deals, DEALS_TYPES,
                              "Deal Name", args.workspace_id)
    wo_board = build_board(client, "BI Agent - Work Orders", wos, WORK_ORDER_TYPES,
                           "Serial #", args.workspace_id)

    print("\nDONE. Put these in your Streamlit secrets / environment:")
    print(f"  MONDAY_DEALS_BOARD_ID = \"{deals_board}\"")
    print(f"  MONDAY_WORK_ORDERS_BOARD_ID = \"{wo_board}\"")


if __name__ == "__main__":
    main()
