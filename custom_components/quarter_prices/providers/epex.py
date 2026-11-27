"""Generic provider backed by the public epexprijzen.nl / epexprijzen.be API.

This is the broadest price source in the integration. Instead of integrating
with one supplier, it reads the all-in dynamic tariff that
`epexprijzen.nl <https://epexprijzen.nl/api-documentatie>`_ publishes for a
curated list of Dutch providers (and, via ``epexprijzen.be``, Belgian providers
with a region). The API needs no key and supports both resolutions:

* ``/api/v1/prices/{provider_id}/quarterly`` -- 15-minute prices (96/day)
* ``/api/v1/prices/{provider_id}/hourly``    -- hourly prices (24/day)
* ``/api/v1/providers``                      -- the provider list
* ``.../prices/{id}/{resolution}?region=...`` -- Belgian regions

Response shape (verified live):

.. code-block:: json

    {
      "today":   [{"t": "2026-11-26T23:00:00Z", "price": 0.413684}, ...],
      "tomorrow":[...],
      "energy_tax": 0.11085,
      "provider_charge": 0.018,
      "spot_coefficient": 1
    }

``price`` is the all-in consumer price in EUR/kWh including VAT.
``energy_tax`` and ``provider_charge`` are stated *including VAT* by the site's
documentation; therefore the bare energy price (the feed-in base, equivalent to
Frank's ``market_price`` and Tibber's ``energy``) is::

    (price - energy_tax - provider_charge) / (1 + 21% VAT)

The provider's own spot multiplier (``spot_coefficient``) is already part of
that bare energy price, so the four normalized components still sum exactly to
``price``:

    market_price + market_price_tax + provider_charge + energy_tax == price

What this source cannot provide
-------------------------------

* **Feed-in / return-to-grid tariffs.** The API only returns purchase prices.
  The derived bare energy price is used as the feed-in base, and the existing
  feed-in adjustment option covers a supplier's fee or bonus. The exact
  per-provider feed-in formula is not exposed by the API.
* **Contract-specific discounts or a different markup.** The API models the
  provider's published standard dynamic product (and a few named variants such
  as ``frank-energie-slim``), not an individual customer's contract.
* **Providers outside the curated list, and markets outside NL/BE.** The list is
  fetched live, so it tracks what epexprijzen.nl currently supports.

The integration deliberately keeps the direct Frank Energie and Tibber
providers available: those use the suppliers' own APIs and can expose exact
provider fields and markets that this generic source cannot.
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
from urllib.parse import quote

import aiohttp
from aiohttp import ClientError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_CURRENCY,
    CONF_EPEX_PROVIDER_ID,
    CONF_EPEX_PROVIDER_NAME,
    CONF_MARKET,
    CONF_REGION,
    CONF_RESOLUTION,
    CONF_TIME_ZONE,
    DEFAULT_CURRENCY,
    DEFAULT_RESOLUTION,
    EPEX_BASE_URLS,
    EPEX_MARKET_TIMEZONES,
    EPEX_VAT_RATE,
    MARKET_NL,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    RESOLUTION_MINUTES,
    SUPPORTED_EPEX_MARKETS,
    SUPPORTED_RESOLUTIONS,
)
from ..market_time import market_date_of, resolve_timezone
from .base import (
    PriceProvider,
    ProviderConnectionError,
    ProviderError,
)

_LOGGER = logging.getLogger(__name__)

# Delay (seconds) between retries for transport-level and 5xx/429 failures.
RETRY_BACKOFF = 2

# Reuse a fetched today/tomorrow pair for this long. The coordinator calls
# today and tomorrow back to back; one minute collapses that into one request.
_CACHE_SECONDS = 55

# The site's own API is cached by its CDN for hours, so a short local cache is
# enough to avoid duplicate requests without hiding a newly published tomorrow.
_PROVIDERS_PATH = "/api/v1/providers"
_PRICES_PATH = "/api/v1/prices/{provider}/{resolution}"


def _as_float(value: Any) -> float | None:
    """Return ``value`` as a float, rejecting booleans and non-numerics."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _api_error_message(text: str) -> str | None:
    """Extract the site's concise ``{"error": ...}`` message, if present."""
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str) and error:
            return error
    return None


