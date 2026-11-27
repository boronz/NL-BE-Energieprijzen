"""End-to-end tests for a Tibber config entry and its entities."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.quarter_prices.const import (
    CONF_ACCESS_TOKEN,
    CONF_COUNTRY,
    CONF_CURRENCY,
    CONF_FEED_IN_ADJUSTMENT,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_PROVIDER,
    CONF_TIME_ZONE,
    DOMAIN,
    PROVIDER_FRANK,
    PROVIDER_TIBBER,
)
from custom_components.quarter_prices.providers.base import ProviderAuthError

from .conftest import SETUP_NOW, TEST_TIMEZONE

TZ = ZoneInfo(TEST_TIMEZONE)
HOME_ID = "home-1"

CURRENT_PRICE = "sensor.tibber_current_price"
RETURN_PRICE = "sensor.tibber_current_return_price"
PRICES_TODAY = "sensor.tibber_prices_today"
PRICES_TOMORROW = "sensor.tibber_prices_tomorrow"
TOMORROW_AVAILABLE = "binary_sensor.tibber_tomorrow_prices_available"

# The complete set of entities a Tibber entry is expected to create.
EXPECTED_ENTITIES = {
    CURRENT_PRICE,
    RETURN_PRICE,
    PRICES_TODAY,
    PRICES_TOMORROW,
    "sensor.tibber_cheapest_today",
    "sensor.tibber_cheapest_time_today",
    "sensor.tibber_most_expensive_today",
    "sensor.tibber_most_expensive_time_today",
    "sensor.tibber_cheapest_tomorrow",
    "sensor.tibber_cheapest_time_tomorrow",
    "sensor.tibber_most_expensive_tomorrow",
    "sensor.tibber_most_expensive_time_tomorrow",
    "sensor.tibber_cheap_zone_start_today",
    "sensor.tibber_cheap_zone_end_today",
    "sensor.tibber_expensive_zone_start_today",
    "sensor.tibber_expensive_zone_end_today",
    TOMORROW_AVAILABLE,
    "binary_sensor.tibber_cheap_period",
    "binary_sensor.tibber_expensive_period",
}


def tibber_block(start: datetime) -> dict[str, Any]:
    """Return one normalized Tibber quarter block."""
    till = start + timedelta(minutes=15)
    return {
        "from": start,
        "till": till,
        "duration_minutes": 15,
        "market_price": 0.08,
        "market_price_tax": 0.05,
        "sourcing_markup_price": 0.0,
        "energy_tax_price": 0.0,
        "total_price_eur_kwh": 0.13,
        "per_unit": "kWh",
        "provider": PROVIDER_TIBBER,
    }


def tibber_day(day) -> dict[str, Any]:
    """Return a full quarter-hour day shaped like the Tibber provider output."""
    cursor = datetime.combine(day, time.min, tzinfo=TZ)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=TZ)
    blocks = []
    while cursor < end:
        blocks.append(tibber_block(cursor))
        cursor += timedelta(minutes=15)
    return {"prices": blocks}


def tibber_entry(options: dict[str, Any] | None = None) -> MockConfigEntry:
    """Return a configured Tibber entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Tibber Quarter Prices (Home)",
        data={
            CONF_PROVIDER: PROVIDER_TIBBER,
            CONF_ACCESS_TOKEN: "tibber_pat_test",
            CONF_HOME_ID: HOME_ID,
            CONF_HOME_NAME: "Home",
            CONF_TIME_ZONE: TEST_TIMEZONE,
            CONF_CURRENCY: "EUR",
        },
        options=options or {},
        unique_id="tibber_home-1",
    )


