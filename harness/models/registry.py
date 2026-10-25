"""Maps provider ``type`` names from config to ModelClient factories."""

from __future__ import annotations

from collections.abc import Callable

from harness.config import ConfigError, HarnessConfig, ProviderConfig, RoleModel
from harness.models.base import ModelClient
from harness.models.fake import FakeModelClient

ClientFactory = Callable[[ProviderConfig, RoleModel], ModelClient]

_FACTORIES: dict[str, ClientFactory] = {
    "fake": lambda provider, role_model: FakeModelClient(),
}


def register_provider_type(type_name: str, factory: ClientFactory) -> None:
    """Make a provider ``type`` usable from config (adapters call this on import)."""
    _FACTORIES[type_name] = factory


def available_provider_types() -> list[str]:
    return sorted(_FACTORIES)


def client_for_role(config: HarnessConfig, role: str) -> ModelClient:
    """Build the ModelClient configured for ``role``."""
    role_model = config.model_for(role)
    provider = config.providers[role_model.provider]
    factory = _FACTORIES.get(provider.type)
    if factory is None:
        raise ConfigError(
            f"provider {role_model.provider!r} has type {provider.type!r}, which is not "
            f"available (known: {', '.join(available_provider_types())})"
        )
    return factory(provider, role_model)
