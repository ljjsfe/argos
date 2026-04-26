"""Unified LLM adapter. Supports moonshot, dashscope (Qwen), openai, deepseek, anthropic."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from .types import LLMUsage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: str
    base_url: str | None = None
    max_tokens: int = 8192
    temperature: float = 0.0
    context_window: int = 262_144  # Model's max input token limit


class LLMClient:
    """Single interface for all LLM providers."""

    def __init__(self, config: LLMConfig):
        self._config = config
        self._client = self._build_client()
        self._total_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def _build_client(self) -> Any:
        if self._config.provider in ("moonshot", "openai", "deepseek", "dashscope"):
            from openai import OpenAI
            return OpenAI(
                api_key=self._config.api_key,
                base_url=self._config.base_url,
            )
        elif self._config.provider == "anthropic":
            import anthropic
            return anthropic.Anthropic(api_key=self._config.api_key)
        else:
            raise ValueError(f"Unknown provider: {self._config.provider}")

    def chat(self, system: str, user: str) -> str:
        """Single-turn chat. Returns text response."""
        response, _ = self.chat_with_usage(system, user)
        return response

    def chat_with_usage(self, system: str, user: str) -> tuple[str, LLMUsage]:
        """Chat and return (response_text, usage_info)."""
        # Pre-send guard: truncate system prompt if total tokens exceed context window.
        # User message is preserved (usually small). System prompt is trimmed from the end.
        system = self._guard_token_limit(system, user)

        start = time.time()

        if self._config.provider in ("moonshot", "openai", "deepseek", "dashscope"):
            return self._chat_openai_compat(system, user, start)
        elif self._config.provider == "anthropic":
            return self._chat_anthropic(system, user, start)
        else:
            raise ValueError(f"Unknown provider: {self._config.provider}")

    def _guard_token_limit(self, system: str, user: str) -> str:
        """Truncate system prompt if estimated tokens exceed context window.

        This is a safety net — the ContextManager should prevent this from
        happening in most cases. But if it does (legacy paths, edge cases),
        this guard prevents API 400 errors.

        Uses binary search on char position to find the right truncation
        point, validated by estimate_tokens (tiktoken-based).
        """
        from .token_estimator import estimate_tokens

        # Reserve tokens for: output (max_tokens) + user message + overhead
        user_tokens = estimate_tokens(user)
        system_tokens = estimate_tokens(system)
        overhead = 200  # message framing, special tokens
        total = system_tokens + user_tokens + self._config.max_tokens + overhead

        if total <= self._config.context_window:
            return system

        # Need to trim system prompt
        available = self._config.context_window - user_tokens - self._config.max_tokens - overhead
        if available <= 0:
            # Even with no system prompt, user message exceeds window.
            # Truncate system to a minimal stub rather than sending full.
            logger.error(
                "User message alone (%d tokens) nearly fills context window (%d). "
                "Truncating system prompt to minimal stub.",
                user_tokens, self._config.context_window,
            )
            return system[:2000] + "\n\n... [system prompt truncated: user message too large]"

        logger.warning(
            "Pre-send guard: trimming system prompt from %d to ~%d tokens "
            "(context_window=%d, output_reserve=%d)",
            system_tokens, available,
            self._config.context_window, self._config.max_tokens,
        )

        # Binary search for the right char position that fits `available` tokens.
        # Conservative: use 2.5 chars/token as initial estimate (safe for CJK),
        # then verify with estimate_tokens.
        lo, hi = 0, len(system)
        best = min(int(available * 2.5), hi)  # conservative starting point

        for _ in range(8):  # converge in ~8 iterations
            mid = (lo + hi) // 2
            if estimate_tokens(system[:mid]) <= available:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

        return system[:best] + "\n\n... [system prompt truncated to fit context window]"

    def _chat_openai_compat(self, system: str, user: str, start: float) -> tuple[str, LLMUsage]:
        """OpenAI-compatible API (Moonshot/Kimi, OpenAI, DeepSeek)."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self._client.chat.completions.create(
                    model=self._config.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    max_tokens=self._config.max_tokens,
                    temperature=self._config.temperature,
                )
                latency_ms = int((time.time() - start) * 1000)
                usage = response.usage
                input_tokens = usage.prompt_tokens if usage else 0
                output_tokens = usage.completion_tokens if usage else 0
                cost = self._estimate_cost(input_tokens, output_tokens)

                self._total_usage["input_tokens"] += input_tokens
                self._total_usage["output_tokens"] += output_tokens
                self._total_usage["cost_usd"] += cost

                return (
                    response.choices[0].message.content or "",
                    LLMUsage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_usd=cost,
                        latency_ms=latency_ms,
                        provider=self._config.provider,
                        model=self._config.model,
                    ),
                )
            except Exception as e:
                if attempt < max_retries - 1 and self._is_retryable(e):
                    time.sleep(2 ** attempt)
                    continue
                raise

        raise RuntimeError("Exhausted retries")

    def _chat_anthropic(self, system: str, user: str, start: float) -> tuple[str, LLMUsage]:
        """Anthropic native API."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self._client.messages.create(
                    model=self._config.model,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    max_tokens=self._config.max_tokens,
                    temperature=self._config.temperature,
                )
                latency_ms = int((time.time() - start) * 1000)
                input_tokens = response.usage.input_tokens
                output_tokens = response.usage.output_tokens
                cost = self._estimate_cost(input_tokens, output_tokens)

                self._total_usage["input_tokens"] += input_tokens
                self._total_usage["output_tokens"] += output_tokens
                self._total_usage["cost_usd"] += cost

                text = "".join(
                    block.text for block in response.content if hasattr(block, "text")
                )
                return (
                    text,
                    LLMUsage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_usd=cost,
                        latency_ms=latency_ms,
                        provider=self._config.provider,
                        model=self._config.model,
                    ),
                )
            except Exception as e:
                if attempt < max_retries - 1 and self._is_retryable(e):
                    time.sleep(2 ** attempt)
                    continue
                raise

        raise RuntimeError("Exhausted retries")

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Rough cost estimate. Updated as pricing changes."""
        rates = {
            "moonshot": (0.012, 0.012),     # per 1K tokens (CNY, rough USD equiv)
            "anthropic": (0.003, 0.015),     # Sonnet
            "openai": (0.005, 0.015),        # GPT-4o
            "deepseek": (0.0014, 0.0028),    # DeepSeek V3
            "dashscope": (0.0035, 0.007),    # Qwen3.5-35B-A3B (rough USD equiv)
        }
        input_rate, output_rate = rates.get(self._config.provider, (0.01, 0.01))
        return (input_tokens * input_rate + output_tokens * output_rate) / 1000

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        err_str = str(error).lower()
        return any(kw in err_str for kw in ("rate_limit", "rate limit", "429", "timeout", "503"))

    @property
    def total_usage(self) -> dict:
        return dict(self._total_usage)


def create_client_from_config(config: dict) -> LLMClient:
    """Create LLMClient from config.yaml llm section.

    Eval environment variables (MODEL_API_URL, MODEL_API_KEY, MODEL_NAME)
    override config.yaml values when set.
    """
    llm_cfg = config["llm"]

    # Eval environment overrides (injected by KDD Cup Docker runtime)
    model = os.environ.get("MODEL_NAME", "") or llm_cfg["model"]
    base_url = os.environ.get("MODEL_API_URL", "") or llm_cfg.get("base_url")
    api_key = (
        os.environ.get("MODEL_API_KEY", "")
        or os.environ.get(llm_cfg.get("api_key_env", ""), "")
    )

    return LLMClient(
        LLMConfig(
            provider=llm_cfg["provider"],
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_tokens=llm_cfg.get("max_tokens", 8192),
            temperature=llm_cfg.get("temperature", 0.0),
            context_window=llm_cfg.get("context_window", 262_144),
        )
    )