async def setup_tibber(hass, entry: MockConfigEntry):
    """Set up an entry with the provider stubbed at its public method."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    entry.add_to_hass(hass)
    mock = AsyncMock(side_effect=lambda day: tibber_day(day))
    freezer = freeze_time(SETUP_NOW)
    freezer.start()
    patcher = patch(
        "custom_components.quarter_prices.providers.tibber."
        "TibberProvider.async_get_prices_for_date",
        mock,
    )
    patcher.start()
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        freezer.stop()
        patcher.stop()
    return mock


async def test_tibber_entry_creates_a_tibber_device_and_entities(hass):
    """The same entity set is created, prefixed with the provider's name."""
    entry = tibber_entry()
    await setup_tibber(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    states = {state.entity_id for state in hass.states.async_all()}
    assert EXPECTED_ENTITIES <= states

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device is not None
    assert device.name == "Tibber"
    assert device.manufacturer == "Tibber AS"
    assert device.model == "Tibber Price Info"


async def test_current_price_is_tibbers_all_in_price(hass):
    """Tibber's total (energy + tax) is the current price, with spot available."""
    await setup_tibber(hass, tibber_entry())

    state = hass.states.get(CURRENT_PRICE)
    assert state is not None
    assert float(state.state) == pytest.approx(0.13)

    full_block = state.attributes["full_block"]
    assert full_block["market_price"] == pytest.approx(0.08)
    assert full_block["total_price_eur_kwh"] == pytest.approx(0.13)
    assert full_block["duration_minutes"] == 15
    assert state.attributes["time"] in {"14:00", "14:15"}

    # The all-in price is also what the day arrays and extrema compare on.
    prices_today = hass.states.get(PRICES_TODAY)
    assert int(prices_today.state) == 96
    assert prices_today.attributes["average_price"] == pytest.approx(0.13)
    assert float(hass.states.get("sensor.tibber_cheapest_today").state) == (
        pytest.approx(0.13)
    )


async def test_feed_in_price_uses_tibbers_spot_price(hass):
    """The feed-in estimate is spot (``energy``) plus the adjustment."""
    entry = tibber_entry({CONF_FEED_IN_ADJUSTMENT: -0.01})
    await setup_tibber(hass, entry)

    state = hass.states.get(RETURN_PRICE)
    assert state is not None
    assert float(state.state) == pytest.approx(0.07)
    assert state.attributes["market_price"] == pytest.approx(0.08)
    assert state.attributes["feed_in_adjustment"] == pytest.approx(-0.01)


async def test_tomorrow_is_available_when_the_provider_returns_it(hass):
    """Tomorrow's entities are available because the stub returns a full day."""
    await setup_tibber(hass, tibber_entry())

    assert hass.states.get(TOMORROW_AVAILABLE).state == "on"
    assert int(hass.states.get(PRICES_TOMORROW).state) == 96
    assert hass.states.get("sensor.tibber_cheapest_tomorrow") is not None


async def test_auth_failure_puts_the_entry_into_setup_retry(hass):
    """A rejected token is surfaced as a failed setup, not a broken entity."""
    entry = tibber_entry()
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    entry.add_to_hass(hass)
    freezer = freeze_time(SETUP_NOW)
    freezer.start()
    try:
        with patch(
            "custom_components.quarter_prices.providers.tibber."
            "TibberProvider.async_get_prices_for_date",
            AsyncMock(side_effect=ProviderAuthError("bad token")),
        ):
            assert not await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
    finally:
        freezer.stop()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_frank_and_tibber_entries_coexist(hass):
    """Provider selection means both sources can live side by side."""
    tibber = tibber_entry()
    frank = MockConfigEntry(
        domain=DOMAIN,
        title="Frank Quarter Prices (NL)",
        data={CONF_PROVIDER: PROVIDER_FRANK, CONF_COUNTRY: "NL"},
        unique_id="frank_NL",
    )

    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    tibber.add_to_hass(hass)
    frank.add_to_hass(hass)
    freezer = freeze_time(SETUP_NOW)
    freezer.start()
    try:
        with (
            patch(
                "custom_components.quarter_prices.providers.tibber."
                "TibberProvider.async_get_prices_for_date",
                AsyncMock(side_effect=lambda day: tibber_day(day)),
            ),
            patch(
                "custom_components.quarter_prices.providers.frank."
                "FrankProvider.async_get_prices_for_date",
                AsyncMock(side_effect=lambda day: tibber_day(day)),
            ),
        ):
            assert await hass.config_entries.async_setup(tibber.entry_id)
            # ``add_to_hass`` already schedules setup for the second entry; the
            # patched providers cover that automatic load as well.
            await hass.async_block_till_done()
    finally:
        freezer.stop()

    assert tibber.state is ConfigEntryState.LOADED
    assert frank.state is ConfigEntryState.LOADED
    assert hass.states.get(CURRENT_PRICE) is not None
    assert hass.states.get("sensor.frank_current_price") is not None
    # Two providers, two devices, no entity-id collision.
    assert len(dr.async_get(hass).devices) == 2


async def test_diagnostics_redact_the_access_token(hass):
    """The stored token must never appear in a diagnostics download."""
    from custom_components.quarter_prices.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = tibber_entry()
    await setup_tibber(hass, entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry"]["data"][CONF_ACCESS_TOKEN] == "**REDACTED**"
    assert diagnostics["provider"]["id"] == PROVIDER_TIBBER
    assert diagnostics["provider"]["home_id"] == HOME_ID
    assert diagnostics["resolved_options"]["market_timezone"] == TEST_TIMEZONE


async def test_diagnostics_work_before_the_entry_is_loaded(hass):
    """Diagnostics remain available while the entry is failing to set up."""
    from custom_components.quarter_prices.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = tibber_entry()
    entry.add_to_hass(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["coordinator"]["loaded"] is False
    assert diagnostics["resolved_options"]["market_timezone"] == TEST_TIMEZONE
