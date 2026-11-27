"""Tests for the provider-selecting config flow, reauth and options flow."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.quarter_prices.const import (
    CONF_ACCESS_TOKEN,
    CONF_APPLY_FEED_IN_VAT,
    CONF_CHEAP_ZONE_MARGIN_PERCENT,
    CONF_COUNTRY,
    CONF_CURRENCY,
    CONF_EPEX_PROVIDER_ID,
    CONF_EPEX_PROVIDER_NAME,
    CONF_EXPENSIVE_ZONE_MARGIN_PERCENT,
    CONF_FEED_IN_ADJUSTMENT,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_MARKET,
    CONF_OPTIMAL_CHEAP_DURATION_MINUTES,
    CONF_OPTIMAL_CHEAP_ENABLED,
    CONF_OPTIMAL_EXPENSIVE_DURATION_MINUTES,
    CONF_OPTIMAL_EXPENSIVE_ENABLED,
    CONF_PROVIDER,
    CONF_REGION,
    CONF_RESOLUTION,
    CONF_TIME_ZONE,
    DOMAIN,
    MARKET_BE,
    MARKET_NL,
    PROVIDER_EPEX,
    PROVIDER_FRANK,
    PROVIDER_TIBBER,
    RESOLUTION_HOURLY,
    RESOLUTION_QUARTERLY,
)
from custom_components.quarter_prices.providers import (
    ProviderAuthError,
    ProviderConnectionError,
)

_FETCH_HOMES = "custom_components.quarter_prices.config_flow.async_fetch_homes"
_FETCH_EPEX_PROVIDERS = (
    "custom_components.quarter_prices.config_flow.async_fetch_epex_providers"
)


def home(
    home_id: str = "home-1",
    nickname: str = "Home",
    price_count: int = 96,
    currency: str = "EUR",
    time_zone: str = "Europe/Amsterdam",
) -> dict[str, Any]:
    """Return a home dict in the shape ``async_fetch_homes`` produces."""
    return {
        "id": home_id,
        "time_zone": time_zone,
        "app_nickname": nickname,
        "currency": currency,
        "price_count": price_count,
        "tomorrow_count": 0,
    }


async def start_user_flow(hass):
    """Start a config flow at the provider-selection step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def choose_provider(hass, result, provider: str):
    """Submit the provider-selection step."""
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PROVIDER: provider}
    )


async def test_user_flow_offers_all_sources(hass):
    """The first step lets the user pick Frank, Tibber or the generic EPEX source."""
    result = await start_user_flow(hass)

    schema = result["data_schema"].schema
    provider_key = next(iter(schema))
    selector = schema[provider_key]
    values = [option["value"] for option in selector.config["options"]]
    assert values == [PROVIDER_FRANK, PROVIDER_TIBBER, PROVIDER_EPEX]


async def test_frank_entry_is_created_with_country(hass):
    """Frank needs no credentials and stores the chosen country."""
    result = await choose_provider(hass, await start_user_flow(hass), PROVIDER_FRANK)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "frank"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "BE"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Frank Quarter Prices (BE)"
    assert result["data"] == {CONF_PROVIDER: PROVIDER_FRANK, CONF_COUNTRY: "BE"}
    assert result["result"].unique_id == f"{PROVIDER_FRANK}_BE"


