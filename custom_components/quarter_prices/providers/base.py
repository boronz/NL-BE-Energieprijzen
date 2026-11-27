"""Common interface implemented by every price provider.

The rest of the integration only ever talks to this interface. A provider's job
is narrow on purpose: fetch one market day of prices and normalize each block
into the stable shape below. Everything else — caching, completeness checks,
zones, optimal periods, the quarter-hour refresh cadence and every entity — is
provider independent and lives outside this package.

Normalized block shape (all providers)::

    {
        "from": aware datetime,          # start, in the market timezone
        "till": aware datetime,          # end, in the market timezone
        "duration_minutes": int,         # 15 for a quarter, 60 for an hour
        "market_price": float,           # raw spot price, excl. taxes/VAT
        "market_price_tax": float,       # taxes on the energy price
        "sourcing_markup_price": float,  # supplier's purchase markup
        "energy_tax_price": float,       # energy tax, when billed separately
        "total_price_eur_kwh": float,    # all-in consumer purchase price
        "per_unit": str,
    }

``market_price`` is always the spot price *excluding* VAT and energy tax,
because that is the base the feed-in estimate uses. ``total_price_eur_kwh`` is
always the all-in price the consumer pays for that block.

The four components are signed and are summed without clamping, so a negative
spot price stays negative. ``market_price + market_price_tax +
sourcing_markup_price + energy_tax_price`` equals ``total_price_eur_kwh`` for
every provider; the "dynamic component" shown by the *Current price* sensor is
the same sum minus an explicitly separate energy tax (see :mod:`.price`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date as date_type
from datetime import tzinfo
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


class ProviderError(Exception):
    """Base error for a price provider."""


class ProviderAuthError(ProviderError):
    """Raised when the provider rejects the configured credentials."""


class ProviderConnectionError(ProviderError):
    """Raised when the provider cannot be reached or returns unusable data."""


class PriceProvider(ABC):
    """One configured price source for one config entry."""

    #: Stable machine id, also stored in the config entry (``provider``).
    provider_id: ClassVar[str]
    #: Human-readable provider name, shown in the config flow.
    provider_name: ClassVar[str]
    #: Short device name; entity ids are prefixed with it (``sensor.tibber_*``).
    device_name: ClassVar[str]
    #: Device registry metadata.
    manufacturer: ClassVar[str]
    model: ClassVar[str]
    #: Whether the provider needs credentials during setup.
    requires_auth: ClassVar[bool] = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the provider for a config entry."""
        self.hass = hass
        self.entry = entry

    @property
    @abstractmethod
    def time_zone(self) -> tzinfo:
        """Return the timezone the provider's price day is defined in."""

    @property
    def currency(self) -> str:
        """Return the ISO currency code of the prices (``EUR`` for all)."""
        return "EUR"

    @property
    def country(self) -> str | None:
        """Return the market country code, when the provider has one."""
        return None

    @abstractmethod
    async def async_get_prices_for_date(self, target_date: date_type) -> dict[str, Any]:
        """Fetch and normalize the prices for one market calendar day.

        Must return ``{"prices": [...]}``. An unpublished day is an empty list,
        not an error: the coordinator treats "no prices yet" as an expected
        state for tomorrow. Transport, authentication and schema failures must
        raise :class:`ProviderError` (or a subclass) so the coordinator can keep
        already cached data alive.
        """

    def __repr__(self) -> str:
        """Return a compact representation useful in logs."""
        return f"<{type(self).__name__} provider={self.provider_id}>"
