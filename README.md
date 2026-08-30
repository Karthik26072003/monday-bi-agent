# Skylark BI Agent

A conversational AI agent that answers founder‑level business‑intelligence questions
across two monday.com boards — **Deals** (sales pipeline) and **Work Orders**
(delivery, billing, collections). It reads monday.com dynamically over the API,
cleans the (deliberately messy) data in a deterministic layer, and uses an LLM
(through a provider‑agnostic OpenAI‑compatible API) with a set of read‑only BI
tools to interpret questions, run the analysis, and explain the result with caveats.

**Live prototype:** _<paste your Streamlit Cloud URL here>_

---

## Architecture

```
                 ┌────────────────────────────────────────────┐
   user ───────► │  Streamlit chat UI  (app.py)               │
                 │   • connection status, data‑quality panel  │
                 │   • streams tool calls as they happen      │
                 └───────────────────────┬────────────────────┘
                                         │
                 ┌───────────────────────▼────────────────────┐
                 │  Agent loop  (monday_bi/agent.py)          │
                 │   LLM + manual tool‑use loop (OpenAI‑compat)│
                 └───────────────────────┬────────────────────┘
                                         │  read‑only tools (monday_bi/tools.py)
   ┌─────────────────────────────────────┼───────────────────────────────────┐
   │ get_board_schema  get_data_quality_report  compute_metric               │
   │ leadership_brief  search_column_values     query_dataframe (sandboxed)  │
   └─────────────────────────────────────┬───────────────────────────────────┘
                                         │
                 ┌───────────────────────▼────────────────────┐
                 │  service.py   fetch → clean → cache (TTL)  │
                 ├────────────────────────────────────────────┤
                 │  monday_client.py  GraphQL, auth, paging,   │
                 │                    retry/backoff (READ ONLY)│
                 │  cleaning.py       dates · money · sectors ·│
                 │                    stages · dedupe · flags  │
                 │  quality.py        per‑board caveat report  │
                 │  metrics.py        pipeline · win rate ·    │
                 │                    revenue · AR · ops       │
                 │  leadership.py     leadership‑update spine  │
                 └───────────────────────┬────────────────────┘
                                         │
                                monday.com  (Deals board, Work Orders board)
```

Key ideas:

* **Cleaning is code, not the LLM** — deterministic, unit‑tested, and every
  transformation that could mask a problem also emits a caveat the agent surfaces.
* **Structured tools first, escape hatch second** — a vetted metric library covers
  the common questions; `query_dataframe` runs a single sandboxed pandas expression
  for anything open‑ended.
* **Dynamic reads, short‑TTL cache** — the agent always queries monday.com; results
  are cached for 5 minutes so a conversation is fast and API‑friendly. No CSV data
  is hardcoded into the agent.
* **Read‑only** — the monday client has no mutation methods. The only writes in the
  repo are in `scripts/import_to_monday.py`, a one‑time setup tool.

---

## Repository layout

| Path | Purpose |
|---|---|
| `app.py` | Streamlit conversational UI |
| `config.py` | Config from Streamlit secrets / env vars |
| `monday_bi/monday_client.py` | Read‑only monday.com GraphQL client |
| `monday_bi/loader.py` | monday items → raw DataFrame (+ CSV loader for tests) |
| `monday_bi/cleaning.py` | Normalisation + per‑row data‑quality flags |
| `monday_bi/quality.py` | Data‑quality report and default analysis frame |
| `monday_bi/metrics.py` | Reusable BI calculations |
| `monday_bi/leadership.py` | "Leadership update" data spine |
| `monday_bi/tools.py` | Tool schemas + dispatch |
| `monday_bi/agent.py` | LLM tool‑use loop (OpenAI‑compatible; provider‑agnostic) |
| `monday_bi/prompts.py` | System prompt |
| `scripts/import_to_monday.py` | One‑time: create boards + import the CSVs |
| `tests/test_cleaning.py` | Cleaning‑layer tests against the real sample data |
| `sample_data/` | The two provided CSVs (used only by tests / the import script) |

---

## Setup

### 1. Prerequisites

* Python 3.11+
* A free LLM API key — **Groq** (default, `https://console.groq.com/keys`, no card),
  or Google Gemini / OpenRouter
* A monday.com account (the 14‑day trial has API access)

### 2. Install

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Get a monday.com API token

monday.com → click your avatar → **Administration → Connections → API** →
copy your **personal API token** (v2).

### 4. Import the sample data into monday.com

This creates two boards and uploads every row. Column types are set sensibly
(status / date / numbers / text); the agent tolerates any types because the
cleaning layer reads each column's display text.

The script reads `MONDAY_API_TOKEN` from `.streamlit/secrets.toml` (or the
environment), so just set that first (step 5), then:

```bash
python scripts/import_to_monday.py            # add --workspace-id 123456 to target a workspace
```

It prints the two board IDs at the end:

```
MONDAY_DEALS_BOARD_ID = "1234567890"
MONDAY_WORK_ORDERS_BOARD_ID = "1234567891"
```

*(Alternatively, use monday's built‑in “Import from Excel/CSV”. For the Work Orders
file, the real header is the 2nd row — delete the blank first row before importing.)*

### 5. Configure secrets

Copy `.streamlit/secrets.toml.example` → `.streamlit/secrets.toml` and fill in:

```toml
LLM_API_KEY = "gsk_..."                       # Groq key (free)
LLM_BASE_URL = "https://api.groq.com/openai/v1"
LLM_MODEL = "llama-3.3-70b-versatile"
MONDAY_API_TOKEN = "your-token"
MONDAY_DEALS_BOARD_ID = "1234567890"           # filled after step 4
MONDAY_WORK_ORDERS_BOARD_ID = "1234567891"
```

Do steps 5 (token + LLM key) and 4 (import) in that order — the import script
needs the monday token. The board IDs go in after the import prints them.

### 6. Run

```bash
streamlit run app.py
```

---

## Deploy to Streamlit Community Cloud

1. Push this repo to GitHub.
2. [share.streamlit.io](https://share.streamlit.io) → **New app** → pick the repo,
   branch, and `app.py`. Set Python to 3.11+ under *Advanced settings*.
3. **Settings → Secrets**: paste the same keys as in `secrets.toml`.
4. Deploy. The app is testable with no local setup — just the URL.

To re‑point at fresh boards later, edit the board‑ID secrets and reboot the app.

---

## Testing

```bash
pytest -q
```

The tests run the cleaning layer against the real messy CSVs (masked money
placeholders, `#VALUE!` cells, pasted header rows, duplicates, the bulk‑import
block, unit‑laden quantities, fiscal‑quarter maths).

### Local dev without monday.com

Set `SKY_DATA_SOURCE=csv` to read `sample_data/*.csv` instead of the API. This is
**dev only** — the sidebar shows a warning and the hosted app never uses it.

---

## Example questions

* “How's our pipeline looking for the energy sector this quarter?”
* “What's our win rate, by count and by value?”
* “How much have we billed vs collected, and where's the receivable risk?”
* “Give me a leadership update for FY26 Q3.”
* “Which sectors are pulling their weight and which aren't?”
* “Any deals stuck in negotiation for a long time?”

See `DECISION_LOG.md` for assumptions, trade‑offs, and the “leadership update”
interpretation.
