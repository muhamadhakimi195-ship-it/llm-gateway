"""Configuration references secret environment variables rather than embedding secrets."""

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import Field, field_validator, model_validator

from gateway.schemas import NonnegativePrice, StrictModel, TeamPolicy


class ProviderConfig(StrictModel):
    kind: Literal["compatible", "ollama", "anthropic"]
    base_url: str
    base_url_env: str | None = None
    api_key_env: str | None = None
    model: str = Field(min_length=1)
    input_usd_per_million: NonnegativePrice = 0.0
    output_usd_per_million: NonnegativePrice = 0.0
    mock_scenario: (
        Literal["normal", "unavailable", "rate_limit", "auth_error", "midstream"] | None
    ) = None

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
            raise ValueError(
                "provider base_url must be an HTTP(S) URL without embedded credentials"
            )
        if parts.query or parts.fragment:
            raise ValueError("provider base_url cannot contain a query or fragment")
        return value.rstrip("/")

    def resolved_url(self) -> str:
        value = (
            os.environ.get(self.base_url_env, self.base_url) if self.base_url_env else self.base_url
        )
        return self.validate_url(value)


class TeamConfig(StrictModel):
    api_key_env: str
    policy: TeamPolicy


class GatewayConfig(StrictModel):
    providers: dict[str, ProviderConfig]
    routes: dict[str, list[str]]
    teams: dict[str, TeamConfig]
    provider_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    request_deadline_seconds: float = Field(default=60.0, gt=0, le=300)
    retry_count: int = Field(default=1, ge=0, le=3)
    retry_base_seconds: float = Field(default=0.1, ge=0, le=5)
    circuit_failure_threshold: int = Field(default=3, ge=1, le=100)
    circuit_window_seconds: float = Field(default=30.0, gt=0)
    circuit_cooldown_seconds: float = Field(default=10.0, gt=0)
    interactive_concurrency: int = Field(default=16, ge=1, le=1000)
    batch_concurrency: int = Field(default=2, ge=1, le=1000)
    queue_timeout_seconds: float = Field(default=1.0, gt=0, le=30)

    @model_validator(mode="after")
    def consistent_references(self) -> "GatewayConfig":
        if not self.providers or not self.routes or not self.teams:
            raise ValueError("providers, routes, and teams cannot be empty")
        for name in [*self.providers, *self.routes, *self.teams]:
            if (
                not name
                or len(name) > 80
                or not all(c.isascii() and (c.isalnum() or c in "_-.") for c in name)
            ):
                raise ValueError("configuration identifiers must use letters, digits, _, or -")
        for chain in self.routes.values():
            if (
                not chain
                or len(chain) != len(set(chain))
                or any(p not in self.providers for p in chain)
            ):
                raise ValueError("routes require nonempty, unique, known provider IDs")
        for team in self.teams.values():
            if any(model not in self.routes for model in team.policy.allowed_models):
                raise ValueError("team allowed_models must reference configured routes")
        return self


def load_config(path: str | Path) -> GatewayConfig:
    return GatewayConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
