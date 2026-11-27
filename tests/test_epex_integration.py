"""End-to-end tests for an EPEX config entry and its entities."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.quarter_prices.const import (
    CONF_CURRENCY,
    CONF_EPEX_PROVIDER_ID,
    CONF_EPEX_PROVIDER_NAME,
    CONF_FEED_IN_ADJUSTMENT,
    CONF_MARKET,
    CONF_PROVIDER,
    CONF_RESOLUTION,
    CONF_TIME_ZONE,
    DOMAIN,
    MARKET_NL,
    PROVIDER_EPEX,
    RESOLUTION_QUARTERLY,
)

from .conftest import SETUP_NOW, TEST_TIMEZONE
from .test_epex_provider import (
    ENERGY_TAX,
    PRICE,
    PROVIDER_CHARGE,
    epex_entries,
)

CURRENT_PRICE = "sensor.eneco_current_price"
RETURN_PRICE = "sensor.eneco_current_return_price"
PRICES_TODAY = "sensor.eneco_prices_today"
PRICES_TOMORROW = "sensor.eneco_prices_tomorrow"
TOMORROW_AVAILABLE = "binary_sensor.eneco_tomorrow_prices_available"

EXPECTED_MARKET_PRICE = round((PRICE - ENERGY_TAX - PROVIDER_CHARGE) / 1.21, 6)


def payload(day: date) -> dict[str, Any]:
    """Return a two-day EPEX API response."""
    return {
        "today": epex_entries(day),
        "tomorrow": epex_entries(day + timedelta(days=1)),
        "energy_tax": ENERGY_TAX,
        "provider_charge": PROVIDER_CHARGE,
        "spot_coefficient": 1.0,
    }


def epex_entry(options: dict[str, Any] | None = None) -> MockConfigEntry:
    """Return a configured Dutch EPEX entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Eneco Quarter Prices",
        data={
            CONF_PROVIDER: PROVIDER_EPEX,
            CONF_MARKET: MARKET_NL,
            CONF_EPEX_PROVIDER_ID: "eneco",
            CONF_EPEX_PROVIDER_NAME: "Eneco",
            CONF_RESOLUTION: RESOLUTION_QUARTERLY,
            CONF_TIME_ZONE: TEST_TIMEZONE,
            CONF_CURRENCY: "EUR",
        },
        options=options or {},
        unique_id="epex_nl__eneco",
    )


async def setup_epex(hass, aioclient_mock, entry: MockConfigEntry):
    """Set up an EPEX entry against a mocked epexprijzen response."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    aioclient_mock.get(
        "https://epexprijzen.nl/api/v1/prices/eneco/quarterly",
        json=payload(SETUP_NOW.date()),
    )
    entry.add_to_hass(hass)
    freezer = freeze_time(SETUP_NOW)
    freezer.start()
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        freezer.stop()


async def test_epex_entry_creates_entities_on_the_provider_device(hass, aioclient_mock):
    """EPEX entities are named after the chosen provider, not the source."""
    entry = epex_entry()
    await setup_epex(hass, aioclient_mock, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(CURRENT_PRICE) is not None
    assert hass.states.get(PRICES_TODAY) is not None
    assert int(hass.states.get(PRICES_TODAY).state) == 96
    assert hass.states.get(TOMORROW_AVAILABLE).state == "on"

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device is not None
    assert device.name == "Eneco"
    assert device.manufacturer == "epexprijzen.nl"
    assert "NL" in device.model


async def test_epex_current_price_is_all_in(hass, aioclient_mock):
    """The current price uses the published all-in price, not the spot only."""
    await setup_epex(hass, aioclient_mock, epex_entry())

    state = hass.states.get(CURRENT_PRICE)
    assert state is not None
    assert float(state.state) == pytest.approx(PRICE)
    block = state.attributes["full_block"]
    assert block["total_price_eur_kwh"] == pytest.approx(PRICE)
    assert block["energy_tax_price"] == pytest.approx(ENERGY_TAX)
    assert block["dynamic_price_eur_kwh"] == pytest.approx(PRICE)


async def test_epex_feed_in_uses_derived_spot(hass, aioclient_mock):
    """The feed-in sensor uses the derived bare energy price plus adjustment."""
    await setup_epex(
        hass,
        aioclient_mock,
        epex_entry({CONF_FEED_IN_ADJUSTMENT: -0.01}),
    )

    state = hass.states.get(RETURN_PRICE)
    assert state is not None
    assert float(state.state) == pytest.approx(EXPECTED_MARKET_PRICE - 0.01, abs=1e-6)
    assert state.attributes["market_price"] == pytest.approx(
        EXPECTED_MARKET_PRICE, abs=1e-6
    )


async def test_epex_day_arrays_use_the_all_in_price(hass, aioclient_mock):
    """Cheapest/most expensive and the daily arrays use total_price_eur_kwh."""
    await setup_epex(hass, aioclient_mock, epex_entry())

    prices_today = hass.states.get(PRICES_TODAY)
    assert prices_today.attributes["average_price"] == pytest.approx(PRICE)
    assert float(hass.states.get("sensor.eneco_cheapest_today").state) == (
        pytest.approx(PRICE)
    )
    assert float(hass.states.get("sensor.eneco_most_expensive_today").state) == (
        pytest.approx(PRICE)
    )
    assert int(hass.states.get(PRICES_TOMORROW).state) == 96


async def test_epex_entity_ids_follow_the_provider_name(hass, aioclient_mock):
    """Renaming the chosen provider changes the device, not the source prefix."""
    data = dict(epex_entry().data)
    data[CONF_EPEX_PROVIDER_NAME] = "Zonneplan"
    data[CONF_EPEX_PROVIDER_ID] = "zonneplan"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zonneplan Quarter Prices",
        data=data,
        unique_id="epex_nl__zonneplan",
    )
    aioclient_mock.get(
        "https://epexprijzen.nl/api/v1/prices/zonneplan/quarterly",
        json=payload(SETUP_NOW.date()),
    )
    entry.add_to_hass(hass)
    freezer = freeze_time(SETUP_NOW)
    freezer.start()
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        freezer.stop()

    assert hass.states.get("sensor.zonneplan_current_price") is not None
