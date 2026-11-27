"""Tests for the generic EPEX (epexprijzen.nl/.be) provider."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.quarter_prices.const import (
    CONF_CURRENCY,
    CONF_EPEX_PROVIDER_ID,
    CONF_EPEX_PROVIDER_NAME,
    CONF_MARKET,
    CONF_PROVIDER,
    CONF_REGION,
    CONF_RESOLUTION,
    CONF_TIME_ZONE,
    DOMAIN,
    MARKET_BE,
    MARKET_NL,
    PROVIDER_EPEX,
    RESOLUTION_HOURLY,
    RESOLUTION_QUARTERLY,
)
from custom_components.quarter_prices.price import calculate_feed_in_price
from custom_components.quarter_prices.providers.base import ProviderConnectionError
from custom_components.quarter_prices.providers.epex import (
    EpexProvider,
    async_fetch_epex_providers,
    format_provider_label,
)

TZ = ZoneInfo("Europe/Amsterdam")
BE_TZ = ZoneInfo("Europe/Brussels")
PROVIDER_ID = "eneco"
ENERGY_TAX = 0.05
PROVIDER_CHARGE = 0.018
PRICE = 0.13

NL_BASE = "https://epexprijzen.nl"
BE_BASE = "https://epexprijzen.be"


def epex_entries(
    day: date,
    resolution_minutes: int = 15,
    price: float = PRICE,
    time_zone: ZoneInfo = TZ,
) -> list[dict[str, Any]]:
    """Build API entries covering the full market day, stepping in absolute time."""
    cursor = datetime.combine(day, time.min, tzinfo=time_zone).astimezone(UTC)
    end = datetime.combine(
        day + timedelta(days=1), time.min, tzinfo=time_zone
    ).astimezone(UTC)
    entries = []
    step = timedelta(minutes=resolution_minutes)
    while cursor < end:
        entries.append(
            {
                "t": cursor.astimezone(time_zone).isoformat(),
                "price": price,
            }
        )
        cursor += step
    return entries


def payload(
    day: date,
    resolution_minutes: int = 15,
    price: float = PRICE,
    tomorrow: bool = True,
    time_zone: ZoneInfo = TZ,
) -> dict[str, Any]:
    """Return a complete API response for a day and its tomorrow."""
    return {
        "today": epex_entries(day, resolution_minutes, price, time_zone),
        "tomorrow": (
            epex_entries(day + timedelta(days=1), resolution_minutes, price, time_zone)
            if tomorrow
            else []
        ),
        "energy_tax": ENERGY_TAX,
        "provider_charge": PROVIDER_CHARGE,
        "spot_coefficient": 1.0,
    }


def make_entry(
    market: str = MARKET_NL,
    resolution: str = RESOLUTION_QUARTERLY,
    region: str | None = None,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Return a config entry bound to the EPEX provider."""
    data: dict[str, Any] = {
        CONF_PROVIDER: PROVIDER_EPEX,
        CONF_MARKET: market,
        CONF_EPEX_PROVIDER_ID: PROVIDER_ID,
        CONF_EPEX_PROVIDER_NAME: "Eneco",
        CONF_RESOLUTION: resolution,
        CONF_TIME_ZONE: str(TZ if market == MARKET_NL else BE_TZ),
        CONF_CURRENCY: "EUR",
    }
    if region:
        data[CONF_REGION] = region
    return MockConfigEntry(
        domain=DOMAIN,
        title="Eneco Quarter Prices",
        data=data,
        options=options or {},
        unique_id=f"epex_{market.lower()}_{region or ''}_{PROVIDER_ID}",
    )


def make_provider(hass, **kwargs: Any) -> EpexProvider:
    """Create a provider bound to a mock config entry."""
    return EpexProvider(hass, make_entry(**kwargs))


def url(resolution: str = RESOLUTION_QUARTERLY, market: str = MARKET_NL) -> str:
    """Return the prices URL for the default provider."""
    base = NL_BASE if market == MARKET_NL else BE_BASE
    return f"{base}/api/v1/prices/{PROVIDER_ID}/{resolution}"


# --- Parsing ----------------------------------------------------------------


