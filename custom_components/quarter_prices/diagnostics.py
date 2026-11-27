"""Diagnostics support for the Quarter Prices integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_COUNTRY,
    CONF_EPEX_PROVIDER_ID,
    CONF_MARKET,
    CONF_PROVIDER,
    CONF_REGION,
    CONF_RESOLUTION,
    CONF_TIME_ZONE,
    DEFAULT_COUNTRY,
    DEFAULT_RESOLUTION,
    DEFAULT_TIBBER_TIME_ZONE,
    MARKET_TIMEZONES,
    PROVIDER_EPEX,
    PROVIDER_FRANK,
    PROVIDER_TIBBER,
)
from .market_time import resolve_timezone
from .optimal_period import (
    get_optimal_cheap_duration_minutes,
    get_optimal_cheap_enabled,
    get_optimal_expensive_duration_minutes,
    get_optimal_expensive_enabled,
)
from .price import MARKET_PRICE_FIELD, get_apply_feed_in_vat, get_feed_in_adjustment
from .price_zone import (
    get_cheap_zone_margin_percent,
    get_expensive_zone_margin_percent,
)

if TYPE_CHECKING:
    from . import QuarterPricesConfigEntry

# The Tibber personal access token is a credential and must never appear in a
# diagnostics download. Frank needs no authentication.
TO_REDACT: set[str] = {CONF_ACCESS_TOKEN}


def _market_timezone_name(entry: QuarterPricesConfigEntry) -> str:
    """Return the market timezone an entry's prices are defined in.

    Prefers the loaded coordinator's resolved timezone and falls back to the
    stored entry data, so diagnostics work on an entry that is currently in
    ``SETUP_RETRY``.
    """
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is not None:
        return str(coordinator.time_zone)
    provider = entry.data.get(CONF_PROVIDER, PROVIDER_FRANK)
    if provider in (PROVIDER_TIBBER, PROVIDER_EPEX):
        return str(
            resolve_timezone(entry.data.get(CONF_TIME_ZONE, DEFAULT_TIBBER_TIME_ZONE))
        )
    country = entry.options.get(
        CONF_COUNTRY, entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY)
    )
    return str(resolve_timezone(_country_timezone(country)))


def _country_timezone(country: str) -> str:
    """Return the market timezone name for a Frank country."""
    return MARKET_TIMEZONES.get(country, MARKET_TIMEZONES[DEFAULT_COUNTRY])


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: QuarterPricesConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    ``runtime_data`` only exists while the entry is loaded: Home Assistant
    deletes it on unload, and it is never set when setup fails. The diagnostics
    endpoint has no such precondition — it serves any entry that exists — so
    reading it unguarded would answer with a 500 exactly when the entry is in
    ``SETUP_RETRY`` (the price API was down at startup) and a user is trying to
    collect diagnostics to report that. The settings below need no coordinator,
    so they are always reported.
    """
    coordinator = getattr(entry, "runtime_data", None)
    provider_id = entry.data.get(CONF_PROVIDER, PROVIDER_FRANK)
    country = entry.options.get(
        CONF_COUNTRY, entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY)
    )

    return {
        "entry": {
            "title": entry.title,
            "state": str(entry.state),
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "provider": {
            "id": provider_id,
            "name": (
                coordinator.provider_name if coordinator is not None else provider_id
            ),
            "home_id": entry.data.get("home_id"),
            "home_name": entry.data.get("home_name"),
            "market": entry.data.get(CONF_MARKET),
            "region": entry.data.get(CONF_REGION),
            "epex_provider_id": entry.data.get(CONF_EPEX_PROVIDER_ID),
            "currency": (
                coordinator.currency
                if coordinator is not None
                else entry.data.get("currency")
            ),
        },
        "coordinator": {
            "loaded": coordinator is not None,
            "last_update_success": (
                coordinator.last_update_success if coordinator else None
            ),
            "data": coordinator.data if coordinator else None,
        },
        # The derived cheap/expensive zones themselves already travel inside
        # ``coordinator.data``; only the resolved settings behind them are added
        # here, so the effective values are visible even on a day that yields no
        # zone at all.
        "resolved_options": {
            # The market whose day the prices describe, and the timezone that
            # defines it — the two most useful facts when a day is not
            # validating as complete. Home Assistant's own timezone is shown
            # alongside precisely because the two may legitimately differ.
            "country": country if provider_id == PROVIDER_FRANK else None,
            "resolution": (
                entry.options.get(
                    CONF_RESOLUTION,
                    entry.data.get(CONF_RESOLUTION, DEFAULT_RESOLUTION),
                )
                if provider_id == PROVIDER_EPEX
                else None
            ),
            "market_timezone": _market_timezone_name(entry),
            "homeassistant_timezone": str(hass.config.time_zone),
            "cheap_margin_percent": get_cheap_zone_margin_percent(entry.options),
            "expensive_margin_percent": get_expensive_zone_margin_percent(
                entry.options
            ),
            # The optimal-period settings as they are actually in force. The
            # derived periods themselves already travel inside
            # ``coordinator.data``, so only the resolved settings are added
            # here — enough to explain an unavailable planner entity without
            # dumping a second copy of the daily price array.
            #
            # A duration of ``null`` means the stored value is not a usable
            # duration and the planner is deliberately not running. The raw
            # stored value is not repeated here because ``entry.options`` above
            # already carries it verbatim, which is exactly what a bug report
            # needs to see next to this.
            "optimal_cheap_enabled": get_optimal_cheap_enabled(entry.options),
            "optimal_cheap_duration_minutes": get_optimal_cheap_duration_minutes(
                entry.options
            ),
            "optimal_expensive_enabled": get_optimal_expensive_enabled(entry.options),
            "optimal_expensive_duration_minutes": (
                get_optimal_expensive_duration_minutes(entry.options)
            ),
            "return_price_source": MARKET_PRICE_FIELD,
            "return_price_apply_vat": get_apply_feed_in_vat(entry.options),
            "return_price_feed_in_adjustment": get_feed_in_adjustment(entry.options),
        },
    }
