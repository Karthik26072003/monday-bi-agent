"""Tool-use agent loop over an OpenAI-compatible chat API.

Provider-agnostic on purpose: it talks the OpenAI chat-completions dialect, so it
works with Groq, Google Gemini (OpenAI-compat endpoint), OpenRouter, Cerebras,
Together, a local Ollama, or OpenAI itself - whichever the user has a (free) key
for. Configure LLM_BASE_URL / LLM_MODEL / LLM_API_KEY.

Manual loop (no framework) so we can stream each tool call into the Streamlit UI.
Stateless: the caller owns the message history (OpenAI message format).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator

from openai import APIConnectionError, APIStatusError, OpenAI

from config import config
from monday_bi.prompts import system_prompt
from monday_bi.tools import TOOL_SCHEMAS, dispatch


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]
    result: str


@dataclass
class AgentReply:
    text: str
    tool_calls: list[ToolCall]
    messages: list[dict[str, Any]]      # updated history to persist
    stopped_early: bool = False


class AgentError(RuntimeError):
    pass


# our TOOL_SCHEMAS use `input_schema`; OpenAI wants function/parameters wrapping
_OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    }
    for t in TOOL_SCHEMAS
]


def _client() -> OpenAI:
    if not config.LLM_API_KEY:
        raise AgentError(
            "No LLM API key set. Add LLM_API_KEY to .streamlit/secrets.toml "
            "(free options: Groq, Google Gemini, OpenRouter - see README)."
        )
    return OpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL)


def _complete(client: OpenAI, history: list[dict[str, Any]], *, use_tools: bool = True):
    try:
        return client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=history,
            tools=_OPENAI_TOOLS if use_tools else None,
            temperature=0.2,
            max_tokens=2048,
        )
    except APIStatusError as exc:
        raise AgentError(f"LLM API error ({exc.status_code}): {exc.message}") from exc
    except APIConnectionError as exc:
        raise AgentError(f"Could not reach the LLM API: {exc}") from exc


def run(messages: list[dict[str, Any]]) -> Iterator[Any]:
    """Yield ToolCall objects as they happen, then a final AgentReply.

    `messages` is the running conversation (OpenAI format), without the system
    prompt - it is prepended here. The list is copied, not mutated.
    """
    client = _client()
    history: list[dict[str, Any]] = [{"role": "system", "content": system_prompt()}]
    # drop any stale system message the caller kept
    history += [dict(m) for m in messages if m.get("role") != "system"]

    tool_calls: list[ToolCall] = []

    for step in range(config.MAX_AGENT_STEPS):
        resp = _complete(client, history)
        msg = resp.choices[0].message

        assistant_entry: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        history.append(assistant_entry)

        if not msg.tool_calls:
            text = (msg.content or "").strip() or "(no text response)"
            yield AgentReply(text, tool_calls, _strip_system(history))
            return

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = dispatch(tc.function.name, args)
            call = ToolCall(tc.function.name, args, result)
            tool_calls.append(call)
            yield call
            history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    # hit the step limit - force a final answer with no tools
    history.append({
        "role": "system",
        "content": "Tool-call limit reached. Answer now with what you have and note any gaps.",
    })
    try:
        resp = _complete(client, history, use_tools=False)
        text = (resp.choices[0].message.content or "").strip()
    except AgentError as exc:
        text = f"(Stopped after {config.MAX_AGENT_STEPS} tool calls: {exc})"
    yield AgentReply(text or "(no answer)", tool_calls, _strip_system(history), stopped_early=True)


def _strip_system(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in history if m.get("role") != "system"]


def ask(messages: list[dict[str, Any]]) -> AgentReply:
    reply: AgentReply | None = None
    for event in run(messages):
        if isinstance(event, AgentReply):
            reply = event
    assert reply is not None
    return reply
