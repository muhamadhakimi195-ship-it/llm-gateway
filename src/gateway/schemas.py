"""Public contracts are deliberately narrower than any individual vendor's API."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Message(StrictModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32_000)

    @field_validator("content")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message content must not be blank")
        # Preserve the caller's bytes: normalization can change code and structured prompts.
        return value


class ChatRequest(StrictModel):
    model: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    messages: list[Message] = Field(min_length=1, max_length=64)
    max_tokens: int = Field(default=256, ge=1, le=4096)
    temperature: float = Field(default=0.0, ge=0, le=2)
    stream: bool = False
    priority: Literal["interactive", "batch"] = "interactive"

    @field_validator("messages")
    @classmethod
    def bounded_prompt(cls, messages: list[Message]) -> list[Message]:
        if sum(len(m.content.encode("utf-8")) for m in messages) > 64_000:
            raise ValueError("combined message content exceeds 64000 UTF-8 bytes")
        if not any(m.role == "user" for m in messages):
            raise ValueError("at least one user message is required")
        seen_conversation = False
        for message in messages:
            if message.role == "system" and seen_conversation:
                raise ValueError("system messages must precede conversation messages")
            seen_conversation = seen_conversation or message.role != "system"
        return messages


class Usage(StrictModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ProviderReply(StrictModel):
    content: str
    usage: Usage | None = None
    finish_reason: str = "stop"


class StreamEvent(StrictModel):
    text: str = ""
    usage: Usage | None = None
    done: bool = False
    finish_reason: str = "stop"


class TeamPolicy(StrictModel):
    allowed_models: list[str] = Field(min_length=1, max_length=32)
    requests_per_minute: int = Field(default=60, ge=1, le=100_000)
    tokens_per_minute: int = Field(default=100_000, ge=1, le=100_000_000)
    daily_budget_microusd: int = Field(default=5_000_000, ge=0, le=10**12)
    system_prompt: str = Field(default="", max_length=4000)


NonnegativePrice = Annotated[float, Field(ge=0, le=100_000, allow_inf_nan=False)]
