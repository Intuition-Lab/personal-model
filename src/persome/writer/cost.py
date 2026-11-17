"""LLM USD cost calculator (F4)."""

from __future__ import annotations

import llm_prices

# Anthropic, OpenAI and Google ids are priced from llm-prices (read from each provider's
# pricing page, dated per row). This table covers the rest.
# Per-model pricing: (input, output, cache_read, cache_creation) per 1M tokens, USD.
# Prefix-matched: "claude-sonnet-4-6" matches any model ID that starts with that string.
_COSTS: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-4-7": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
    "gpt-4o-mini": (0.15, 0.6, 0.0, 0.0),
    "gpt-4o": (2.5, 10.0, 0.0, 0.0),
    # DeepSeek family — same pricing whether routed via /v1 (OpenAI shape) or
    # /anthropic gateway. The prefix entry covers both `deepseek-chat`,
    # `deepseek/deepseek-v4-flash`, and `anthropic/deepseek-v4-flash` etc.
    "deepseek-chat": (0.27, 1.1, 0.07, 0.0),
    "deepseek-v4-flash": (0.27, 1.1, 0.07, 0.0),
    "anthropic/deepseek": (0.27, 1.1, 0.07, 0.0),
    "deepseek/": (0.27, 1.1, 0.07, 0.0),
}


def calculate_usd(model: str, usage: dict[str, int]) -> float | None:
    """Return USD cost for the given model and token usage, or None for unknown models."""
    rates = _list_price(model)
    if rates is not None:
        return _usd(rates, usage)
    for prefix, (inp, out, cr, cw) in _COSTS.items():
        if model.startswith(prefix):
            return _usd((inp, out, cr, cw), usage)
    return None


def _list_price(model: str) -> tuple[float, float, float, float] | None:
    p = llm_prices.lookup(model)
    if p is None or p.provider not in ("anthropic", "openai", "google"):
        return None
    if p.input is None or p.output is None:
        return None
    cache_read = p.cache_read if p.cache_read is not None else p.input
    cache_write = p.cache_write.get("5m", p.input)
    return (p.input, p.output, cache_read, cache_write)


def _usd(rates: tuple[float, float, float, float], usage: dict[str, int]) -> float:
    inp, out, cr, cw = rates
    return (
        usage.get("prompt_tokens", 0) * inp / 1_000_000
        + usage.get("completion_tokens", 0) * out / 1_000_000
        + usage.get("cache_read_tokens", 0) * cr / 1_000_000
        + usage.get("cache_creation_tokens", 0) * cw / 1_000_000
    )
