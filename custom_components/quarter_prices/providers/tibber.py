"""Tibber price provider.

Tibber exposes its prices through a small authenticated GraphQL API at
``https://api.tibber.com/v1-beta/gql``. This provider uses the
``currentSubscription.priceInfo`` field with an explicit
``resolution: QUARTER_HOURLY`` argument, which returns native 15-minute blocks
for markets that publish them (the Netherlands, Belgium and Germany since the
SDAC moved to 15-minute market time units on 1 October 2025). Older API
deployments reject the argument; the provider then falls back to the plain
``priceInfo`` field (hourly blocks) exactly once and remembers that choice.

Authentication is a personal access token created at
https://developer.tibber.com/ and sent as a ``Bearer`` token. The token is stored
in the config entry only.

Field notes (verified against the published Tibber schema):

* ``Price.energy``  -- Nord Pool spot price, excluding VAT and energy tax. This
  is the feed-in base used by :mod:`.price`.
* ``Price.tax``     -- the tax part of the price (VAT plus energy tax, plus
  guarantee-of-origin certificates where applicable).
* ``Price.total``   -- ``energy + tax``: the all-in consumer price.
* ``Home.timeZone`` -- the IANA timezone that defines the price day.

Tibber does not split its markup out of ``tax``, so a Tibber block reports the
whole tax part as ``market_price_tax`` with a zero sourcing markup and zero
separately billed energy tax. The shared sum
``market_price + market_price_tax + sourcing_markup_price + energy_tax_price``
therefore still equals ``total_price_eur_kwh``, and the *Current price* sensor
shows Tibber's all-in price, just as the Frank provider shows Frank's all-in
dynamic component.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import date as date_type
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

import aiohttp
from aiohttp import ClientError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_ACCESS_TOKEN,
    CONF_CURRENCY,
    CONF_HOME_ID,
    CONF_TIME_ZONE,
    DEFAULT_CURRENCY,
    DEFAULT_TIBBER_TIME_ZONE,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
)
from ..market_time import market_date_of, resolve_timezone
from .base import (
    PriceProvider,
    ProviderAuthError,
    ProviderConnectionError,
)

_LOGGER = logging.getLogger(__name__)

TIBBER_API_ENDPOINT = "https://api.tibber.com/v1-beta/gql"

# Delay (seconds) between retry attempts for transport-level failures.
RETRY_BACKOFF = 2

# How long a fetched today/tomorrow pair may be reused. The coordinator fetches
# today and tomorrow back to back on every quarter tick; without this cache that
# is two identical HTTP requests each time. One minute is short enough that a
# newly published tomorrow is visible on the next (15-minute) tick.
_CACHE_SECONDS = 55

# The two GraphQL queries. Everything except the ``priceInfo`` selection is
# identical; keeping them as literal strings makes the fallback trivial and
# avoids sending a variable of an enum type an older schema may not know.
_PRICE_FIELDS = """
          today {
            startsAt
            total
            energy
            tax
            currency
          }
          tomorrow {
            startsAt
            total
            energy
            tax
            currency
          }"""

_QUARTER_QUERY = f"""
query QuarterPrices {{
  viewer {{
    homes {{
      id
      timeZone
      appNickname
      currentSubscription {{
        priceInfo(resolution: QUARTER_HOURLY) {{{_PRICE_FIELDS}
        }}
      }}
    }}
  }}
}}
"""

_HOURLY_QUERY = f"""
query QuarterPrices {{
  viewer {{
    homes {{
      id
      timeZone
      appNickname
      currentSubscription {{
        priceInfo {{{_PRICE_FIELDS}
        }}
      }}
    }}
  }}
}}
"""


class _ResolutionNotSupported(Exception):
    """Internal: the API rejected the quarter-hourly ``resolution`` argument."""


def _as_float(value: Any) -> float | None:
    """Return ``value`` as a float, rejecting booleans and non-numerics."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _looks_like_resolution_error(text: str) -> bool:
    """Return whether an API error is about the ``resolution`` argument.

    The exact wording differs between GraphQL validation errors and a proxy
    rejecting the request, so match the stable tokens rather than one message.
    """
    lowered = text.lower()
    return any(
        token in lowered
        for token in (
            "quarter_hourly",
            "unknown argument",
            "unknown field",
            "cannot query field",
            "resolution",
        )
    )


def _is_auth_error(status: int, text: str) -> bool:
    """Return whether a response means the access token is unusable."""
    if status in (401, 403):
        return True
    lowered = text.lower()
    return any(
        token in lowered
        for token in ("unauthenticated", "forbidden", "invalid token", "access token")
    )


