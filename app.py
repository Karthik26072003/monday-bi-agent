"""Skylark BI Agent - conversational Streamlit front end."""
from __future__ import annotations

import json

import streamlit as st

from config import config
from monday_bi import service
from monday_bi.agent import AgentError, AgentReply, ToolCall, run

st.set_page_config(page_title="Skylark BI Agent", page_icon="📊", layout="centered")

EXAMPLES = [
    "How's our pipeline looking for the energy sector this quarter?",
    "What's our win rate, by count and by value?",
    "How much have we billed vs collected, and where's the receivable risk?",
    "Give me a leadership update for the current quarter.",
    "Which sectors are pulling their weight and which aren't?",
]


# --------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Connection")
    health = service.health_check()
    ok = health.get("ok")
    (st.success if ok else st.error)(health.get("monday", "unknown"))
    if not ok and health.get("error"):
        st.error(health["error"])
        if health.get("traceback"):
            with st.expander("Traceback"):
                st.code(health["traceback"])
    if health.get("data_source") == "csv":
        st.warning("Running in **dev CSV mode** - not live monday data.")
    cols = st.columns(2)
    cols[0].metric("Deals", health.get("deals_rows", "—"))
    cols[1].metric("Work orders", health.get("work_orders_rows", "—"))
    st.caption(f"Model: `{health.get('model')}`  ·  cache TTL {config.CACHE_TTL_SECONDS}s")
    if st.button("🔄 Refresh data from monday.com"):
        service.clear_cache()
        st.rerun()

    if ok:
        with st.expander("Data quality"):
            for board, rep in service.get_quality_reports().items():
                st.markdown(f"**{board}** — {rep['rows_used_for_analysis_by_default']} "
                            f"of {rep['rows_total']} rows used")
                for c in rep["caveats"]:
                    st.caption(f"• {c}")

    st.divider()
    if st.button("Clear conversation"):
        st.session_state.clear()
        st.rerun()


# --------------------------------------------------------------------- state
if "messages" not in st.session_state:
    st.session_state.messages = []      # OpenAI-format chat history
if "display" not in st.session_state:
    st.session_state.display = []       # [{role, text, tools:[...]}]

st.title("📊 Skylark BI Agent")
st.caption("Ask founder-level questions across the Deals and Work Orders boards. "
           "Monetary values are masked INR.")

if not st.session_state.display:
    st.markdown("**Try one of these:**")
    for ex in EXAMPLES:
        if st.button(ex, key=f"ex_{hash(ex)}"):
            st.session_state.pending = ex
            st.rerun()


def _render_tool(call: ToolCall):
    with st.expander(f"🔧 `{call.name}`  {json.dumps(call.input, default=str)[:120]}"):
        try:
            st.json(json.loads(call.result))
        except Exception:
            st.code(call.result)


for turn in st.session_state.display:
    with st.chat_message(turn["role"]):
        for call in turn.get("tools", []):
            _render_tool(call)
        st.markdown(turn["text"])


# --------------------------------------------------------------------- input
prompt = st.chat_input("Ask about pipeline, revenue, sectors, receivables…")
if "pending" in st.session_state:
    prompt = st.session_state.pop("pending")

if prompt:
    st.session_state.display.append({"role": "user", "text": prompt, "tools": []})
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        status = st.status("Thinking…", expanded=True)
        seen: list[ToolCall] = []
        final: AgentReply | None = None
        try:
            for event in run(st.session_state.messages):
                if isinstance(event, ToolCall):
                    seen.append(event)
                    status.update(label=f"Running `{event.name}`…")
                    _render_tool(event)
                elif isinstance(event, AgentReply):
                    final = event
        except AgentError as exc:
            status.update(label="Error", state="error")
            st.error(str(exc))
            final = None

        if final is not None:
            status.update(label=f"Done · {len(seen)} tool call(s)", state="complete")
            st.markdown(final.text)
            st.session_state.messages = final.messages
            st.session_state.display.append(
                {"role": "assistant", "text": final.text, "tools": seen}
            )
        else:
            st.session_state.display.append(
                {"role": "assistant", "text": "_Something went wrong - see the error above._", "tools": seen}
            )
