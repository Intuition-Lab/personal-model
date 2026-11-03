"""Tests for run_tool_loop harness: retry, context trim, token tracking."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from persome.config import Config, WriterConfig
from persome.writer import llm as llm_mod


def _make_cfg(**writer_kwargs) -> Config:
    return Config(writer=WriterConfig(**writer_kwargs))


@dataclass
class _State:
    committed: bool = False


def _noop_dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────


def test_run_tool_loop_retries_on_llm_failure(monkeypatch):
    calls: list[int] = []

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise RuntimeError("transient network error")
        return llm_mod._build_response("")  # no tool calls → loop exits cleanly

    cfg = _make_cfg(llm_retry_attempts=2)
    monkeypatch.setattr(llm_mod, "call_llm", _stub)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
    ]
    state = _State()

    result = llm_mod.run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=_noop_dispatch,
        valid_tool_names=set(),
        state=state,
        max_iter=3,
    )

    assert len(calls) == 2, "\u7b2c\u4e00\u6b21\u5931\u8d25\u540e\u5e94\u91cd\u8bd5\u4e00\u6b21"
    assert result == 3, "loop \u4ee5 max_iter \u6b63\u5e38\u7ed3\u675f"


# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────


def test_run_tool_loop_trims_context_when_over_limit(monkeypatch):

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        return llm_mod._build_response("")

    cfg = _make_cfg(context_token_limit=50, llm_retry_attempts=1)
    monkeypatch.setattr(llm_mod, "call_llm", _stub)

    fat = "x" * 200
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": fat},
        {"role": "user", "content": fat},
        {"role": "assistant", "content": fat},
        {"role": "tool", "content": fat, "tool_call_id": "id1", "name": "t"},
        {"role": "assistant", "content": fat},
        {"role": "tool", "content": fat, "tool_call_id": "id2", "name": "t"},
    ]
    original_len = len(messages)
    state = _State()

    llm_mod.run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=_noop_dispatch,
        valid_tool_names=set(),
        state=state,
        max_iter=1,
    )

    assert len(messages) < original_len, (
        "\u8d85\u9650\u65f6\u6d88\u606f\u5386\u53f2\u5e94\u88ab\u539f\u5730\u622a\u77ed"
    )

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"


# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────


def test_run_tool_loop_logs_token_usage(monkeypatch):

    class _Usage:
        prompt_tokens = 42
        completion_tokens = 17
        total_tokens = 59

    def _stub(cfg, stage, *, messages, tools=None, json_mode=False):
        resp = llm_mod._build_response("")
        resp.usage = _Usage()
        return resp

    cfg = _make_cfg(llm_retry_attempts=1)
    monkeypatch.setattr(llm_mod, "call_llm", _stub)

    logged: list[str] = []
    monkeypatch.setattr(llm_mod.logger, "info", lambda fmt, *args: logged.append(fmt % args))

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
    ]
    state = _State()

    llm_mod.run_tool_loop(
        cfg,
        "classifier",
        messages,
        tools=[],
        dispatch_fn=_noop_dispatch,
        valid_tool_names=set(),
        state=state,
        max_iter=2,
    )

    log_text = " ".join(logged)
    assert "prompt_tokens" in log_text
    assert "42" in log_text


# ──────────────────────────────────────────────────────────────
# Provider extensions on tool calls (#126): Gemini thought
# signatures (extra_content) and DeepSeek reasoning_content
# must survive the loop and reach the next request verbatim.
# ──────────────────────────────────────────────────────────────

_EXTRA_CONTENT = {"google": {"thought_signature": "sig-from-first-reply"}}


def _signed_tool_response():
    from types import SimpleNamespace

    fn = SimpleNamespace(name="search_memory", arguments='{"query": "morning routine"}')
    call = SimpleNamespace(
        id="function-call-search_memory", function=fn, extra_content=_EXTRA_CONTENT
    )
    msg = SimpleNamespace(content=None, tool_calls=[call], reasoning_content="thinking it over")
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="tool_calls")],
        usage=None,
    )


def test_extract_tool_calls_preserves_extra_content():
    calls = llm_mod.extract_tool_calls(_signed_tool_response())

    assert calls == [
        {
            "id": "function-call-search_memory",
            "name": "search_memory",
            "arguments": {"query": "morning routine"},
            "extra_content": _EXTRA_CONTENT,
        }
    ]


def test_extract_tool_calls_preserves_extra_content_from_sdk_object():
    from openai.types.chat import ChatCompletion

    completion = ChatCompletion.model_validate(
        {
            "id": "chatcmpl-1",
            "created": 0,
            "model": "gemini-3.5-flash",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "function-call-search_memory",
                                "type": "function",
                                "function": {
                                    "name": "search_memory",
                                    "arguments": '{"query": "morning routine"}',
                                },
                                "extra_content": _EXTRA_CONTENT,
                            }
                        ],
                    },
                }
            ],
        }
    )

    calls = llm_mod.extract_tool_calls(completion)

    assert calls[0]["extra_content"] == _EXTRA_CONTENT


def test_extract_tool_calls_without_extra_content_is_unchanged():
    from types import SimpleNamespace

    fn = SimpleNamespace(name="search_memory", arguments="{}")
    call = SimpleNamespace(id="c0", function=fn)
    msg = SimpleNamespace(content=None, tool_calls=[call])
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="tool_calls")])

    assert llm_mod.extract_tool_calls(resp) == [
        {"id": "c0", "name": "search_memory", "arguments": {}}
    ]


def _run_signed_loop(monkeypatch) -> list[dict[str, Any]]:
    responses = iter([_signed_tool_response(), llm_mod._build_response("done")])
    monkeypatch.setattr(llm_mod, "call_llm", lambda cfg, stage, **kw: next(responses))
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "morning routine?"},
    ]
    llm_mod.run_tool_loop(
        _make_cfg(llm_retry_attempts=1),
        "classifier",
        messages,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "search_memory",
                    "description": "search",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        dispatch_fn=_noop_dispatch,
        valid_tool_names={"search_memory"},
        state=_State(),
        max_iter=3,
    )
    return messages


def test_run_tool_loop_keeps_extra_content_on_rebuilt_tool_call(monkeypatch):
    messages = _run_signed_loop(monkeypatch)

    assistant = next(m for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))
    assert assistant["tool_calls"][0]["extra_content"] == _EXTRA_CONTENT


def test_to_openai_messages_sends_back_extra_content_and_reasoning_content(
    monkeypatch,
):
    messages = _run_signed_loop(monkeypatch)

    wire = llm_mod._to_openai_messages(messages)
    assistant = next(m for m in wire if m.get("role") == "assistant" and m.get("tool_calls"))
    assert assistant["tool_calls"][0]["extra_content"] == _EXTRA_CONTENT
    assert assistant["reasoning_content"] == "thinking it over"


def test_to_openai_messages_without_reasoning_content_is_unchanged():
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c0",
                    "type": "function",
                    "function": {"name": "search_memory", "arguments": "{}"},
                }
            ],
        },
    ]

    wire = llm_mod._to_openai_messages(messages)

    assert wire[1] == messages[1]
    assert "reasoning_content" not in wire[1]
    assert "extra_content" not in wire[1]["tool_calls"][0]
