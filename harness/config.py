"""Harness configuration: providers, per-role models, context budgets.

Config is merged from the user's personal file (``~/.config/harness/config.yaml``)
and the project's ``.harness/config.yaml``; project values win.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

USER_CONFIG_PATH = Path(os.environ.get("HARNESS_USER_CONFIG", "~/.config/harness/config.yaml"))


class ConfigError(Exception):
    """Raised when the configuration is missing, malformed, or inconsistent."""


class ProviderConfig(BaseModel):
    """A model provider. ``type`` selects the adapter; extra keys go to the adapter."""

    model_config = ConfigDict(extra="allow")

    type: str
    api_key_env: str | None = None
    base_url: str | None = None


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class RoleModel(BaseModel):
    """Which provider and model a role uses, plus generation settings."""

    provider: str
    model: str
    max_tokens: int = 32_000
    effort: Effort | None = None


class ModelsConfig(BaseModel):
    default: RoleModel | None = None
    roles: dict[str, RoleModel] = Field(default_factory=dict)


class OrchestratorConfig(BaseModel):
    max_review_rounds: int = 3  # changes_requested rounds before escalating
    max_gate_attempts: int = 4  # failed `finish` gate runs per agent before escalating
    max_merge_failures: int = 3
    agent_max_turns: int = 80
    lease_s: float = 900.0
    max_parallel_agents: int | None = None  # None = no cap


class Price(BaseModel):
    """USD per million tokens. Cache prices default to 10% (read) / 125% (write) of input."""

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None


# Anthropic first-party list prices; override or extend with `pricing:` in config.
DEFAULT_PRICING: dict[str, Price] = {
    "claude-fable-5-1": Price(input=10.0, output=50.0, cache_read=0.25),
    "claude-opus-5-5": Price(input=4.0, output=20.0, cache_read=0.20),
    "claude-sonnet-5-5": Price(input=2.0, output=10.0, cache_read=0.20),
    "claude-haiku-5-5": Price(input=0.10, output=0.50),
}


class HarnessConfig(BaseModel):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    context_budget_tokens: dict[str, int] = Field(default_factory=dict)
    stacks: list[str] | None = None
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    pricing: dict[str, Price] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_providers_exist(self) -> HarnessConfig:
        used = list(self.models.roles.items())
        if self.models.default is not None:
            used.append(("default", self.models.default))
        for role, rm in used:
            if rm.provider not in self.providers:
                raise ValueError(f"models.{role} uses unknown provider {rm.provider!r}")
        return self

    def model_for(self, role: str) -> RoleModel:
        """The model a role should use: its own entry, else ``models.default``."""
        if role in self.models.roles:
            return self.models.roles[role]
        if self.models.default is not None:
            return self.models.default
        raise ConfigError(f"no model configured for role {role!r} and no models.default")

    def provider_for(self, role: str) -> ProviderConfig:
        return self.providers[self.model_for(role).provider]

    def price_for(self, model: str) -> Price | None:
        return self.pricing.get(model) or DEFAULT_PRICING.get(model)

    def context_budget(self, role: str, fallback: int = 12_000) -> int:
        return self.context_budget_tokens.get(role, fallback)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``; lists and scalars replace."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def load_config(project_root: Path | None = None, user_config: Path | None = None) -> HarnessConfig:
    """Load and validate the merged user + project configuration."""
    raw = _read_yaml((user_config or USER_CONFIG_PATH).expanduser())
    if project_root is not None:
        raw = deep_merge(raw, _read_yaml(project_root / ".harness" / "config.yaml"))
    try:
        return HarnessConfig.model_validate(raw)
    except ValueError as e:
        raise ConfigError(str(e)) from e