async def test_quarter_hour_day_is_split_into_components(hass, aioclient_mock):
    """The all-in price is split so the four components sum to it exactly."""
    day = date(2026, 8, 6)
    aioclient_mock.get(url(), json=payload(day))

    provider = make_provider(hass)
    blocks = (await provider.async_get_prices_for_date(day))["prices"]

    assert len(blocks) == 96
    assert {block["duration_minutes"] for block in blocks} == {15}
    block = blocks[0]
    assert block["total_price_eur_kwh"] == pytest.approx(PRICE)
    assert block["energy_tax_price"] == pytest.approx(ENERGY_TAX)
    assert block["sourcing_markup_price"] == pytest.approx(PROVIDER_CHARGE)
    assert block["market_price"] == pytest.approx(
        (PRICE - ENERGY_TAX - PROVIDER_CHARGE) / 1.21, abs=1e-6
    )
    assert (
        block["market_price"]
        + block["market_price_tax"]
        + block["sourcing_markup_price"]
        + block["energy_tax_price"]
    ) == pytest.approx(PRICE)
    # The current-price override is the published all-in price.
    assert block["dynamic_price_eur_kwh"] == pytest.approx(PRICE)
    # The derived spot price is the feed-in base.
    assert calculate_feed_in_price(block, 0.0) == pytest.approx(0.05124)
    assert block["epex_provider_id"] == PROVIDER_ID
    assert block["epex_resolution"] == RESOLUTION_QUARTERLY


async def test_hourly_day_has_twenty_four_blocks(hass, aioclient_mock):
    """The hourly endpoint keeps 60-minute blocks and the same split."""
    day = date(2026, 8, 6)
    aioclient_mock.get(url(RESOLUTION_HOURLY), json=payload(day, resolution_minutes=60))

    provider = make_provider(hass, resolution=RESOLUTION_HOURLY)
    blocks = (await provider.async_get_prices_for_date(day))["prices"]

    assert len(blocks) == 24
    assert {block["duration_minutes"] for block in blocks} == {60}
    assert blocks[-1]["till"].astimezone(TZ).strftime("%H:%M") == "00:00"


async def test_resolution_option_overrides_entry_data(hass, aioclient_mock):
    """Changing the resolution in the options takes effect immediately."""
    day = date(2026, 8, 6)
    aioclient_mock.get(url(RESOLUTION_HOURLY), json=payload(day, resolution_minutes=60))

    provider = make_provider(hass, options={CONF_RESOLUTION: RESOLUTION_HOURLY})
    assert provider.resolution == RESOLUTION_HOURLY
    blocks = (await provider.async_get_prices_for_date(day))["prices"]
    assert len(blocks) == 24


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 3, 29), 92),  # spring forward
        (date(2026, 10, 25), 100),  # fall back
    ],
)
async def test_dst_days_are_continuous(hass, aioclient_mock, day, expected):
    """DST days keep 15-minute durations and exact absolute-time continuity."""
    aioclient_mock.get(url(), json=payload(day))

    provider = make_provider(hass)
    blocks = (await provider.async_get_prices_for_date(day))["prices"]

    assert len(blocks) == expected
    assert {block["duration_minutes"] for block in blocks} == {15}
    for previous, block in zip(blocks, blocks[1:]):
        assert previous["till"] == block["from"]
    assert blocks[-1]["till"].astimezone(TZ).strftime("%H:%M") == "00:00"


async def test_negative_price_is_not_clamped(hass, aioclient_mock):
    """A negative all-in price keeps a negative spot and feed-in base."""
    day = date(2026, 8, 6)
    aioclient_mock.get(url(), json=payload(day, price=-0.05))

    provider = make_provider(hass)
    block = (await provider.async_get_prices_for_date(day))["prices"][0]

    assert block["total_price_eur_kwh"] == pytest.approx(-0.05)
    assert block["market_price"] < 0
    assert (
        block["market_price"]
        + block["market_price_tax"]
        + block["sourcing_markup_price"]
        + block["energy_tax_price"]
    ) == pytest.approx(-0.05)


# --- Fetching and caching ---------------------------------------------------


async def test_today_and_tomorrow_share_one_request(hass, aioclient_mock):
    """One response serves both days and is reused within the cache TTL."""
    today = date(2026, 8, 6)
    aioclient_mock.get(url(), json=payload(today))
    provider = make_provider(hass)

    today_prices = await provider.async_get_prices_for_date(today)
    tomorrow_prices = await provider.async_get_prices_for_date(
        today + timedelta(days=1)
    )

    assert len(today_prices["prices"]) == 96
    assert len(tomorrow_prices["prices"]) == 96
    assert aioclient_mock.call_count == 1


