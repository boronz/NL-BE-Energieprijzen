# Changelog

All notable changes to this project are documented here.

This project is a provider-agnostic fork of
[ha-frank-quarter-prices](https://github.com/Bennie-JC/ha-frank-quarter-prices).
The original project's history up to v0.2.0-beta.1 is its own; this file starts
at the fork.

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
