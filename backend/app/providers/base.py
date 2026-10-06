from collections.abc import AsyncIterator
from typing import Protocol

from pydantic import BaseModel


class Generation(BaseModel):
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLMProvider(Protocol):
    async def generate(self, system: str, content: str, model: str, max_tokens: int) -> Generation: ...
    def stream(self, system: str, content: str, model: str, max_tokens: int) -> AsyncIterator[str]: ...
