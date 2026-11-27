"""Unit tests for the Tibber provider: parsing, fallback, auth and caching."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.quarter_prices.const import (
    CONF_ACCESS_TOKEN,
    CONF_CURRENCY,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_PROVIDER,
    CONF_TIME_ZONE,
    DOMAIN,
    PROVIDER_TIBBER,
)
from custom_components.quarter_prices.price import (
    calculate_dynamic_price,
    calculate_feed_in_price,
)
from custom_components.quarter_prices.providers.base import (
    ProviderAuthError,
    ProviderConnectionError,
)
from custom_components.quarter_prices.providers.tibber import (
    TIBBER_API_ENDPOINT,
    TibberProvider,
    _async_post,
    _ResolutionNotSupported,
    async_fetch_homes,
)

TZ = ZoneInfo("Europe/Amsterdam")
HOME_ID = "home-1"
TOKEN = "tibber_pat_test"

_ASYNC_POST = "custom_components.quarter_prices.providers.tibber._async_post"


def entry_data(**overrides: Any) -> dict[str, Any]:
    """Return a valid Tibber config-entry data dict."""
    data = {
        CONF_PROVIDER: PROVIDER_TIBBER,
        CONF_ACCESS_TOKEN: TOKEN,
        CONF_HOME_ID: HOME_ID,
        CONF_HOME_NAME: "Home",
        CONF_TIME_ZONE: str(TZ),
        CONF_CURRENCY: "EUR",
    }
    data.update(overrides)
    return data


def tibber_entries(
    day: date,
    resolution_minutes: int = 15,
    energy: float = 0.08,
    tax: float = 0.05,
    currency: str = "EUR",
) -> list[dict[str, Any]]:
    """Build Tibber price entries covering the full market day, in absolute time.

    Stepping in UTC is what the API's timestamps describe, so the spring-forward
    day yields 92 and the fall-back day 100 entries without special-casing.
    """
    cursor = datetime.combine(day, time.min, tzinfo=TZ).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=TZ).astimezone(UTC)
    entries: list[dict[str, Any]] = []
    step = timedelta(minutes=resolution_minutes)
    index = 0
    while cursor < end:
        entries.append(
            {
                "startsAt": cursor.astimezone(TZ).isoformat(),
                "total": round(energy + tax + index * 0.0001, 6),
                "energy": energy,
                "tax": tax,
                "currency": currency,
            }
        )
        cursor += step
        index += 1
    return entries


def price_info(
    today: list[dict[str, Any]] | None = None,
    tomorrow: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the ``priceInfo`` object shape the provider parses."""
    return {"today": today or [], "tomorrow": tomorrow or []}


def graphql_data(
    price_info_payload: dict[str, Any],
    *,
    home_id: str = HOME_ID,
    time_zone: str = str(TZ),
    nickname: str = "Home",
    subscription: bool = True,
) -> dict[str, Any]:
    """Return a GraphQL ``data`` object with one home."""
    home: dict[str, Any] = {
        "id": home_id,
        "timeZone": time_zone,
        "appNickname": nickname,
        "currentSubscription": (
            {"priceInfo": price_info_payload} if subscription else None
        ),
    }
    return {"viewer": {"homes": [home]}}