async def test_second_frank_country_is_allowed(hass):
    """A second Frank entry for another market is a legitimate second device."""
    first = await choose_provider(hass, await start_user_flow(hass), PROVIDER_FRANK)
    await hass.config_entries.flow.async_configure(
        first["flow_id"], {CONF_COUNTRY: "NL"}
    )

    second = await choose_provider(hass, await start_user_flow(hass), PROVIDER_FRANK)
    result = await hass.config_entries.flow.async_configure(
        second["flow_id"], {CONF_COUNTRY: "BE"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Frank Quarter Prices (BE)"


async def test_duplicate_frank_country_aborts(hass):
    """The same provider and country can only be configured once."""
    first = await choose_provider(hass, await start_user_flow(hass), PROVIDER_FRANK)
    await hass.config_entries.flow.async_configure(
        first["flow_id"], {CONF_COUNTRY: "NL"}
    )

    second = await choose_provider(hass, await start_user_flow(hass), PROVIDER_FRANK)
    result = await hass.config_entries.flow.async_configure(
        second["flow_id"], {CONF_COUNTRY: "NL"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_tibber_single_home_creates_entry(hass):
    """A token with one usable home creates the entry without a home step."""
    with patch(_FETCH_HOMES, AsyncMock(return_value=[home(nickname="Villa")])) as fetch:
        result = await choose_provider(
            hass, await start_user_flow(hass), PROVIDER_TIBBER
        )
        assert result["step_id"] == "tibber"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: "tibber_pat_abc"}
        )

    assert fetch.await_count == 1
    assert fetch.await_args.args[1] == "tibber_pat_abc"
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Tibber Quarter Prices (Villa)"
    assert result["data"] == {
        CONF_PROVIDER: PROVIDER_TIBBER,
        CONF_ACCESS_TOKEN: "tibber_pat_abc",
        CONF_HOME_ID: "home-1",
        CONF_HOME_NAME: "Villa",
        CONF_TIME_ZONE: "Europe/Amsterdam",
        CONF_CURRENCY: "EUR",
    }
    assert result["result"].unique_id == f"{PROVIDER_TIBBER}_home-1"


async def test_tibber_multiple_homes_asks_which_one(hass):
    """Two usable homes add a selection step, and the chosen one is stored."""
    homes = [
        home("home-1", "House"),
        home("home-2", "Cabin", time_zone="Europe/Brussels"),
    ]
    with patch(_FETCH_HOMES, AsyncMock(return_value=homes)):
        result = await choose_provider(
            hass, await start_user_flow(hass), PROVIDER_TIBBER
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: "tibber_pat_abc"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "tibber_home"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOME_ID: "home-2"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOME_ID] == "home-2"
    assert result["data"][CONF_HOME_NAME] == "Cabin"
    assert result["data"][CONF_TIME_ZONE] == "Europe/Brussels"


async def test_tibber_non_euro_home_is_rejected(hass):
    """Non-euro markets are outside the integration's unit contract."""
    with patch(
        _FETCH_HOMES,
        AsyncMock(return_value=[home(currency="NOK")]),
    ):
        result = await choose_provider(
            hass, await start_user_flow(hass), PROVIDER_TIBBER
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: "tibber_pat_abc"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unsupported_currency"}


async def test_tibber_invalid_token_shows_error(hass):
    """A rejected token keeps the form open with an explanatory error."""
    with patch(_FETCH_HOMES, AsyncMock(side_effect=ProviderAuthError("bad"))):
        result = await choose_provider(
            hass, await start_user_flow(hass), PROVIDER_TIBBER
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: "wrong"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.parametrize(
    ("homes", "expected"),
    [
        ([], "no_homes"),
        ([home(price_count=0)], "no_price_info"),
    ],
)
async def test_tibber_account_without_usable_prices(hass, homes, expected):
    """An account with no homes or no price information gets a precise error."""
    with patch(_FETCH_HOMES, AsyncMock(return_value=homes)):
        result = await choose_provider(
            hass, await start_user_flow(hass), PROVIDER_TIBBER
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: "tibber_pat_abc"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


async def test_tibber_connection_error(hass):
    """A transport failure is reported as cannot_connect."""
    with patch(_FETCH_HOMES, AsyncMock(side_effect=ProviderConnectionError("down"))):
        result = await choose_provider(
            hass, await start_user_flow(hass), PROVIDER_TIBBER
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: "tibber_pat_abc"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_empty_token_is_rejected_without_a_request(hass):
    """The token field is validated before any network call."""
    with patch(_FETCH_HOMES, AsyncMock()) as fetch:
        result = await choose_provider(
            hass, await start_user_flow(hass), PROVIDER_TIBBER
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: "   "}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ACCESS_TOKEN: "invalid_token"}
    assert fetch.await_count == 0


# --- Reauthentication --------------------------------------------------------


def tibber_entry() -> MockConfigEntry:
    """Return a configured Tibber entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Tibber Quarter Prices (Home)",
        data={
            CONF_PROVIDER: PROVIDER_TIBBER,
            CONF_ACCESS_TOKEN: "old-token",
            CONF_HOME_ID: "home-1",
            CONF_HOME_NAME: "Home",
            CONF_TIME_ZONE: "Europe/Amsterdam",
            CONF_CURRENCY: "EUR",
        },
        unique_id="tibber_home-1",
    )


async def test_reauth_updates_the_token(hass):
    """A replacement token for the same home updates the entry and aborts."""
    entry = tibber_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(_FETCH_HOMES, AsyncMock(return_value=[home()])):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: "new-token"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_ACCESS_TOKEN] == "new-token"


async def test_reauth_rejects_a_different_home(hass):
    """A token for another home must not silently rebind the entry."""
    entry = tibber_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
    )
    with patch(
        _FETCH_HOMES,
        AsyncMock(return_value=[home(home_id="other-home")]),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: "new-token"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_account"}
    assert entry.data[CONF_ACCESS_TOKEN] == "old-token"


# --- Options flow ------------------------------------------------------------


async def test_options_flow_keeps_all_values(hass):
    """The provider-neutral options save exactly what was submitted."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Frank Quarter Prices (NL)",
        data={CONF_PROVIDER: PROVIDER_FRANK, CONF_COUNTRY: "NL"},
        unique_id="frank_NL",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    submitted = {
        CONF_FEED_IN_ADJUSTMENT: -0.01,
        CONF_APPLY_FEED_IN_VAT: True,
        CONF_CHEAP_ZONE_MARGIN_PERCENT: 10,
        CONF_EXPENSIVE_ZONE_MARGIN_PERCENT: 20,
        CONF_OPTIMAL_CHEAP_ENABLED: True,
        CONF_OPTIMAL_CHEAP_DURATION_MINUTES: 120,
        CONF_OPTIMAL_EXPENSIVE_ENABLED: True,
        CONF_OPTIMAL_EXPENSIVE_DURATION_MINUTES: 60,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], submitted
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == submitted


async def test_options_flow_rejects_invalid_duration(hass):
    """The step check catches a duration the selector would let through."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Frank Quarter Prices (NL)",
        data={CONF_PROVIDER: PROVIDER_FRANK, CONF_COUNTRY: "NL"},
        unique_id="frank_NL",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_OPTIMAL_CHEAP_ENABLED: True,
            CONF_OPTIMAL_CHEAP_DURATION_MINUTES: 17,
            CONF_OPTIMAL_EXPENSIVE_ENABLED: False,
            CONF_OPTIMAL_EXPENSIVE_DURATION_MINUTES: 120,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_OPTIMAL_CHEAP_DURATION_MINUTES: "invalid_duration"}


# --- EPEX (generic provider) ------------------------------------------------


def epex_provider(
    provider_id: str = "eneco",
    name: str = "Eneco",
    product: str | None = None,
    regions: list[str] | None = None,
    product_by_region: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a provider dict in the shape the EPEX list endpoint produces."""
    return {
        "id": provider_id,
        "name": name,
        "product": product,
        "product_by_region": product_by_region or {},
        "regions": regions or [],
    }


async def test_epex_nl_flow_creates_provider_entry(hass):
    """A Dutch EPEX entry stores the chosen provider and resolution."""
    providers = [epex_provider(), epex_provider("zonneplan", "Zonneplan")]
    with patch(_FETCH_EPEX_PROVIDERS, AsyncMock(return_value=providers)):
        result = await choose_provider(hass, await start_user_flow(hass), PROVIDER_EPEX)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "epex_market"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MARKET: MARKET_NL}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "epex_provider"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EPEX_PROVIDER_ID: "eneco",
                CONF_RESOLUTION: RESOLUTION_QUARTERLY,
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Eneco Quarter Prices"
    assert result["data"] == {
        CONF_PROVIDER: PROVIDER_EPEX,
        CONF_MARKET: MARKET_NL,
        CONF_EPEX_PROVIDER_ID: "eneco",
        CONF_EPEX_PROVIDER_NAME: "Eneco",
        CONF_RESOLUTION: RESOLUTION_QUARTERLY,
        CONF_TIME_ZONE: "Europe/Amsterdam",
        CONF_CURRENCY: "EUR",
    }
    assert result["result"].unique_id == "epex_nl__eneco"


async def test_epex_belgium_flow_asks_region_and_filters_providers(hass):
    """Belgian setup filters the list by region and forwards ?region= later."""
    providers = [
        epex_provider(
            "bolt",
            "Bolt",
            regions=["vlaanderen", "brussel"],
            product_by_region={"vlaanderen": "Bolt Variabel Online"},
        ),
        epex_provider("cociter", "Cociter", regions=["wallonie"]),
    ]
    with patch(_FETCH_EPEX_PROVIDERS, AsyncMock(return_value=providers)):
        result = await choose_provider(hass, await start_user_flow(hass), PROVIDER_EPEX)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MARKET: MARKET_BE}
        )
        assert result["step_id"] == "epex_region"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REGION: "vlaanderen"}
        )
        assert result["step_id"] == "epex_provider"

        schema = result["data_schema"].schema
        provider_key = next(key for key in schema if str(key) == CONF_EPEX_PROVIDER_ID)
        values = [option["value"] for option in schema[provider_key].config["options"]]
        assert values == ["bolt"]

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EPEX_PROVIDER_ID: "bolt",
                CONF_RESOLUTION: RESOLUTION_HOURLY,
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_REGION] == "vlaanderen"
    assert result["data"][CONF_TIME_ZONE] == "Europe/Brussels"
    assert result["data"][CONF_RESOLUTION] == RESOLUTION_HOURLY
    assert result["result"].unique_id == "epex_be_vlaanderen_bolt"


