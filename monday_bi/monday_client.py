"""Thin, read-only monday.com GraphQL client.

Responsibilities:
  * authentication + connection management
  * cursor pagination over items_page / next_items_page
  * retry with backoff on 429 / 5xx / complexity-budget errors

This module never mutates monday data. The only mutations in this repo live in
scripts/import_to_monday.py, which is a one-time setup tool, not part of the agent.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests

from config import config


class MondayError(RuntimeError):
    """Raised for any unrecoverable monday.com API problem."""


@dataclass
class MondayColumn:
    id: str
    title: str
    type: str


@dataclass
class BoardData:
    id: str
    name: str
    columns: list[MondayColumn]
    items: list[dict[str, Any]] = field(default_factory=list)


_ITEMS_QUERY = """
query ($ids: [ID!], $limit: Int!) {
  boards(ids: $ids) {
    id
    name
    columns { id title type }
    items_page(limit: $limit) {
      cursor
      items {
        id
        name
        column_values { id text value column { title } }
      }
    }
  }
}
"""

_NEXT_QUERY = """
query ($cursor: String!, $limit: Int!) {
  next_items_page(cursor: $cursor, limit: $limit) {
    cursor
    items {
      id
      name
      column_values { id text value column { title } }
    }
  }
}
"""

_PING_QUERY = "query { me { id name email } }"


class MondayClient:
    def __init__(
        self,
        token: str | None = None,
        api_url: str | None = None,
        api_version: str | None = None,
        page_size: int = 100,
        max_retries: int = 4,
        timeout: int = 30,
    ):
        self.token = token or config.MONDAY_API_TOKEN
        self.api_url = api_url or config.MONDAY_API_URL
        self.api_version = api_version or config.MONDAY_API_VERSION
        self.page_size = page_size
        self.max_retries = max_retries
        self.timeout = timeout
        if not self.token:
            raise MondayError(
                "MONDAY_API_TOKEN is not set. Add it to Streamlit secrets or the "
                "environment. See README.md."
            )
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": self.token,
                "Content-Type": "application/json",
                "API-Version": self.api_version,
            }
        )

    # ------------------------------------------------------------------ core
    def _execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.post(self.api_url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:  # network problem
                last_err = MondayError(f"Network error talking to monday.com: {exc}")
                self._sleep(attempt)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = MondayError(f"monday.com returned HTTP {resp.status_code}")
                self._sleep(attempt, resp)
                continue
            if resp.status_code == 401:
                raise MondayError("monday.com rejected the API token (HTTP 401).")
            if resp.status_code != 200:
                raise MondayError(f"monday.com HTTP {resp.status_code}: {resp.text[:500]}")

            body = resp.json()
            errors = body.get("errors") or body.get("error_message")
            if errors:
                text = str(errors)
                # complexity / rate budget problems are transient -> retry
                if "complexity" in text.lower() or "rate limit" in text.lower():
                    last_err = MondayError(f"monday.com throttled the request: {text}")
                    self._sleep(attempt)
                    continue
                raise MondayError(f"monday.com GraphQL error: {text}")
            return body["data"]

        raise last_err or MondayError("monday.com request failed after retries.")

    @staticmethod
    def _sleep(attempt: int, resp: requests.Response | None = None) -> None:
        delay = min(2 ** attempt, 30)
        if resp is not None:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, int(float(retry_after)))
                except ValueError:
                    pass
        time.sleep(delay)

    # --------------------------------------------------------------- public
    def ping(self) -> dict[str, Any]:
        """Return the authenticated user, or raise MondayError."""
        data = self._execute(_PING_QUERY)
        me = (data or {}).get("me")
        if not me:
            raise MondayError("monday.com did not return an authenticated user.")
        return me

    def fetch_board(self, board_id: str | int) -> BoardData:
        """Fetch a whole board (schema + every item) with pagination."""
        if not board_id:
            raise MondayError("No board id provided.")
        data = self._execute(_ITEMS_QUERY, {"ids": [str(board_id)], "limit": self.page_size})
        boards = data.get("boards") or []
        if not boards:
            raise MondayError(
                f"Board {board_id} not found or the token has no access to it."
            )
        board = boards[0]
        columns = [MondayColumn(c["id"], c["title"], c["type"]) for c in board["columns"]]
        page = board["items_page"]
        items = list(page["items"])
        cursor = page.get("cursor")

        while cursor:
            nxt = self._execute(_NEXT_QUERY, {"cursor": cursor, "limit": self.page_size})
            npage = nxt["next_items_page"]
            items.extend(npage["items"])
            cursor = npage.get("cursor")

        return BoardData(id=str(board["id"]), name=board["name"], columns=columns, items=items)