def make_provider(hass, **overrides: Any) -> TibberProvider:
    """Create a provider bound to a mock config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data(**overrides),
        unique_id=f"tibber_{HOME_ID}",
    )
    return TibberProvider(hass, entry)


def bare_provider(time_zone: ZoneInfo = TZ) -> TibberProvider:
    """Create a provider without Home Assistant, for pure parsing tests."""
    provider = TibberProvider.__new__(TibberProvider)
    provider._time_zone = time_zone
    provider._time_zone_name = str(time_zone)
    return provider


# --- Parsing ----------------------------------------------------------------


async def test_quarter_hour_day_is_normalized():
    """A 96-entry Tibber day becomes 96 contiguous quarter-hour blocks."""
    provider = bare_provider()
    day = date(2026, 8, 6)
    blocks = provider._parse_entries(tibber_entries(day))

    assert len(blocks) == 96
    assert {block["duration_minutes"] for block in blocks} == {15}
    first, last = blocks[0], blocks[-1]
    assert first["from"].astimezone(TZ).strftime("%H:%M") == "00:00"
    assert first["market_price"] == pytest.approx(0.08)
    assert first["market_price_tax"] == pytest.approx(0.05)
    assert first["total_price_eur_kwh"] == pytest.approx(0.13)
    assert last["till"].astimezone(TZ).strftime("%H:%M") == "00:00"
    assert dt_util.as_utc(last["till"]) == dt_util.as_utc(
        datetime.combine(day + timedelta(days=1), time.min, tzinfo=TZ)
    )
    # The Tibber tax is the whole tax component, and the shared sum must still
    # reproduce the all-in price.
    assert calculate_dynamic_price(first) == pytest.approx(0.13)
    # Feed-in is based on the raw spot price.
    assert calculate_feed_in_price(first, 0.0) == pytest.approx(0.08)


async def test_hourly_fallback_day_is_normalized():
    """Hourly entries become 60-minute blocks with the same field mapping."""
    provider = bare_provider()
    day = date(2026, 8, 6)
    blocks = provider._parse_entries(tibber_entries(day, resolution_minutes=60))

    assert len(blocks) == 24
    assert {block["duration_minutes"] for block in blocks} == {60}
    assert blocks[-1]["till"].astimezone(TZ).strftime("%H:%M") == "00:00"


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 3, 29), 92),  # spring forward: 23-hour day
        (date(2026, 10, 25), 100),  # fall back: 25-hour day
    ],
)
async def test_dst_days_have_the_right_block_count_and_no_gaps(day, expected):
    """DST days are continuous in absolute time and keep 15-minute durations."""
    provider = bare_provider()
    blocks = provider._parse_entries(tibber_entries(day))

    assert len(blocks) == expected
    assert {block["duration_minutes"] for block in blocks} == {15}
    for previous, block in pairwise(blocks):
        assert dt_util.as_utc(previous["till"]) == dt_util.as_utc(block["from"])
    # Complete against the real market-day span, as the coordinator validates.
    assert dt_util.as_utc(blocks[0]["from"]) == dt_util.as_utc(
        datetime.combine(day, time.min, tzinfo=TZ)
    )
    assert dt_util.as_utc(blocks[-1]["till"]) == dt_util.as_utc(
        datetime.combine(day + timedelta(days=1), time.min, tzinfo=TZ)
    )


async def test_missing_tax_is_derived_from_total_and_energy():
    """A record without ``tax`` is completed from the two other components."""
    provider = bare_provider()
    blocks = provider._parse_entries(
        [
            {
                "startsAt": "2026-08-06T00:00:00+02:00",
                "total": 0.2,
                "energy": 0.08,
                "currency": "EUR",
            },
            {
                "startsAt": "2026-08-06T00:15:00+02:00",
                "total": 0.3,
                "energy": 0.18,
                "currency": "EUR",
            },
        ]
    )

    assert len(blocks) == 2
    assert blocks[0]["market_price"] == pytest.approx(0.08)
    assert blocks[0]["market_price_tax"] == pytest.approx(0.12)
    assert blocks[0]["total_price_eur_kwh"] == pytest.approx(0.2)


async def test_record_without_energy_is_skipped():
    """Without a spot price the block cannot support the feed-in estimate."""
    provider = bare_provider()
    blocks = provider._parse_entries(
        [
            {
                "startsAt": "2026-08-06T00:00:00+02:00",
                "total": 0.2,
                "tax": 0.12,
                "currency": "EUR",
            }
        ]
    )

    assert blocks == []


# --- Fetching, fallback and caching -----------------------------------------


async def test_fetch_parses_today_and_tomorrow_and_caches(hass):
    """One HTTP response serves both days and is reused within the TTL."""
    provider = make_provider(hass)
    today, tomorrow = date(2026, 8, 6), date(2026, 8, 7)
    data = graphql_data(price_info(tibber_entries(today), tibber_entries(tomorrow)))

    with patch(_ASYNC_POST, AsyncMock(return_value=data)) as post:
        today_prices = await provider.async_get_prices_for_date(today)
        tomorrow_prices = await provider.async_get_prices_for_date(tomorrow)

    assert len(today_prices["prices"]) == 96
    assert len(tomorrow_prices["prices"]) == 96
    assert post.await_count == 1


async def test_unpublished_tomorrow_is_an_empty_list_not_an_error(hass):
    """Tibber returns an empty tomorrow before the day-ahead auction."""
    provider = make_provider(hass)
    today, tomorrow = date(2026, 8, 6), date(2026, 8, 7)
    data = graphql_data(price_info(tibber_entries(today), []))

    with patch(_ASYNC_POST, AsyncMock(return_value=data)):
        assert len((await provider.async_get_prices_for_date(today))["prices"]) == 96
        assert (await provider.async_get_prices_for_date(tomorrow))["prices"] == []


async def test_rejected_resolution_falls_back_to_hourly(hass):
    """An old API that rejects QUARTER_HOURLY is retried once, then remembered."""
    provider = make_provider(hass)
    today = date(2026, 8, 6)
    data = graphql_data(price_info(tibber_entries(today, resolution_minutes=60)))

    post = AsyncMock(side_effect=[_ResolutionNotSupported("unknown argument"), data])
    with patch(_ASYNC_POST, post):
        result = await provider.async_get_prices_for_date(today)

    assert len(result["prices"]) == 24
    assert {block["duration_minutes"] for block in result["prices"]} == {60}
    assert provider._use_quarter is False
    assert post.await_count == 2

    # The next (cache-expired) fetch goes straight to the hourly query.
    provider._loaded_at = None
    post2 = AsyncMock(return_value=data)
    with patch(_ASYNC_POST, post2):
        await provider.async_get_prices_for_date(today)
    assert post2.await_count == 1


async def test_empty_quarter_result_retries_hourly(hass):
    """A schema that accepts the argument but returns no quarters is retried."""
    provider = make_provider(hass)
    today = date(2026, 8, 6)
    empty = graphql_data(price_info([], []))
    hourly = graphql_data(price_info(tibber_entries(today, resolution_minutes=60)))

    with patch(_ASYNC_POST, AsyncMock(side_effect=[empty, hourly])) as post:
        result = await provider.async_get_prices_for_date(today)

    assert len(result["prices"]) == 24
    assert post.await_count == 2
    assert provider._use_quarter is False


async def test_selected_home_is_respected(hass):
    """With several homes, only the configured home's prices are used."""
    provider = make_provider(hass)
    today = date(2026, 8, 6)
    other_home = {
        "id": "other",
        "timeZone": str(TZ),
        "appNickname": "Other",
        "currentSubscription": {"priceInfo": price_info([], [])},
    }
    wanted = graphql_data(price_info(tibber_entries(today)))
    wanted["viewer"]["homes"].append(other_home)

    with patch(_ASYNC_POST, AsyncMock(return_value=wanted)):
        result = await provider.async_get_prices_for_date(today)

    assert len(result["prices"]) == 96