async def _async_get_json(
    hass, url: str, params: dict[str, str] | None = None
) -> dict[str, Any]:
    """GET one JSON document from epexprijzen.nl/be with retries.

    A 404 is not retried: it means the provider id or region is not modelled by
    the site and the user needs a precise error, not a retry storm. 429 and 5xx
    are retried; transport errors are retried as well.
    """
    session = async_get_clientsession(hass)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(url, params=params, timeout=timeout) as response:
                status = response.status
                text = await response.text()
        except (ClientError, TimeoutError) as err:
            last_error = err
            _LOGGER.warning(
                "epexprijzen request failed (attempt %d/%d): %s",
                attempt,
                MAX_RETRIES,
                err,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
            continue

        if status == 404:
            message = _api_error_message(text) or "not found"
            raise ProviderConnectionError(f"epexprijzen: {message}")

        if status == 429 or status >= 500:
            last_error = ProviderConnectionError(f"epexprijzen returned HTTP {status}")
            _LOGGER.warning(
                "epexprijzen returned HTTP %d (attempt %d/%d)",
                status,
                attempt,
                MAX_RETRIES,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
            continue

        if status != 200:
            raise ProviderConnectionError(f"epexprijzen returned HTTP {status}")

        try:
            payload = json.loads(text)
        except ValueError as err:
            raise ProviderConnectionError("epexprijzen returned invalid JSON") from err
        if not isinstance(payload, dict):
            raise ProviderConnectionError("epexprijzen returned an unexpected payload")
        return payload

    raise ProviderConnectionError(
        f"Failed to reach epexprijzen after {MAX_RETRIES} attempts"
    ) from last_error


async def async_fetch_epex_providers(hass, market: str) -> list[dict[str, Any]]:
    """Fetch the provider list for a market.

    Normalizes the two response shapes the site uses:

    * NL: ``{"id", "name", "product"?}``
    * BE: ``{"id", "name", "product"?, "productByRegion"?, "regions"}``
    """
    base_url = EPEX_BASE_URLS.get(market)
    if base_url is None:
        raise ProviderError(f"Unsupported EPEX market: {market!r}")

    payload = await _async_get_json(hass, f"{base_url}{_PROVIDERS_PATH}")
    raw = payload.get("providers")
    if not isinstance(raw, list):
        raise ProviderConnectionError("epexprijzen returned no provider list")

    providers: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        provider_id = item.get("id")
        name = item.get("name")
        if not isinstance(provider_id, str) or not provider_id:
            continue
        if not isinstance(name, str) or not name:
            continue
        product_by_region = item.get("productByRegion")
        providers.append(
            {
                "id": provider_id,
                "name": name,
                "product": item.get("product")
                if isinstance(item.get("product"), str)
                else None,
                "product_by_region": (
                    product_by_region if isinstance(product_by_region, dict) else {}
                ),
                "regions": [
                    region
                    for region in (item.get("regions") or [])
                    if isinstance(region, str)
                ],
            }
        )
    return providers


def format_provider_label(provider: dict[str, Any], region: str | None = None) -> str:
    """Return a human-readable label including a named product, if any."""
    name = provider["name"]
    product = provider.get("product")
    if region:
        product = provider.get("product_by_region", {}).get(region) or product
    return f"{name} — {product}" if product else name


class EpexProvider(PriceProvider):
    """Every provider modelled by epexprijzen.nl/be, behind one interface."""

    provider_id = "epex"
    provider_name = "EPEX (epexprijzen.nl)"
    device_name = "EPEX"
    manufacturer = "epexprijzen.nl"
    model = "EPEX day-ahead prices"
    requires_auth = False

    def __init__(self, hass, entry) -> None:
        """Initialize the provider from a config entry."""
        super().__init__(hass, entry)
        market = entry.data.get(CONF_MARKET, MARKET_NL)
        self._market = market if market in SUPPORTED_EPEX_MARKETS else MARKET_NL
        self._region = entry.data.get(CONF_REGION) or None
        self._epex_provider_id = entry.data[CONF_EPEX_PROVIDER_ID]
        self._provider_label = (
            entry.data.get(CONF_EPEX_PROVIDER_NAME) or self._epex_provider_id
        )
        # The device is named after the chosen provider, so entity ids are
        # ``sensor.eneco_current_price`` / ``sensor.zonneplan_current_price``.
        self.device_name = self._provider_label
        self.provider_name = (
            f"{self._provider_label} via epexprijzen"
            f"{'.be' if self._market == 'BE' else '.nl'}"
        )
        self.model = (
            f"EPEX day-ahead prices ({self._market}"
            f"{f', {self._region}' if self._region else ''})"
        )
        self._time_zone_name = (
            entry.data.get(CONF_TIME_ZONE) or EPEX_MARKET_TIMEZONES[self._market]
        )
        self._time_zone = resolve_timezone(self._time_zone_name)
        self._currency = entry.data.get(CONF_CURRENCY) or DEFAULT_CURRENCY

        resolution = entry.options.get(CONF_RESOLUTION) or entry.data.get(
            CONF_RESOLUTION, DEFAULT_RESOLUTION
        )
        self._resolution = (
            resolution if resolution in SUPPORTED_RESOLUTIONS else DEFAULT_RESOLUTION
        )

        self._days: dict[date_type, list[dict[str, Any]]] = {}
        self._loaded_at: float | None = None

    @property
    def time_zone(self):
        """Return the market's timezone."""
        return self._time_zone

    @property
    def currency(self) -> str:
        """Return the currency of the prices (always EUR for NL/BE)."""
        return self._currency

    @property
    def market(self) -> str:
        """Return the configured market code (NL or BE)."""
        return self._market

    @property
    def region(self) -> str | None:
        """Return the configured Belgian region, if any."""
        return self._region

    @property
    def resolution(self) -> str:
        """Return the configured price resolution."""
        return self._resolution

    @property
    def epex_provider_id(self) -> str:
        """Return the epexprijzen provider id."""
        return self._epex_provider_id

    def _base_url(self) -> str:
        """Return the site that serves the configured market."""
        return EPEX_BASE_URLS[self._market]

    def _price_url(self) -> str:
        """Return the prices endpoint for this provider and resolution."""
        return self._base_url() + _PRICES_PATH.format(
            provider=quote(self._epex_provider_id, safe=""),
            resolution=self._resolution,
        )

    async def async_get_prices_for_date(self, target_date: date_type) -> dict[str, Any]:
        """Return the normalized price blocks for one market day."""
        if not self._cache_is_fresh():
            await self._async_load()
        prices = list(self._days.get(target_date, []))
        _LOGGER.debug(
            "epexprijzen %s/%s: %d %s block(s) for %s",
            self._epex_provider_id,
            self._market,
            len(prices),
            self._resolution,
            target_date.isoformat(),
        )
        return {"prices": prices}

    def _cache_is_fresh(self) -> bool:
        """Return whether the cached today/tomorrow pair can be reused."""
        if self._loaded_at is None:
            return False
        return (asyncio.get_running_loop().time() - self._loaded_at) < _CACHE_SECONDS

    async def _async_load(self) -> None:
        """Fetch today/tomorrow and parse both into normalized blocks."""
        params = {"region": self._region} if self._region else None
        payload = await _async_get_json(self.hass, self._price_url(), params=params)

        energy_tax = _as_float(payload.get("energy_tax"))
        provider_charge = _as_float(payload.get("provider_charge"))
        spot_coefficient = _as_float(payload.get("spot_coefficient"))
        if energy_tax is None or provider_charge is None:
            _LOGGER.warning(
                "epexprijzen response for %s is missing energy_tax/provider_charge; "
                "the feed-in base cannot be reconstructed exactly",
                self._epex_provider_id,
            )
            energy_tax = energy_tax or 0.0
            provider_charge = provider_charge or 0.0
        if spot_coefficient is None:
            spot_coefficient = 1.0

        days: dict[date_type, list[dict[str, Any]]] = {}
        for key in ("today", "tomorrow"):
            entries = payload.get(key) or []
            blocks = self._parse_entries(
                entries,
                energy_tax=energy_tax,
                provider_charge=provider_charge,
                spot_coefficient=spot_coefficient,
            )
            for block in blocks:
                day = market_date_of(block["from"], self._time_zone)
                days.setdefault(day, []).append(block)

        for blocks in days.values():
            blocks.sort(key=lambda block: dt_util.as_utc(block["from"]))

        self._days = days
        self._loaded_at = asyncio.get_running_loop().time()
        _LOGGER.debug(
            "epexprijzen %s/%s loaded: %s",
            self._epex_provider_id,
            self._market,
            ", ".join(
                f"{day.isoformat()}={len(blocks)}"
                for day, blocks in sorted(days.items())
            )
            or "no days",
        )

    def _parse_entries(
        self,
        entries: Any,
        *,
        energy_tax: float,
        provider_charge: float,
        spot_coefficient: float,
    ) -> list[dict[str, Any]]:
        """Normalize one day list into the shared block shape.

        All timestamps are absolute (UTC ``Z``), and durations are measured in
        UTC. Subtracting two datetimes that share one ``tzinfo`` object is a
        naive wall-clock subtraction, which is wrong on both DST transition
        days.
        """
        if not isinstance(entries, list):
            return []

        points: list[tuple[datetime, float]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            start = dt_util.parse_datetime(entry.get("t"))
            if start is None:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=self._time_zone)
            price = _as_float(entry.get("price"))
            if price is None:
                continue
            points.append((start.astimezone(self._time_zone), price))

        if not points:
            return []

        by_start = {dt_util.as_utc(point[0]): point for point in points}
        ordered = [by_start[key] for key in sorted(by_start)]

        default_step = RESOLUTION_MINUTES[self._resolution]
        step_minutes = self._detect_step_minutes(
            [point[0] for point in ordered], default_step
        )

        blocks: list[dict[str, Any]] = []
        for index, (start, price) in enumerate(ordered):
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

            # price = energy_component + energy_tax + provider_charge, where the
            # two fixed components are stated including VAT and the energy
            # component carries VAT on top. Splitting it this way keeps the four
            # normalized components summing exactly to the published price.
            energy_component = price - energy_tax - provider_charge
            market_price = round(energy_component / (1.0 + EPEX_VAT_RATE), 6)
            market_price_tax = round(
                price - energy_tax - provider_charge - market_price, 6
            )

            blocks.append(
                {
                    "from": start,
                    "till": till,
                    "duration_minutes": duration,
                    "market_price": market_price,
                    "market_price_tax": market_price_tax,
                    "sourcing_markup_price": round(provider_charge, 6),
                    "energy_tax_price": round(energy_tax, 6),
                    "total_price_eur_kwh": round(price, 6),
                    # The EPEX source publishes an all-in price. Unlike the
                    # direct Frank provider (which deliberately shows its
                    # marktprijs+ component), the current-price sensor for this
                    # source shows what the consumer actually pays.
                    "dynamic_price_eur_kwh": round(price, 6),
                    "per_unit": "kWh",
                    "provider": self.provider_id,
                    "epex_provider_id": self._epex_provider_id,
                    "epex_resolution": self._resolution,
                    "spot_coefficient": spot_coefficient,
                }
            )
        return blocks

    @staticmethod
    def _detect_step_minutes(starts: list[datetime], default_minutes: int) -> int:
        """Return the native step from the actual gaps, with a configured default."""
        utc = [dt_util.as_utc(start) for start in starts]
        gaps = [
            int((later - earlier).total_seconds() // 60)
            for earlier, later in pairwise(utc)
            if later > earlier
        ]
        if not gaps:
            return default_minutes
        gap = Counter(gaps).most_common(1)[0][0]
        return 60 if gap >= 60 else 15
