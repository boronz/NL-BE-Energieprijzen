"""Config flow for the Quarter Prices integration.

Setup is two decisions at most: **which provider** and, depending on it, that
provider's own details. Frank Energie needs only a market country because its
public market-prices endpoint is unauthenticated. Tibber needs a personal access
token and, when the account has more than one home, the home whose prices should
be published.

Each config entry is bound to exactly one provider and one account/home, so
several entries can coexist (Frank for one address, Tibber for another, or two
Tibber homes). The unique id is namespaced by provider to make that safe.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
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
    DEFAULT_APPLY_FEED_IN_VAT,
    DEFAULT_COUNTRY,
    DEFAULT_CURRENCY,
    DEFAULT_FEED_IN_ADJUSTMENT,
    DEFAULT_OPTIMAL_DURATION_MINUTES,
    DEFAULT_OPTIMAL_ENABLED,
    DEFAULT_RESOLUTION,
    DEFAULT_TIBBER_TIME_ZONE,
    DEFAULT_ZONE_MARGIN_PERCENT,
    DOMAIN,
    EPEX_MARKET_LABELS,
    EPEX_MARKET_TIMEZONES,
    EPEX_REGION_LABELS,
    FEED_IN_ADJUSTMENT_STEP,
    MARKET_BE,
    MARKET_NL,
    MAX_FEED_IN_ADJUSTMENT,
    MAX_OPTIMAL_DURATION_MINUTES,
    MAX_ZONE_MARGIN_PERCENT,
    MIN_FEED_IN_ADJUSTMENT,
    MIN_OPTIMAL_DURATION_MINUTES,
    MIN_ZONE_MARGIN_PERCENT,
    MINUTES_UNIT,
    OPTIMAL_DURATION_STEP_MINUTES,
    PERCENT_UNIT,
    PRICE_UNIT_EUR_KWH,
    PROVIDER_EPEX,
    PROVIDER_FRANK,
    PROVIDER_LABELS,
    PROVIDER_TIBBER,
    RESOLUTION_MINUTES,
    SUPPORTED_COUNTRIES,
    SUPPORTED_EPEX_MARKETS,
    SUPPORTED_PROVIDERS,
    SUPPORTED_RESOLUTIONS,
    ZONE_MARGIN_PERCENT_STEP,
)
from .optimal_period import validate_duration_minutes
from .providers import (
    ProviderAuthError,
    ProviderConnectionError,
    ProviderError,
    async_fetch_epex_providers,
    async_fetch_homes,
    format_provider_label,
)

_LOGGER = logging.getLogger(__name__)

# Human-readable labels for the supported Frank country codes. The stored option
# value stays the uppercase ISO code (e.g. "NL"), which is sent in the
# x-country header; only the displayed label differs.
_COUNTRY_LABELS: dict[str, str] = {
    "NL": "Netherlands (NL)",
    "BE": "Belgium (BE)",
}

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PROVIDER, default=PROVIDER_FRANK): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(
                        value=provider,
                        label=PROVIDER_LABELS.get(provider, provider),
                    )
                    for provider in SUPPORTED_PROVIDERS
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
    }
)

STEP_FRANK_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_COUNTRY, default=DEFAULT_COUNTRY): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(
                        value=country, label=_COUNTRY_LABELS.get(country, country)
                    )
                    for country in SUPPORTED_COUNTRIES
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
    }
)

STEP_TIBBER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCESS_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


STEP_EPEX_MARKET_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MARKET, default=MARKET_NL): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(
                        value=market,
                        label=f"{EPEX_MARKET_LABELS.get(market, market)} ({market})",
                    )
                    for market in SUPPORTED_EPEX_MARKETS
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
    }
)

STEP_EPEX_REGION_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REGION, default="vlaanderen"): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(
                        value=region,
                        label=f"{EPEX_REGION_LABELS.get(region, region)} ({region})",
                    )
                    for region in EPEX_REGION_LABELS
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
    }
)


def _resolution_selector() -> SelectSelector:
    """Return the quarterly/hourly selector shared by setup and options."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(
                    value=resolution,
                    label=f"{RESOLUTION_MINUTES[resolution]} min ({resolution})",
                )
                for resolution in SUPPORTED_RESOLUTIONS
            ],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


class QuarterPricesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Quarter Prices."""

    VERSION = 1

    # Data carried across Tibber's two steps. Kept on the flow instance rather
    # than in the config entry so a half-finished flow never persists a token.
    _tibber_token: str | None = None
    _tibber_homes: list[dict[str, Any]] | None = None

    # Data carried across the EPEX market/region/provider steps.
    _epex_market: str | None = None
    _epex_region: str | None = None
    _epex_providers: list[dict[str, Any]] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the price provider."""
        if user_input is not None:
            if user_input[CONF_PROVIDER] == PROVIDER_TIBBER:
                return await self.async_step_tibber()
            if user_input[CONF_PROVIDER] == PROVIDER_EPEX:
                return await self.async_step_epex_market()
            return await self.async_step_frank()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
        )

    async def async_step_frank(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up Frank Energie, which needs only a market country."""
        if user_input is not None:
            country = user_input[CONF_COUNTRY]
            await self.async_set_unique_id(f"{PROVIDER_FRANK}_{country}")
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"Frank Quarter Prices ({country})",
                data={
                    CONF_PROVIDER: PROVIDER_FRANK,
                    CONF_COUNTRY: country,
                },
            )

        return self.async_show_form(
            step_id="frank",
            data_schema=STEP_FRANK_DATA_SCHEMA,
        )

    async def async_step_tibber(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate a Tibber token and discover the account's homes."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token = str(user_input.get(CONF_ACCESS_TOKEN, "")).strip()
            if not token:
                errors[CONF_ACCESS_TOKEN] = "invalid_token"
            else:
                try:
                    homes = await async_fetch_homes(self.hass, token)
                except ProviderAuthError:
                    errors["base"] = "invalid_auth"
                except ProviderConnectionError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error validating the Tibber token")
                    errors["base"] = "unknown"
                else:
                    usable = [
                        home
                        for home in homes
                        if home.get("price_count")
                        and (home.get("currency") or DEFAULT_CURRENCY)
                        == DEFAULT_CURRENCY
                    ]
                    if not homes:
                        errors["base"] = "no_homes"
                    elif not usable:
                        if any(home.get("price_count") for home in homes):
                            errors["base"] = "unsupported_currency"
                        else:
                            errors["base"] = "no_price_info"
                    elif len(usable) == 1:
                        return await self._async_create_tibber_entry(token, usable[0])
                    else:
                        self._tibber_token = token
                        self._tibber_homes = usable
                        return await self.async_step_tibber_home()

        return self.async_show_form(
            step_id="tibber",
            data_schema=STEP_TIBBER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_tibber_home(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which Tibber home to publish prices for."""
        if not self._tibber_homes or not self._tibber_token:
            # The flow was interrupted between the two steps; start over rather
            # than render a form with no options.
            return await self.async_step_tibber()

        if user_input is not None:
            home_id = user_input[CONF_HOME_ID]
            home = next(
                (item for item in self._tibber_homes if item["id"] == home_id), None
            )
            if home is not None:
                return await self._async_create_tibber_entry(self._tibber_token, home)

        options = [
            SelectOptionDict(
                value=home["id"],
                label=self._home_label(home),
            )
            for home in self._tibber_homes
        ]
        return self.async_show_form(
            step_id="tibber_home",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOME_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
        )

    @staticmethod
    def _home_label(home: dict[str, Any]) -> str:
        """Return a stable, human-readable label for a Tibber home."""
        name = home.get("app_nickname") or "Home"
        time_zone = home.get("time_zone") or DEFAULT_TIBBER_TIME_ZONE
        return f"{name} ({time_zone})"

    async def _async_create_tibber_entry(
        self, token: str, home: dict[str, Any]
    ) -> ConfigFlowResult:
        """Create the config entry for one validated Tibber home."""
        home_id = str(home["id"])
        await self.async_set_unique_id(f"{PROVIDER_TIBBER}_{home_id}")
        self._abort_if_unique_id_configured()

        home_name = home.get("app_nickname") or "Home"
        return self.async_create_entry(
            title=f"Tibber Quarter Prices ({home_name})",
            data={
                CONF_PROVIDER: PROVIDER_TIBBER,
                CONF_ACCESS_TOKEN: token,
                CONF_HOME_ID: home_id,
                CONF_HOME_NAME: home_name,
                CONF_TIME_ZONE: home.get("time_zone") or DEFAULT_TIBBER_TIME_ZONE,
                CONF_CURRENCY: home.get("currency") or DEFAULT_CURRENCY,
            },
        )

    # --- EPEX (any supported provider) -------------------------------------

    async def async_step_epex_market(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the EPEX market (Netherlands or Belgium)."""
        if user_input is not None:
            self._epex_market = user_input[CONF_MARKET]
            self._epex_region = None
            self._epex_providers = None
            if self._epex_market == MARKET_BE:
                return await self.async_step_epex_region()
            return await self._async_prepare_epex_providers(
                "epex_market", STEP_EPEX_MARKET_DATA_SCHEMA
            )

        return self.async_show_form(
            step_id="epex_market",
            data_schema=STEP_EPEX_MARKET_DATA_SCHEMA,
        )

    async def async_step_epex_region(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the Belgian region whose levies and tariff apply."""
        if self._epex_market is None:
            return await self.async_step_epex_market()

        if user_input is not None:
            self._epex_region = user_input[CONF_REGION]
            return await self._async_prepare_epex_providers(
                "epex_region", STEP_EPEX_REGION_DATA_SCHEMA
            )

        return self.async_show_form(
            step_id="epex_region",
            data_schema=STEP_EPEX_REGION_DATA_SCHEMA,
        )

    async def _async_prepare_epex_providers(
        self, fallback_step: str, fallback_schema: vol.Schema
    ) -> ConfigFlowResult:
        """Load and filter the provider list, or re-show the previous step."""
        assert self._epex_market is not None
        try:
            providers = await async_fetch_epex_providers(self.hass, self._epex_market)
        except ProviderError as err:
            _LOGGER.warning("Could not load the epexprijzen provider list: %s", err)
            return self.async_show_form(
                step_id=fallback_step,
                data_schema=fallback_schema,
                errors={"base": "cannot_connect"},
            )
        except Exception:
            _LOGGER.exception("Unexpected error loading the epexprijzen providers")
            return self.async_show_form(
                step_id=fallback_step,
                data_schema=fallback_schema,
                errors={"base": "unknown"},
            )

        if self._epex_market == MARKET_BE:
            providers = [
                provider
                for provider in providers
                if self._epex_region in provider.get("regions", [])
            ]

        if not providers:
            return self.async_show_form(
                step_id=fallback_step,
                data_schema=fallback_schema,
                errors={"base": "no_providers"},
            )

        providers.sort(
            key=lambda provider: format_provider_label(
                provider, self._epex_region
            ).lower()
        )
        self._epex_providers = providers
        return await self.async_step_epex_provider()

    async def async_step_epex_provider(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose a provider and the quarter/hour resolution."""
        if not self._epex_providers or self._epex_market is None:
            return await self.async_step_epex_market()

        errors: dict[str, str] = {}
        if user_input is not None:
            provider = next(
                (
                    item
                    for item in self._epex_providers
                    if item["id"] == user_input.get(CONF_EPEX_PROVIDER_ID)
                ),
                None,
            )
            resolution = user_input.get(CONF_RESOLUTION, DEFAULT_RESOLUTION)
            if provider is None:
                errors[CONF_EPEX_PROVIDER_ID] = "invalid_provider"
            elif resolution not in SUPPORTED_RESOLUTIONS:
                errors[CONF_RESOLUTION] = "invalid_resolution"
            else:
                region = self._epex_region or ""
                await self.async_set_unique_id(
                    f"epex_{self._epex_market.lower()}_{region}_{provider['id']}"
                )
                self._abort_if_unique_id_configured()

                label = format_provider_label(provider, self._epex_region)
                data: dict[str, Any] = {
                    CONF_PROVIDER: PROVIDER_EPEX,
                    CONF_MARKET: self._epex_market,
                    CONF_EPEX_PROVIDER_ID: provider["id"],
                    CONF_EPEX_PROVIDER_NAME: provider["name"],
                    CONF_RESOLUTION: resolution,
                    CONF_TIME_ZONE: EPEX_MARKET_TIMEZONES[self._epex_market],
                    CONF_CURRENCY: DEFAULT_CURRENCY,
                }
                if self._epex_region:
                    data[CONF_REGION] = self._epex_region

                return self.async_create_entry(
                    title=f"{label} Quarter Prices",
                    data=data,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_EPEX_PROVIDER_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                value=provider["id"],
                                label=format_provider_label(
                                    provider, self._epex_region
                                ),
                            )
                            for provider in self._epex_providers
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_RESOLUTION, default=DEFAULT_RESOLUTION
                ): _resolution_selector(),
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)

        return self.async_show_form(
            step_id="epex_provider", data_schema=schema, errors=errors
        )

    # --- Reauthentication --------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauthentication after Tibber rejected the token."""
        entry = self._get_reauth_entry()
        if entry.data.get(CONF_PROVIDER) != PROVIDER_TIBBER:
            return self.async_abort(reason="reauth_not_supported")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate and store a replacement Tibber token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            token = str(user_input.get(CONF_ACCESS_TOKEN, "")).strip()
            if not token:
                errors[CONF_ACCESS_TOKEN] = "invalid_token"
            else:
                try:
                    homes = await async_fetch_homes(self.hass, token)
                except ProviderAuthError:
                    errors["base"] = "invalid_auth"
                except ProviderConnectionError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error validating the Tibber token")
                    errors["base"] = "unknown"
                else:
                    home = next(
                        (
                            item
                            for item in homes
                            if str(item.get("id")) == str(entry.data.get(CONF_HOME_ID))
                        ),
                        None,
                    )
                    if home is None:
                        errors["base"] = "wrong_account"
                    else:
                        new_data = dict(entry.data)
                        new_data[CONF_ACCESS_TOKEN] = token
                        new_data[CONF_TIME_ZONE] = (
                            home.get("time_zone")
                            or entry.data.get(CONF_TIME_ZONE)
                            or DEFAULT_TIBBER_TIME_ZONE
                        )
                        new_data[CONF_CURRENCY] = (
                            home.get("currency")
                            or entry.data.get(CONF_CURRENCY)
                            or DEFAULT_CURRENCY
                        )
                        self.hass.config_entries.async_update_entry(
                            entry, data=new_data
                        )
                        return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_TIBBER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={
                CONF_HOME_NAME: entry.data.get(CONF_HOME_NAME, "Home")
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> QuarterPricesOptionsFlow:
        """Return the options flow handler."""
        return QuarterPricesOptionsFlow()


class QuarterPricesOptionsFlow(OptionsFlow):
    """Handle the options flow for Quarter Prices.

    One flat step in three logical groups, top to bottom: the feed-in settings,
    the two price-zone margins, then the two optional optimal periods. None of
    them is required during the initial setup, and an existing entry without
    them falls back to its defaults (0.0 adjustment, VAT disabled, 15% for both
    zone margins, both optimal periods off), so an install upgrading from an
    earlier version keeps working untouched.

    **Why the form is flat rather than grouped into collapsible sections.**
    Home Assistant does support sections in a config/options flow, and this form
    is exactly the shape they were designed for. They were still not used, for
    one decisive reason: a section *nests the stored values under the section
    key*. ``feed_in_adjustment`` would become ``feed_in.feed_in_adjustment`` in
    the entry's options, which would silently reset the feed-in adjustment, the
    VAT toggle and both zone margins for every existing installation on the
    first save, and would need a flatten-on-save / inflate-on-load shim plus an
    options migration to avoid it. That is a real compatibility and fragility
    cost for a purely cosmetic gain. The fields are therefore ordered so the
    three groups still read as groups, and each carries a description that says
    which feature it belongs to.

    The options are provider independent: the feed-in base is the spot price
    from either provider, and the zones and optimal periods are derived from the
    normalized blocks, never from a provider-specific field.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the feed-in, price-zone and optimal-period options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # The number selector enforces the 15-720 range and the type, but
            # *not* the step: it accepts 17 or 22.5 quite happily. The frontend
            # box does enforce the step, so this only bites a submission that
            # bypasses it, but a stored 17 would leave the planner permanently
            # unavailable for a reason the user cannot see. Validating here
            # keeps the stored configuration always usable.
            #
            # The check deliberately lives in the step handler rather than in
            # the schema: wrapping the selector in ``vol.All`` makes the options
            # form fail to serialize for the frontend altogether, so the whole
            # form would stop rendering.
            for key in (
                CONF_OPTIMAL_CHEAP_DURATION_MINUTES,
                CONF_OPTIMAL_EXPENSIVE_DURATION_MINUTES,
            ):
                if (
                    key in user_input
                    and validate_duration_minutes(user_input[key]) is None
                ):
                    errors[key] = "invalid_duration"

            if not errors:
                return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        current_adjustment = options.get(
            CONF_FEED_IN_ADJUSTMENT, DEFAULT_FEED_IN_ADJUSTMENT
        )
        current_vat = options.get(CONF_APPLY_FEED_IN_VAT, DEFAULT_APPLY_FEED_IN_VAT)
        current_cheap_margin = options.get(
            CONF_CHEAP_ZONE_MARGIN_PERCENT, DEFAULT_ZONE_MARGIN_PERCENT
        )
        current_expensive_margin = options.get(
            CONF_EXPENSIVE_ZONE_MARGIN_PERCENT, DEFAULT_ZONE_MARGIN_PERCENT
        )
        options_fields: dict[Any, Any] = {
            # --- Feed-in price ---
            vol.Optional(
                CONF_FEED_IN_ADJUSTMENT, default=current_adjustment
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_FEED_IN_ADJUSTMENT,
                    max=MAX_FEED_IN_ADJUSTMENT,
                    step=FEED_IN_ADJUSTMENT_STEP,
                    unit_of_measurement=PRICE_UNIT_EUR_KWH,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_APPLY_FEED_IN_VAT, default=current_vat
            ): BooleanSelector(),
            # --- Price zones ---
            vol.Optional(
                CONF_CHEAP_ZONE_MARGIN_PERCENT, default=current_cheap_margin
            ): _margin_selector(),
            vol.Optional(
                CONF_EXPENSIVE_ZONE_MARGIN_PERCENT,
                default=current_expensive_margin,
            ): _margin_selector(),
            # --- Optimal periods ---
            #
            # Each toggle is followed directly by its own duration. The
            # duration stays visible while the feature is off rather than
            # being hidden behind a second flow step: Home Assistant cannot
            # show or hide a field within one step without re-submitting the
            # form, and splitting one small form into two steps to hide two
            # inactive number boxes would make the UI worse, not better. The
            # field descriptions say the duration only applies while the
            # matching toggle is on.
            vol.Optional(
                CONF_OPTIMAL_CHEAP_ENABLED,
                default=options.get(
                    CONF_OPTIMAL_CHEAP_ENABLED, DEFAULT_OPTIMAL_ENABLED
                ),
            ): BooleanSelector(),
            vol.Optional(
                CONF_OPTIMAL_CHEAP_DURATION_MINUTES,
                default=options.get(
                    CONF_OPTIMAL_CHEAP_DURATION_MINUTES,
                    DEFAULT_OPTIMAL_DURATION_MINUTES,
                ),
            ): _duration_selector(),
            vol.Optional(
                CONF_OPTIMAL_EXPENSIVE_ENABLED,
                default=options.get(
                    CONF_OPTIMAL_EXPENSIVE_ENABLED, DEFAULT_OPTIMAL_ENABLED
                ),
            ): BooleanSelector(),
            vol.Optional(
                CONF_OPTIMAL_EXPENSIVE_DURATION_MINUTES,
                default=options.get(
                    CONF_OPTIMAL_EXPENSIVE_DURATION_MINUTES,
                    DEFAULT_OPTIMAL_DURATION_MINUTES,
                ),
            ): _duration_selector(),
        }

        if self.config_entry.data.get(CONF_PROVIDER) == PROVIDER_EPEX:
            options_fields[
                vol.Optional(
                    CONF_RESOLUTION,
                    default=options.get(
                        CONF_RESOLUTION,
                        self.config_entry.data.get(CONF_RESOLUTION, DEFAULT_RESOLUTION),
                    ),
                )
            ] = _resolution_selector()

        options_schema = vol.Schema(options_fields)
        if user_input is not None:
            # Re-showing the form after a validation error: keep everything the
            # user just typed, so only the offending field has to be corrected.
            options_schema = self.add_suggested_values_to_schema(
                options_schema, user_input
            )

        return self.async_show_form(
            step_id="init", data_schema=options_schema, errors=errors
        )


def _margin_selector() -> NumberSelector:
    """Return the selector shared by both price-zone margin options."""
    return NumberSelector(
        NumberSelectorConfig(
            min=MIN_ZONE_MARGIN_PERCENT,
            max=MAX_ZONE_MARGIN_PERCENT,
            step=ZONE_MARGIN_PERCENT_STEP,
            unit_of_measurement=PERCENT_UNIT,
            mode=NumberSelectorMode.BOX,
        )
    )


def _duration_selector() -> NumberSelector:
    """Return the selector shared by both optimal-period duration options.

    The 15-minute step means every offered value is a whole number of native
    quarter-hour blocks, so a duration chosen here can always be composed out of
    whole blocks on a quarter-hour day.
    """
    return NumberSelector(
        NumberSelectorConfig(
            min=MIN_OPTIMAL_DURATION_MINUTES,
            max=MAX_OPTIMAL_DURATION_MINUTES,
            step=OPTIMAL_DURATION_STEP_MINUTES,
            unit_of_measurement=MINUTES_UNIT,
            mode=NumberSelectorMode.BOX,
        )
    )