async def test_home_missing_from_account_is_an_error(hass):
    """A token that no longer sees the configured home fails clearly."""
    provider = make_provider(hass)
    today = date(2026, 8, 6)
    data = graphql_data(price_info(tibber_entries(today)), home_id="someone-else")

    with (
        patch(_ASYNC_POST, AsyncMock(return_value=data)),
        pytest.raises(ProviderConnectionError),
    ):
        await provider.async_get_prices_for_date(today)


# --- HTTP / authentication ---------------------------------------------------


async def test_401_raises_auth_error(hass, aioclient_mock):
    """A rejected token is reported as an authentication error, not a retry."""
    aioclient_mock.post(TIBBER_API_ENDPOINT, status=401, text="Unauthorized")

    with pytest.raises(ProviderAuthError):
        await _async_post(hass, "bad-token", "query { viewer { id } }")
    assert aioclient_mock.call_count == 1


async def test_graphql_auth_error_with_http_200_is_an_auth_error(hass, aioclient_mock):
    """Tibber reports a bad token as HTTP 200 with an UNAUTHENTICATED error."""
    aioclient_mock.post(
        TIBBER_API_ENDPOINT,
        json={
            "errors": [
                {
                    "message": "invalid token",
                    "locations": [{"line": 1, "column": 9}],
                    "path": ["viewer"],
                    "extensions": {"code": "UNAUTHENTICATED"},
                }
            ],
            "data": None,
        },
    )

    with pytest.raises(ProviderAuthError):
        await _async_post(hass, TOKEN, "query { viewer { id } }")


async def test_graphql_resolution_error_triggers_fallback(hass, aioclient_mock):
    """A schema rejection of QUARTER_HOURLY is signalled to the fallback logic."""
    aioclient_mock.post(
        TIBBER_API_ENDPOINT,
        json={
            "errors": [
                {
                    "message": 'Unknown argument "resolution" on field '
                    '"Subscription.priceInfo".'
                }
            ]
        },
    )

    with pytest.raises(_ResolutionNotSupported):
        await _async_post(hass, TOKEN, "query { viewer { id } }")


async def test_server_errors_are_retried(hass, aioclient_mock, monkeypatch):
    """5xx responses are retried and eventually surface as a connection error."""
    monkeypatch.setattr(
        "custom_components.quarter_prices.providers.tibber.RETRY_BACKOFF", 0
    )
    aioclient_mock.post(TIBBER_API_ENDPOINT, status=503, text="unavailable")

    with pytest.raises(ProviderConnectionError):
        await _async_post(hass, TOKEN, "query { viewer { id } }")
    assert aioclient_mock.call_count == 3


async def test_fetch_homes_reads_counts_currency_and_timezone(hass, aioclient_mock):
    """Home discovery returns what the config flow needs for its labels."""
    today = date(2026, 8, 6)
    payload = {
        "data": graphql_data(
            price_info(tibber_entries(today), []),
            nickname="House",
        )
    }
    aioclient_mock.post(TIBBER_API_ENDPOINT, json=payload)

    homes = await async_fetch_homes(hass, TOKEN)

    assert homes == [
        {
            "id": HOME_ID,
            "time_zone": str(TZ),
            "app_nickname": "House",
            "currency": "EUR",
            "price_count": 96,
            "tomorrow_count": 0,
        }
    ]


async def test_fetch_homes_handles_home_without_subscription(hass, aioclient_mock):
    """A home without a subscription is listed with no prices, not dropped."""
    payload = {"data": graphql_data(price_info(), subscription=False, nickname="Cabin")}
    aioclient_mock.post(TIBBER_API_ENDPOINT, json=payload)

    homes = await async_fetch_homes(hass, TOKEN)

    assert len(homes) == 1
    assert homes[0]["price_count"] == 0
    assert homes[0]["currency"] == "EUR"
