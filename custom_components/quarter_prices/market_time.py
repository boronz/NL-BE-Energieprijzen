"""The market's own calendar day, independent of Home Assistant's timezone.

Every provider publishes a **market day**: midnight to midnight in the market's
own timezone (Europe/Amsterdam for Frank NL, the home's ``timeZone`` for
Tibber). Home Assistant's configured timezone is a *display* preference — a user
may legitimately run Home Assistant in UTC, or on holiday abroad — and it must
therefore never decide

* which blocks belong to "today" and which to "tomorrow",
* where a day starts and ends when validating that a fetched day is complete,
* what "today" is when the coordinator decides which days to request.

Using Home Assistant's timezone for any of those breaks the integration outright
on a non-CET installation: a UTC Home Assistant sees a Dutch day as two hours
short, so no day is ever accepted as complete, the price-zone entities stay
permanently unavailable, and both days get re-requested on every quarter tick.

This module is the single place that maps a provider's timezone to the calendar
day used by the price data. It is deliberately free of Home Assistant imports so
it can be unit-tested in isolation, like :mod:`.price` and :mod:`.price_zone`.
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .const import DEFAULT_COUNTRY, MARKET_TIMEZONES

# Fallback for any value that cannot be resolved to a real IANA zone. CET is the
# market timezone of every supported provider's home market (NL/BE/DE), and is
# deliberately preferred over UTC, which is the timezone of no price market.
_FALLBACK_TIME_ZONE = "Europe/Amsterdam"


def market_timezone(country: str | None) -> tzinfo:
    """Return the timezone the given Frank country publishes its price day in.

    An unknown or missing country falls back to the default market (NL) rather
    than to UTC. UTC is not the day boundary of any Frank market, so silently
    falling back to it would reintroduce exactly the bug this module exists to
    prevent; the config flow only ever stores a supported country anyway.
    """
    name = MARKET_TIMEZONES.get(
        country or DEFAULT_COUNTRY, MARKET_TIMEZONES[DEFAULT_COUNTRY]
    )
    return ZoneInfo(name)


def resolve_timezone(value: str | tzinfo | None) -> tzinfo:
    """Return ``value`` as a :class:`tzinfo`.

    Accepts an already-resolved timezone (the coordinator passes the provider's
    ``time_zone``) or an IANA name (Tibber returns ``Europe/Amsterdam``). An
    empty, unknown or non-string value falls back to a CET market zone rather
    than to UTC, for the reason described in :func:`market_timezone`.
    """
    if isinstance(value, tzinfo):
        return value
    if isinstance(value, str) and value:
        # A supported Frank country code is a market, not an IANA zone name.
        # Mapping it here keeps every caller (including diagnostics and tests)
        # correct for BE as well as NL.
        country_zone = MARKET_TIMEZONES.get(value.upper())
        try:
            return ZoneInfo(country_zone or value)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return ZoneInfo(_FALLBACK_TIME_ZONE)


def market_now(time_zone: str | tzinfo | None) -> datetime:
    """Return the current instant expressed in the market's timezone."""
    return datetime.now(resolve_timezone(time_zone))


def market_midnight(day: date_type, time_zone: str | tzinfo | None) -> datetime:
    """Return market-local midnight starting ``day``."""
    return datetime.combine(day, time.min, tzinfo=resolve_timezone(time_zone))


def market_day_bounds(
    day: date_type, time_zone: str | tzinfo | None
) -> tuple[datetime, datetime]:
    """Return the market-local start and end instants of ``day``.

    The end is the *next* market midnight, so the span is 23, 24 or 25 hours
    across a daylight-saving transition with no special-casing and no
    assumption that a day holds 96 quarters.
    """
    return (
        market_midnight(day, time_zone),
        market_midnight(day + timedelta(days=1), time_zone),
    )


def market_date_of(moment: datetime, time_zone: str | tzinfo | None) -> date_type:
    """Return the market calendar date an aware instant falls on."""
    return moment.astimezone(resolve_timezone(time_zone)).date()
