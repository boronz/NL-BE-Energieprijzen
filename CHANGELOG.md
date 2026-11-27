# Changelog

All notable changes to this project are documented here.

This project is a provider-agnostic fork of
[ha-frank-quarter-prices](https://github.com/Bennie-JC/ha-frank-quarter-prices).
The original project's history up to v0.2.0-beta.1 is its own; this file starts
at the fork.

## [0.2.1] - 2026-11-27

### Fixed

- **Author metadata corrected.** `codeowners` now credits this fork's actual
  repository owner (`@boronz`) instead of the upstream author.
- **Documentation and issue links corrected.** `documentation` and
  `issue_tracker` now point to
  [boronz/NL-BE-Energieprijzen](https://github.com/boronz/NL-BE-Energieprijzen)
  instead of the original Frank Quarter Prices project, so the help button and
  issue link no longer lead to someone else's repository.
- README refreshed: the provider overview now shows the EPEX source, and a
  duplicated note was removed.

### Notes

- HACS may display a commit hash instead of a semantic version for repositories
  without a GitHub release. That is cosmetic; `manifest.json` is at 0.2.1.

## [0.2.0] - 2026-11-27

### Added

- **Generic provider source: "Provider via EPEX".** Uses the public
  epexprijzen.nl / epexprijzen.be API to offer the published all-in dynamic
  tariff of every provider the site models (~34 NL, ~29 BE), including Frank
  Energie, Tibber, Eneco, Vattenfall, Essent, Greenchoice, Zonneplan, ANWB
  Energie and others. No account or API key is required.
- **Live provider list and Belgian regions.** The setup dialog fetches
  `/api/v1/providers`; for Belgium it asks for Flanders, Brussels or Wallonia and
  filters the list, passing `?region=` to the price endpoint.
- **Quarter or hour resolution.** EPEX entries can publish 15-minute (96/day) or
  60-minute (24/day) prices. The choice is made at setup and can be changed in
  the integration options; changing it reloads the entry and clears the day
  cache.
- **Provider-named devices and entities.** An EPEX entry for Eneco creates
  `sensor.eneco_*`; an entry for Zonneplan creates `sensor.zonneplan_*` (the EPEX
  source itself is not the device name).
- **All-in current price for EPEX entries.** The published all-in price is used
  for `current_price`; direct Frank/Tibber keep their existing behaviour.
- **Price reconstruction and feed-in base.** `energy_tax`, `provider_charge` and
  `spot_coefficient` are used to split the all-in price into the shared block
  components and to derive the bare spot price used by the feed-in sensor.
- **Tests:** EPEX parsing (quarter/hour, DST, negative prices, component sums),
  HTTP retries/404s, provider-list normalization, NL/BE config flows, duplicate
  detection, resolution options and a full entity setup.

### Changed

- `PriceProvider` consumers can now override the current-price value per block
  via `dynamic_price_eur_kwh`; the coordinator falls back to the shared dynamic
  calculation when the field is absent.
- Diagnostics show the EPEX market, region, provider id and resolution.
- Translations (EN/NL), README, examples and the changelog document the new
  source and its limits (no feed-in tariffs, no contract-specific markup, no
  markets outside NL/BE, third-party API without SLA).

### Notes

- The EPEX source is additive: direct Frank Energie and Tibber remain available
  for exact supplier fields, non-NL/BE markets and independence from the
  third-party service.

## [0.1.0] - 2026-11-27

### Added

- **Provider selection in the config flow.** The first setup step asks whether to
  use Frank Energie or Tibber. Each config entry is bound to one provider and one
  account/home; several entries can coexist and each gets its own device and
  entity prefix (`frank_*` or `tibber_*`).
- **Tibber provider.**
  - Authenticated GraphQL client (`api.tibber.com/v1-beta/gql`) using a personal
    access token.
  - Native quarter-hourly prices via
    `currentSubscription.priceInfo(resolution: QUARTER_HOURLY)`, with automatic
    one-time fallback to the plain hourly `priceInfo` query on older schemas or
    hourly-only markets.
  - Home discovery, multi-home selection, and reauthentication when a token is
    rejected.
  - Prices normalized to the existing block model: `energy` becomes the spot
    base, `tax` the tax component, `total` the all-in price. The shared
    dynamic-price sum therefore reproduces Tibber's all-in price exactly.
- **Provider abstraction** (`providers/base.py`, `providers/__init__.py`): a
  `PriceProvider` interface, normalized block contract, provider registry and
  factory.
- **Provider-neutral config flow, options flow, coordinator, entities,
  diagnostics and translations** (English and Dutch).
- **Diagnostics redact the Tibber token** and expose provider, home, market
  timezone and resolved options.
- **Tests:** the original coordinator, zone, optimal-period, entity-contract,
  Recorder-attribute and market-time suites are ported, plus new suites for the
  Tibber provider (parsing, DST, fallback, auth, caching, home discovery),
  config/reauth flows and a full Tibber entry/entity setup.
- Neutral `Quarter Prices` branding, icons and ApexCharts examples.

### Changed

- Integration domain is `quarter_prices` (was `frank_quarter_prices`), so this
  fork can be installed alongside the original integration without a domain
  collision.
- Market-day helpers accept a resolved timezone (or IANA name) instead of a
  Frank country code; country codes remain supported for compatibility.
- The Tibber adapter computes the feed-in base from `Price.energy`. A supplier's
  feed-in fee is expressed with the existing (usually negative) adjustment.

### Notes

- Tibber data is restricted to EUR-priced homes (Netherlands, Belgium, Germany)
  because the price entities are denominated in `EUR/kWh`.
- The 21% feed-in VAT option remains a manual, provider-independent choice.
- No migration is provided from the original integration: the domain differs, so
  install this as a separate integration. Frank users get the same entity names
  as before through the `Frank` device prefix.