async def test_epex_provider_list_failure_shows_connect_error(hass):
    """A failed provider-list request keeps the market form with an error."""
    with patch(
        _FETCH_EPEX_PROVIDERS,
        AsyncMock(side_effect=ProviderConnectionError("down")),
    ):
        result = await choose_provider(hass, await start_user_flow(hass), PROVIDER_EPEX)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MARKET: MARKET_NL}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "epex_market"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_epex_empty_provider_list_shows_error(hass):
    """An empty list is not treated as a successful setup."""
    with patch(_FETCH_EPEX_PROVIDERS, AsyncMock(return_value=[])):
        result = await choose_provider(hass, await start_user_flow(hass), PROVIDER_EPEX)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MARKET: MARKET_NL}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_providers"}


async def test_duplicate_epex_provider_aborts(hass):
    """The same provider/market/region can only be configured once."""
    with patch(_FETCH_EPEX_PROVIDERS, AsyncMock(return_value=[epex_provider()])):
        first = await choose_provider(hass, await start_user_flow(hass), PROVIDER_EPEX)
        first = await hass.config_entries.flow.async_configure(
            first["flow_id"], {CONF_MARKET: MARKET_NL}
        )
        first = await hass.config_entries.flow.async_configure(
            first["flow_id"],
            {
                CONF_EPEX_PROVIDER_ID: "eneco",
                CONF_RESOLUTION: RESOLUTION_QUARTERLY,
            },
        )
        assert first["type"] is FlowResultType.CREATE_ENTRY

        second = await choose_provider(hass, await start_user_flow(hass), PROVIDER_EPEX)
        second = await hass.config_entries.flow.async_configure(
            second["flow_id"], {CONF_MARKET: MARKET_NL}
        )
        second = await hass.config_entries.flow.async_configure(
            second["flow_id"],
            {
                CONF_EPEX_PROVIDER_ID: "eneco",
                CONF_RESOLUTION: RESOLUTION_HOURLY,
            },
        )

    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_configured"


