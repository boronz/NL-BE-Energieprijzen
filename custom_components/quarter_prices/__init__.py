"""The Quarter Prices integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN, PLATFORMS
from .coordinator import QuarterPricesCoordinator
from .providers import create_provider

_LOGGER = logging.getLogger(__name__)

# Typed config entry alias for convenience throughout the integration.
type QuarterPricesConfigEntry = ConfigEntry[QuarterPricesCoordinator]


async def async_setup_entry(
    hass: HomeAssistant, entry: QuarterPricesConfigEntry
) -> bool:
    """Set up Quarter Prices from a config entry."""
    _LOGGER.debug("Setting up config entry %s", entry.entry_id)

    provider = create_provider(hass, entry)
    coordinator = QuarterPricesCoordinator(hass=hass, entry=entry, provider=provider)

    # Expose the integration version as the device sw_version.
    integration = await async_get_integration(hass, DOMAIN)
    coordinator.integration_version = str(integration.version or "unknown")

    # Fetch initial data so we have data when entities are added.
    await coordinator.async_config_entry_first_refresh()

    # Store the coordinator on the runtime_data of the entry (HA 2024.6+).
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: QuarterPricesConfigEntry
) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading config entry %s", entry.entry_id)

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant, entry: QuarterPricesConfigEntry
) -> None:
    """Reload the config entry when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)