async def test_unpublished_tomorrow_is_empty(hass, aioclient_mock):
    """An empty tomorrow is an expected state, not an error."""
    today = date(2026, 8, 6)
    aioclient_mock.get(url(), json=payload(today, tomorrow=False))
    provider = make_provider(hass)

    assert len((await provider.async_get_prices_for_date(today))["prices"]) == 96
    assert (await provider.async_get_prices_for_date(today + timedelta(days=1)))[
        "prices"
    ] == []


async def test_unknown_provider_is_a_clear_error(hass, aioclient_mock):
    """A 404 from epexprijzen includes the site's error text."""
    aioclient_mock.get(url(), status=404, json={"error": "Unknown provider"})
    provider = make_provider(hass)

    with pytest.raises(ProviderConnectionError, match="Unknown provider"):
        await provider.async_get_prices_for_date(date(2026, 8, 6))


async def test_server_error_is_retried(hass, aioclient_mock, monkeypatch):
    """5xx responses are retried before the request is given up."""
    monkeypatch.setattr(
        "custom_components.quarter_prices.providers.epex.RETRY_BACKOFF", 0
    )
    aioclient_mock.get(url(), status=503, text="unavailable")
    provider = make_provider(hass)

    with pytest.raises(ProviderConnectionError):
        await provider.async_get_prices_for_date(date(2026, 8, 6))
    assert aioclient_mock.call_count == 3


async def test_belgian_region_is_sent_and_used(hass, aioclient_mock):
    """The Belgian endpoint receives ?region= and parses the response."""
    today = date(2026, 8, 6)
    aioclient_mock.get(
        f"{BE_BASE}/api/v1/prices/{PROVIDER_ID}/quarterly",
        params={"region": "vlaanderen"},
        json=payload(today, time_zone=BE_TZ) | {"region": "vlaanderen"},
    )
    provider = make_provider(hass, market=MARKET_BE, region="vlaanderen")

    blocks = (await provider.async_get_prices_for_date(today))["prices"]

    assert len(blocks) == 96
    assert provider.time_zone == BE_TZ
    assert aioclient_mock.mock_calls[-1][1].query.get("region") == "vlaanderen"


async def test_device_is_named_after_the_provider(hass, aioclient_mock):
    """EPEX entries use the chosen provider as their device name."""
    provider = make_provider(hass)
    assert provider.device_name == "Eneco"
    assert provider.provider_name.startswith("Eneco via epexprijzen.nl")


# --- Provider list ----------------------------------------------------------


async def test_fetch_providers_normalizes_the_list(hass, aioclient_mock):
    """The list endpoint's optional product field is preserved."""
    aioclient_mock.get(
        f"{NL_BASE}/api/v1/providers",
        json={
            "providers": [
                {"id": "eneco", "name": "Eneco"},
                {
                    "id": "frank-energie",
                    "name": "Frank Energie",
                    "product": "Slim terugleveren",
                },
            ]
        },
    )

    providers = await async_fetch_epex_providers(hass, MARKET_NL)

    assert [provider["id"] for provider in providers] == [
        "eneco",
        "frank-energie",
    ]
    assert providers[1]["product"] == "Slim terugleveren"
    assert format_provider_label(providers[1]) == ("Frank Energie — Slim terugleveren")


async def test_fetch_providers_reads_belgian_regions(hass, aioclient_mock):
    """Belgian providers expose their regions and per-region product names."""
    aioclient_mock.get(
        f"{BE_BASE}/api/v1/providers",
        json={
            "providers": [
                {
                    "id": "bolt",
                    "name": "Bolt",
                    "productByRegion": {"vlaanderen": "Bolt Variabel Online"},
                    "regions": ["vlaanderen", "brussel"],
                }
            ]
        },
    )

    providers = await async_fetch_epex_providers(hass, MARKET_BE)

    assert providers[0]["regions"] == ["vlaanderen", "brussel"]
    assert format_provider_label(providers[0], "vlaanderen") == (
        "Bolt — Bolt Variabel Online"
    )
    assert format_provider_label(providers[0]) == "Bolt"


async def test_provider_list_404_is_a_connection_error(hass, aioclient_mock):
    """A provider-list failure is surfaced, not silently treated as empty."""
    aioclient_mock.get(
        f"{NL_BASE}/api/v1/providers", status=404, json={"error": "nope"}
    )

    with pytest.raises(ProviderConnectionError):
        await async_fetch_epex_providers(hass, MARKET_NL)