async def test_epex_options_expose_and_save_resolution(hass):
    """The EPEX options form exposes the quarter/hour choice and saves it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Eneco Quarter Prices",
        data={
            CONF_PROVIDER: PROVIDER_EPEX,
            CONF_MARKET: MARKET_NL,
            CONF_EPEX_PROVIDER_ID: "eneco",
            CONF_EPEX_PROVIDER_NAME: "Eneco",
            CONF_RESOLUTION: RESOLUTION_QUARTERLY,
            CONF_TIME_ZONE: "Europe/Amsterdam",
            CONF_CURRENCY: "EUR",
        },
        unique_id="epex_nl__eneco",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    keys = {str(key) for key in result["data_schema"].schema}
    assert CONF_RESOLUTION in keys

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_FEED_IN_ADJUSTMENT: 0.0,
            CONF_APPLY_FEED_IN_VAT: False,
            CONF_CHEAP_ZONE_MARGIN_PERCENT: 15,
            CONF_EXPENSIVE_ZONE_MARGIN_PERCENT: 15,
            CONF_OPTIMAL_CHEAP_ENABLED: False,
            CONF_OPTIMAL_CHEAP_DURATION_MINUTES: 120,
            CONF_OPTIMAL_EXPENSIVE_ENABLED: False,
            CONF_OPTIMAL_EXPENSIVE_DURATION_MINUTES: 120,
            CONF_RESOLUTION: RESOLUTION_HOURLY,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_RESOLUTION] == RESOLUTION_HOURLY


async def test_epex_entry_does_not_need_reauth(hass):
    """The EPEX source has no credentials, so reauth is rejected cleanly."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Eneco Quarter Prices",
        data={
            CONF_PROVIDER: PROVIDER_EPEX,
            CONF_MARKET: MARKET_NL,
            CONF_EPEX_PROVIDER_ID: "eneco",
            CONF_EPEX_PROVIDER_NAME: "Eneco",
            CONF_RESOLUTION: RESOLUTION_QUARTERLY,
        },
        unique_id="epex_nl__eneco",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_not_supported"
