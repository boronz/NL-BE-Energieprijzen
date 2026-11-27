"""Provider registry.

Adding a provider means adding one module implementing
:class:`~.base.PriceProvider`, registering it below, and adding its setup step
to the config flow. Nothing in the coordinator, the platforms or the pure
price-zone/optimal-period modules needs to change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..const import (
    CONF_PROVIDER,
    PROVIDER_EPEX,
    PROVIDER_FRANK,
    PROVIDER_TIBBER,
    SUPPORTED_PROVIDERS,
)
from .base import (
    PriceProvider,
    ProviderAuthError,
    ProviderConnectionError,
    ProviderError,
)
from .epex import (
    EpexProvider,
    async_fetch_epex_providers,
    format_provider_label,
)
from .frank import FrankProvider
from .tibber import TibberProvider, async_fetch_homes

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PROVIDER_CLASSES: dict[str, type[PriceProvider]] = {
    PROVIDER_FRANK: FrankProvider,
    PROVIDER_TIBBER: TibberProvider,
    PROVIDER_EPEX: EpexProvider,
}

__all__ = [
    "PROVIDER_CLASSES",
    "SUPPORTED_PROVIDERS",
    "PriceProvider",
    "ProviderAuthError",
    "ProviderConnectionError",
    "ProviderError",
    "async_fetch_epex_providers",
    "async_fetch_homes",
    "create_provider",
    "format_provider_label",
    "get_provider_class",
]


def get_provider_class(provider_id: str) -> type[PriceProvider]:
    """Return the provider class for ``provider_id``.

    An unknown id is a programming error, not user input: the config flow only
    stores a registered provider and entries are validated on setup.
    """
    try:
        return PROVIDER_CLASSES[provider_id]
    except KeyError as err:
        raise ValueError(f"Unknown price provider: {provider_id!r}") from err


def create_provider(hass: HomeAssistant, entry: ConfigEntry) -> PriceProvider:
    """Instantiate the provider a config entry is bound to."""
    provider_id = entry.data.get(CONF_PROVIDER, PROVIDER_FRANK)
    return get_provider_class(provider_id)(hass, entry)
