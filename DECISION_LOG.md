# Decision Log — Skylark BI Agent

## Key assumptions

**Data semantics**
- **Deals** = the sales pipeline (one row per deal, with duplicates and junk).
  **Work Orders** = execution/billing/collections for won work. They are *different
  grains* and only loosely related.
- The boards **cannot be joined row‑by‑row**: `Client Code` (`COMPANY089`) and
  `Customer Name Code` (`WOCOMPANY_002`) use different schemes. `Deal Name` +
  `Owner code` is the only shared key and it is not unique. Cross‑board answers are
  presented as sector‑level, with the caveat stated.
- All money is **masked INR** — magnitudes are disguised. The agent quotes values
  as‑is and leans on ratios/shares/comparisons, never absolute claims.
- `Masked Deal value` for won deals is mostly blank; billed/collected figures live
  on the Work Orders board. "Revenue" therefore means Work Orders unless the user
  says "won deal value".
- The Work Orders money sentinel **`1.2332` (×1.18 GST = `1.455176`)** is a
  placeholder for withheld amounts, not ₹1.23 — treated as missing.
- **"Energy sector"** is not a value in the data. Mapped to **Renewables + Powerline**;
  the agent confirms this on first use. `Tender` and `DSP` are deal *types*, not
  industries — bucketed as `Others` and flagged.
- **Time periods**: the company is India‑based, so "this quarter" defaults to the
  **Indian fiscal quarter** (Apr–Mar). The agent states the basis and offers the
  calendar quarter.
- Deal status is derived from `Deal Status` **and** `Deal Stage` together (they
  frequently disagree). `Dead` + stages L/N/O → **Lost**; G/H/J/K or
  "Project Completed" → **Won**; M → **On Hold**.
- A block of ~70 identical `Won / A. Lead Generated` rows dated `2025‑11‑27` is
  treated as a **bulk‑import artefact** and excluded from won/revenue metrics by
  default (surfaced as a caveat, still queryable via `d_all`).
- Exact‑duplicate rows are de‑duplicated by default (kept in `d_all`).
- Probability bands → weights **High 0.8 / Medium 0.5 / Low 0.2** for weighted
  pipeline. Stated in every pipeline answer.

**Platform**
- monday.com data is fetched **live** on each conversation and cached 5 min. No CSV
  is hardcoded into the agent path.
- monday integration is **read‑only** (the client has no mutations).

## Trade‑offs and why

| Decision | Alternative | Why this choice |
|---|---|---|
| **monday REST/GraphQL API**, not MCP | monday's hosted MCP server | Fewer moving parts for a hosted Streamlit app; easy to test, page, and back off. Assignment allows either. |
| **Cleaning in a deterministic Python layer** | Let the LLM clean per query | Reproducible, unit‑testable, cheap, and lets us attach a caveat to every lossy transform. The LLM shouldn't be silently guessing that `#VALUE!` = 0. |
| **Curated metric tools + one sandboxed `query_dataframe`** | Pure text‑to‑SQL / free code execution | Vetted metrics give consistent, correct answers to the 80% common questions; the pandas escape hatch (no builtins, no I/O, output truncated, read‑only copy) covers the long tail without a full code sandbox. |
| **Provider‑agnostic agent** speaking the OpenAI chat dialect | Bind to one vendor SDK | Lets the prototype run on a **free** LLM (default **Groq / `llama-3.3-70b-versatile`**, no credit card) and swap to Gemini / OpenRouter / OpenAI with two config lines. Original plan was Claude; dropped to keep the hosted prototype zero‑cost. |
| **Manual tool‑use loop** | A framework (LangChain etc.) | ~40 lines, no dependency risk, and we stream each tool call into the UI for transparency. |
| **Streamlit + Community Cloud** | FastAPI + React on Render | Fastest path to a testable hosted chat link with secret management; the assignment only needs a conversational interface. |
| **Short‑TTL cache** of monday reads | Query every message | Keeps a multi‑turn conversation fast and within API limits while staying "dynamic". "Refresh" button clears it. |
| **Import script** creates boards | Rely on manual CSV import | Reproducible column typing and structure; documented and re‑runnable. |
| Fiscal‑quarter default | Calendar quarter | Matches how an India‑based founder actually thinks; both are computed and offered. |

## How I interpreted "leadership updates"

A **recurring board/investor‑style snapshot** — the numbers a founder needs to walk
into a monthly or quarterly leadership meeting. `leadership_brief` builds the
**data spine** of that update as one structured object:

- headline metrics — open & weighted pipeline, deals won and created in the period,
  work orders opened, billed / collected to date, outstanding receivable;
- pipeline by stage, trailing‑quarter wins, all‑time win rate;
- top sectors by open pipeline and by billing (where growth vs. cash sits);
- delivery health — completion rate, active work orders, median project duration;
- AR ageing buckets;
- the **data caveats** that must travel with the numbers.

The agent turns this into prose/bullets, or into whatever format the user asks for
("email to the board", "3 bullets", "table"). It deliberately stops at the data +
framing — it does not invent narrative or targets that aren't in the data.

## What I'd do differently with more time

- **Fuzzy cross‑board linking** (`Deal Name` + `Owner` + date proximity) with a
  confidence score, to answer "which won deals have no work order yet" properly.
- **Outlier detection on deal value** — several `Tender`/`Others` values look
  mis‑scaled by ~1000×; right now they're only flagged via the unreliable‑sector
  caveat, not quarantined.
- **Charts** in the UI (pipeline funnel, AR ageing, sector bars) alongside the text.
- **Eval set** — 20–30 question/expected‑answer pairs run in CI to catch regressions
  in tool selection and the cleaning layer.
- **People/owner resolution** — map `OWNER_00x` to names if a monday People column
  is available; add per‑owner pipeline and win‑rate views.
- **Streaming token output** for a snappier feel; and, on a stronger model, tighten
  the system prompt (a 70B open model occasionally needs a nudge to always call the
  data‑quality tool before answering).
- **Incremental fetch** using monday `updated_at` instead of full board reads.
- Richer date normalisation tests once real monday data (which may re‑format dates
  on import) is available.
