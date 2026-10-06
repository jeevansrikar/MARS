"""Adapter preserving the original retrieval/evaluation modules' interface."""

from dataclasses import dataclass

from app.core.config import get_settings
from app.core.errors import AppError
from app.providers.anthropic import AnthropicProvider


@dataclass
class TokenBudget:
    max_tokens: int
    used: int = 0

    def reserve(self, tokens: int):
        if self.used + tokens > self.max_tokens:
            raise AppError("BUDGET_EXHAUSTED", "Token budget exhausted")
        self.used += tokens


class LLMClient:
    def __init__(self, role: str, budget: TokenBudget):
        self.role, self.budget = role, budget

    async def complete(self, *, messages, system, max_tokens, temperature=0, session_id=None):
        settings = get_settings()
        content = "\n".join(str(m["content"]) for m in messages)
        self.budget.reserve(len(content.encode()) + max_tokens)
        provider = AnthropicProvider(settings)
        try:
            result = await provider.generate(system, content, settings.fast_model, max_tokens)
            return result.text
        finally:
            await provider.close()