async def _async_post(hass, access_token: str, query: str) -> dict[str, Any]:
    """Execute one GraphQL query and return its ``data`` object.

    Raises :class:`ProviderAuthError` for bad credentials, a private
    ``_ResolutionNotSupported`` for a schema that does not know the resolution
    argument, and :class:`ProviderConnectionError` for everything else.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    session = async_get_clientsession(hass)
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.post(
                TIBBER_API_ENDPOINT,
                json={"query": query},
                headers=headers,
                timeout=timeout,
            ) as response:
                status = response.status
                text = await response.text()
        except (TimeoutError, ClientError) as err:
            last_error = err
            _LOGGER.warning(
                "Tibber API request failed (attempt %d/%d): %s",
                attempt,
                MAX_RETRIES,
                err,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
            continue

        if _is_auth_error(status, text):
            raise ProviderAuthError("Tibber rejected the access token")

        if status == 400:
            # A schema validation error is how an older API rejects the
            # quarter-hourly argument. Anything else on a 400 is fatal.
            if _looks_like_resolution_error(text):
                raise _ResolutionNotSupported(text[:200])
            raise ProviderConnectionError(f"Tibber API returned HTTP 400: {text[:200]}")

        if status >= 500:
            last_error = ProviderConnectionError(f"Tibber API returned HTTP {status}")
            _LOGGER.warning(
                "Tibber API returned HTTP %d (attempt %d/%d)",
                status,
                attempt,
                MAX_RETRIES,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
            continue

        if status != 200:
            raise ProviderConnectionError(f"Tibber API returned HTTP {status}")

        try:
            payload = json.loads(text)
        except ValueError as err:
            raise ProviderConnectionError("Tibber API returned invalid JSON") from err

        if not isinstance(payload, dict):
            raise ProviderConnectionError("Tibber API returned an unexpected payload")

        if errors := payload.get("errors"):
            message = json.dumps(errors)
            if _is_auth_error(status, message):
                raise ProviderAuthError("Tibber rejected the access token")
            if _looks_like_resolution_error(message):
                raise _ResolutionNotSupported(message[:200])
            raise ProviderConnectionError(f"Tibber GraphQL errors: {errors}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderConnectionError("Tibber response has no data")
        return data

    raise ProviderConnectionError(
        f"Failed to reach the Tibber API after {MAX_RETRIES} attempts"
    ) from last_error


def _price_info_of(home: dict[str, Any]) -> dict[str, Any]:
    """Return a home's ``priceInfo`` object, or an empty dict."""
    subscription = home.get("currentSubscription") or {}
    if not isinstance(subscription, dict):
        return {}
    info = subscription.get("priceInfo") or {}
    return info if isinstance(info, dict) else {}


def _homes_of(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the homes list from a GraphQL ``data`` object."""
    viewer = data.get("viewer") or {}
    homes = viewer.get("homes") if isinstance(viewer, dict) else None
    if not isinstance(homes, list):
        return []
    return [home for home in homes if isinstance(home, dict)]


async def _async_fetch_homes(hass, access_token: str) -> list[dict[str, Any]]:
    """Fetch every home visible to ``access_token``, with its price counts.

    Shared by the config flow (validation and home selection) and the provider.
    Prefers quarter-hourly prices and falls back to the hourly query once.
    """
    try:
        data = await _async_post(hass, access_token, _QUARTER_QUERY)
    except _ResolutionNotSupported:
        _LOGGER.info(
            "Tibber API does not support the quarter-hourly resolution "
            "argument; falling back to hourly prices"
        )
        data = await _async_post(hass, access_token, _HOURLY_QUERY)

    homes: list[dict[str, Any]] = []
    for home in _homes_of(data):
        info = _price_info_of(home)
        today = info.get("today") or []
        tomorrow = info.get("tomorrow") or []
        currency = next(
            (
                entry.get("currency")
                for entry in today
                if isinstance(entry, dict) and entry.get("currency")
            ),
            None,
        )
        homes.append(
            {
                "id": home.get("id"),
                "time_zone": home.get("timeZone") or DEFAULT_TIBBER_TIME_ZONE,
                "app_nickname": home.get("appNickname"),
                "currency": currency or DEFAULT_CURRENCY,
                "price_count": len(today),
                "tomorrow_count": len(tomorrow),
            }
        )
    return homes


async def async_fetch_homes(hass, access_token: str) -> list[dict[str, Any]]:
    """Public wrapper around :func:`_async_fetch_homes` for the config flow."""
    return await _async_fetch_homes(hass, access_token)


class TibberProvider(PriceProvider):
    """Tibber implementation of :class:`PriceProvider`."""

    provider_id = "tibber"
    provider_name = "Tibber"
    device_name = "Tibber"
    manufacturer = "Tibber AS"
    model = "Tibber Price Info"
    requires_auth = True

    def __init__(self, hass, entry) -> None:
        """Initialize the provider from a config entry."""
        super().__init__(hass, entry)
        self._access_token = entry.data[CONF_ACCESS_TOKEN]
        self._home_id = entry.data[CONF_HOME_ID]
        self._time_zone_name = (
            entry.data.get(CONF_TIME_ZONE) or DEFAULT_TIBBER_TIME_ZONE
        )
        self._time_zone = resolve_timezone(self._time_zone_name)
        self._currency = entry.data.get(CONF_CURRENCY) or DEFAULT_CURRENCY
        # Which query form the API last accepted. Starts optimistic and is
        # downgraded once, permanently for this provider instance.
        self._use_quarter = True
        # Parsed blocks per market date, kept for ``_CACHE_SECONDS`` so the
        # coordinator's today+tomorrow fetch costs one HTTP request.
        self._days: dict[date_type, list[dict[str, Any]]] = {}
        self._loaded_at: float | None = None

    @property
    def time_zone(self):
        """Return the home's market timezone."""
        return self._time_zone

    @property
    def currency(self) -> str:
        """Return the currency of the configured home's prices."""
        return self._currency

    async def async_get_prices_for_date(self, target_date: date_type) -> dict[str, Any]:
        """Return the normalized price blocks for one market day."""
        if not self._cache_is_fresh():
            await self._async_load()
        prices = list(self._days.get(target_date, []))
        _LOGGER.debug(
            "Tibber: %d price block(s) for %s (resolution=%s)",
            len(prices),
            target_date.isoformat(),
            self._resolution_label(prices),
        )
        return {"prices": prices}

    def _resolution_label(self, prices: list[dict[str, Any]]) -> str:
        """Describe the resolution of a block list for the debug log."""
        if not prices:
            return "n/a"
        duration = prices[0].get("duration_minutes")
        return f"{duration} min" if isinstance(duration, int) else "unknown"

    def _cache_is_fresh(self) -> bool:
        """Return whether the cached today/tomorrow pair can still be reused."""
        if self._loaded_at is None:
            return False
        return (asyncio.get_running_loop().time() - self._loaded_at) < _CACHE_SECONDS

    async def _async_load(self) -> None:
        """Fetch and parse today's and tomorrow's prices for this home."""
        info = await self._async_fetch_price_info()
        days = self._parse_price_info(info)
        self._days = days
        self._loaded_at = asyncio.get_running_loop().time()
        _LOGGER.debug(
            "Tibber: loaded %s",
            ", ".join(
                f"{day.isoformat()}={len(blocks)}"
                for day, blocks in sorted(days.items())
            )
            or "no days",
        )

    async def _async_fetch_price_info(self) -> dict[str, Any]:
        """Fetch ``priceInfo`` for the configured home.

        Tries the quarter-hourly query first unless a previous attempt already
        proved that the API does not support it, and falls back to the hourly
        query when the resolution argument is rejected *or* when the
        quarter-hourly response is unexpectedly empty.
        """
        modes = [False] if not self._use_quarter else [True, False]
        last_error: Exception | None = None

        for use_quarter in modes:
            query = _QUARTER_QUERY if use_quarter else _HOURLY_QUERY
            try:
                data = await _async_post(self.hass, self._access_token, query)
            except _ResolutionNotSupported as err:
                last_error = err
                _LOGGER.info(
                    "Tibber API rejected the quarter-hourly query; "
                    "falling back to hourly prices"
                )
                self._use_quarter = False
                continue

            homes = _homes_of(data)
            if not homes:
                raise ProviderConnectionError(
                    "The Tibber account has no homes; a power subscription "
                    "is required to read prices"
                )

            home = next(
                (
                    candidate
                    for candidate in homes
                    if candidate.get("id") == self._home_id
                ),
                None,
            )
            if home is None:
                raise ProviderConnectionError(
                    f"Tibber home {self._home_id!r} is not visible to this token"
                )

            info = _price_info_of(home)
            if info.get("today"):
                self._use_quarter = use_quarter
                time_zone = home.get("timeZone") or self._time_zone_name
                if time_zone != self._time_zone_name:
                    # The home's timezone is authoritative; keep the resolved
                    # zone in sync in case the user moved.
                    self._time_zone_name = time_zone
                    self._time_zone = resolve_timezone(time_zone)
                return info

            last_error = ProviderConnectionError("Tibber returned no prices for today")
            if use_quarter:
                _LOGGER.info(
                    "Tibber returned no quarter-hourly prices; retrying hourly"
                )
                self._use_quarter = False

        raise ProviderConnectionError(
            "Tibber returned no usable price information"
        ) from last_error

    # --- Parsing -----------------------------------------------------------

    def _parse_price_info(
        self, info: dict[str, Any]
    ) -> dict[date_type, list[dict[str, Any]]]:
        """Normalize both day lists and group the blocks by market date."""
        days: dict[date_type, list[dict[str, Any]]] = {}
        for key in ("today", "tomorrow"):
            entries = info.get(key) or []
            for block in self._parse_entries(entries):
                day = market_date_of(block["from"], self._time_zone)
                days.setdefault(day, []).append(block)
        for blocks in days.values():
            blocks.sort(key=lambda block: dt_util.as_utc(block["from"]))
        return days

    def _parse_entries(self, entries: Any) -> list[dict[str, Any]]:
        """Normalize one Tibber day list into price blocks.

        Tibber gives only a start per price, not an end, so the end of each
        block is the next block's start and the last block is extended by the
        list's own resolution. Sorting and duration arithmetic happen on
        absolute UTC instants: subtracting two datetimes that share one
        ``tzinfo`` object yields a naive wall-clock delta, which is wrong on
        both daylight-saving transition days.
        """
        if not isinstance(entries, list):
            return []

        points: list[tuple[datetime, float, float, float]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            start = dt_util.parse_datetime(entry.get("startsAt"))
            if start is None:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=self._time_zone)
            start = start.astimezone(self._time_zone)

            total = _as_float(entry.get("total"))
            energy = _as_float(entry.get("energy"))
            tax = _as_float(entry.get("tax"))

            if total is None and energy is not None and tax is not None:
                total = energy + tax
            # Both the all-in price and the spot price are required: the first
            # drives every price entity, the second is the feed-in base. A
            # record missing either cannot be reconstructed honestly.
            if total is None or energy is None:
                _LOGGER.debug("Skipping Tibber price without total/energy: %r", entry)
                continue
            if tax is None:
                tax = total - energy

            points.append((start, total, energy, tax))

        if not points:
            return []

        # De-duplicate on the absolute start, newest value winning.
        by_start = {dt_util.as_utc(point[0]): point for point in points}
        ordered = [by_start[key] for key in sorted(by_start)]

        step_minutes = self._detect_step_minutes([point[0] for point in ordered])

        blocks: list[dict[str, Any]] = []
        for index, (start, total, energy, tax) in enumerate(ordered):
            if index + 1 < len(ordered):
                till = ordered[index + 1][0]
            else:
                till = (
                    dt_util.as_utc(start) + timedelta(minutes=step_minutes)
                ).astimezone(self._time_zone)
            duration = int(
                (dt_util.as_utc(till) - dt_util.as_utc(start)).total_seconds() // 60
            )
            if duration <= 0:
                continue
            blocks.append(
                {
                    "from": start,
                    "till": till,
                    "duration_minutes": duration,
                    "market_price": round(energy, 6),
                    "market_price_tax": round(tax, 6),
                    # Tibber prices its tax and markup into ``tax``; there is no
                    # separate component to report. Keeping these at zero means
                    # the shared sum still equals ``total``.
                    "sourcing_markup_price": 0.0,
                    "energy_tax_price": 0.0,
                    "total_price_eur_kwh": round(total, 6),
                    "per_unit": "kWh",
                    "provider": self.provider_id,
                }
            )
        return blocks

    @staticmethod
    def _detect_step_minutes(starts: list[datetime]) -> int:
        """Return the list's native step in minutes (15 or 60).

        The most common gap is used rather than the first one so a single
        oddity (a duplicated or missing point) cannot change the resolution
        assumed for the final block. Values are snapped to the two resolutions
        the integration understands, with 15 minutes as the safe default.
        """
        utc = [dt_util.as_utc(start) for start in starts]
        gaps = [
            int((later - earlier).total_seconds() // 60)
            for earlier, later in pairwise(utc)
            if later > earlier
        ]
        if not gaps:
            return 15
        gap = Counter(gaps).most_common(1)[0][0]
        return 60 if gap >= 60 else 15
