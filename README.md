# Quarter Prices

Quarter-hourly electricity prices for Home Assistant — with a **choice of energy
provider**.

This is a provider-agnostic fork of
[Bennie-JC/ha-frank-quarter-prices](https://github.com/Bennie-JC/ha-frank-quarter-prices).
The data model, the entity contract, the price-zone and optimal-period logic and
the quarter-hour refresh behaviour are the original integration's. The price
source is now pluggable: the config flow asks which provider to use, and each
provider gets its own device and its own set of entities, so **Frank Energie and
Tibber can be used side by side**.

| | Frank Energie | Tibber |
|---|---|---|
| Credentials | none (public market prices) | personal access token |
| Markets | Netherlands, Belgium | any Tibber home whose prices are in EUR (NL, BE, DE) |
| Resolution | native 15-minute prices when published, hourly otherwise | native 15-minute prices when the market publishes them, hourly otherwise |
| Current price sensor | Frank's dynamic "marktprijs+" component | Tibber's all-in price (`total`) |
| All-in prices, zones, optimal periods | same normalized model | same normalized model |
| Feed-in estimate base | spot market price (`market_price`) | spot price (`energy`) |

---

## Features

- **15-minute prices** for today and tomorrow, with automatic hourly fallback
  when the market or the API only provides hourly data.
- **Provider selection at setup** — Frank Energie or Tibber, several entries
  allowed, each on its own device.
- **Existing entity contract preserved** — current price, feed-in price, whole-day
  price arrays, cheapest/most-expensive blocks and times, cheap/expensive price
  zones and optional optimal cheap/expensive periods.
- **Quarter-hour refresh cadence** aligned to the wall clock (`:00/:15/:30/:45`),
  not a drifting interval.
- **Day cache** — each market day is downloaded at most once; quarter ticks are
  local arithmetic. Tomorrow is requested only after the day-ahead auction has
  plausibly published, with backoff on sustained failures.
- **Market-time correctness** — "today" is the market's calendar day, not Home
  Assistant's configured timezone. The 23-hour spring-forward day and 25-hour
  fall-back day are handled from real timestamps, never from a fixed 96-block
  assumption.
- **Price zones and optimal periods** derived from the same fetched blocks, with
  no additional API requests.
- **Energy Dashboard ready** — monetary `EUR/kWh` sensors with no `state_class`
  for instantaneous prices.

---

## Installation

### HACS (custom repository)

1. In HACS, open **⋮ → Custom repositories**.
2. Add this repository's URL, category **Integration**.
3. Install **Quarter Prices** and restart Home Assistant.

### Manual

Copy `custom_components/quarter_prices` into your Home Assistant
`config/custom_components/` directory and restart.

Requires Home Assistant **2025.1** or newer. The test environment used by this
repository pins Home Assistant 2026.2 through
`pytest-homeassistant-custom-component`.

---

## Setup

**Settings → Devices & Services → Add Integration → Quarter Prices**, then choose
the provider.

### Frank Energie

Select the market country (Netherlands or Belgium). No account is required; the
public Frank Energie market-prices GraphQL endpoint is used.

The entry is titled `Frank Quarter Prices (NL)` or `(BE)`, the device is named
**Frank**, and entity ids are `sensor.frank_*` / `binary_sensor.frank_*`.

### Tibber

You need a **personal access token**:

1. Open <https://developer.tibber.com/> and sign in.
2. Go to **Personal access tokens** and create a token.
3. Paste it into the integration setup.

The integration reads the homes visible to that token. If the account has more
than one home, a second step asks which home to use. Only homes with price
information and **EUR** prices are offered. The entry is titled
`Tibber Quarter Prices (<home name>)`, the device is named **Tibber**, and entity
ids are `sensor.tibber_*` / `binary_sensor.tibber_*`.

The official Tibber integration is **not** required and does not conflict with
this one.

If Tibber later rejects the token, Home Assistant shows a **Reauthenticate**
action on the entry. Enter a new token for the same home; entities, entity ids
and customizations are preserved.

> To change provider or market country, remove the entry and add it again. A
> token refresh does not require that.

---

## Entities

Every entity id is prefixed with the provider's device name. The table uses
`<provider>` = `frank` or `tibber`.

| Entity | Type | Meaning |
|---|---|---|
| `sensor.<provider>_current_price` | EUR/kWh | Current dynamic import price. Frank: marktprijs+ (spot + VAT + sourcing markup). Tibber: all-in `total` (energy + tax). |
| `sensor.<provider>_current_return_price` | EUR/kWh | Estimated feed-in price: spot price + configured adjustment (+ optional 21% VAT). |
| `sensor.<provider>_prices_today` | blocks | Number of blocks today; `prices` attribute carries the full array. |
| `sensor.<provider>_prices_tomorrow` | blocks | Number of blocks tomorrow; unavailable until published. |
| `sensor.<provider>_cheapest_today` | EUR/kWh | Cheapest block today (all-in). |
| `sensor.<provider>_cheapest_time_today` | time | Start of the cheapest block today. |
| `sensor.<provider>_most_expensive_today` | EUR/kWh | Most expensive block today. |
| `sensor.<provider>_most_expensive_time_today` | time | Start of the most expensive block today. |
| `sensor.<provider>_cheapest_tomorrow` | EUR/kWh | Cheapest block tomorrow. |
| `sensor.<provider>_cheapest_time_tomorrow` | time | Start of the cheapest block tomorrow. |
| `sensor.<provider>_most_expensive_tomorrow` | EUR/kWh | Most expensive block tomorrow. |
| `sensor.<provider>_most_expensive_time_tomorrow` | time | Start of the most expensive block tomorrow. |
| `sensor.<provider>_cheap_zone_start_today` | timestamp | Start of today's cheap price zone. |
| `sensor.<provider>_cheap_zone_end_today` | timestamp | End of today's cheap price zone. |
| `sensor.<provider>_expensive_zone_start_today` | timestamp | Start of today's expensive price zone. |
| `sensor.<provider>_expensive_zone_end_today` | timestamp | End of today's expensive price zone. |
| `binary_sensor.<provider>_tomorrow_prices_available` | on/off | Whether tomorrow's prices are published. |
| `binary_sensor.<provider>_cheap_period` | on/off | Whether now is inside today's cheap zone. |
| `binary_sensor.<provider>_expensive_period` | on/off | Whether now is inside today's expensive zone. |

Optional, created only while enabled in the options:

| Entity | Type | Meaning |
|---|---|---|
| `sensor.<provider>_optimal_cheap_period_today` | timestamp | Start of today's cheapest contiguous window of the configured duration. |
| `sensor.<provider>_optimal_cheap_period_tomorrow` | timestamp | Same for tomorrow. |
| `sensor.<provider>_optimal_expensive_period_today` | timestamp | Start of today's most expensive window. |
| `sensor.<provider>_optimal_expensive_period_tomorrow` | timestamp | Same for tomorrow. |
| `binary_sensor.<provider>_optimal_cheap_period` | on/off | Whether now is inside today's optimal cheap window. |
| `binary_sensor.<provider>_optimal_expensive_period` | on/off | Whether now is inside today's optimal expensive window. |

The whole-day sensors publish a `prices` attribute: a list of blocks with
`from`, `till`, `duration_minutes`, `market_price`, `market_price_tax`,
`sourcing_markup_price`, `energy_tax_price` and `total_price_eur_kwh`. The
attribute is excluded from Recorder history because a full day is roughly 25 kB,
well above the Recorder's per-state limit; it remains a normal live attribute
for templates and dashboards.

---

## Options

All options are provider independent and live under
**Settings → Devices & Services → Quarter Prices → Configure**.

### Feed-in adjustment

The feed-in estimate is:

```text
feed-in = spot price + feed_in_adjustment
```

`spot price` is the raw market price excluding VAT and energy tax — Frank's
`market_price`, Tibber's `energy`. Use a **negative** value for a supplier's
feed-in fee or deduction, a **positive** value for extra compensation, and `0`
to use the spot price unchanged. The result is never clamped, so negative spot
prices stay negative.

Enable **Apply 21% VAT to feed-in price** when your compensation is calculated
including VAT. This is a manual choice; it is not read from either provider.
The 21% rate matches the Dutch and Belgian standard rate.

### Price zones

Both margins are a percentage of the day's own price *range* (cheapest block to
most expensive block), measured inward from the respective extreme:

```text
cheap zone      : price <= day_min + spread * cheap%
expensive zone  : price >= day_max - spread * expensive%
```

The zone is the **longest contiguous run** of qualifying blocks. Default 15%,
maximum 49% each; the maximum keeps the two zones disjoint by construction.
Splitting the percentage over the absolute all-in price does not work: a fixed
tax/markup floor dwarfs the daily spread on flat days and makes the zones
overlap.

### Optimal periods

A price *zone* answers "which stretch of today is inside my threshold?". An
optimal *period* answers "which exact X-minute window is the cheapest (or most
expensive)?". The two are independent: the period calculation never reads the
zone margins.

- Duration: 15–720 minutes in 15-minute steps.
- The window is always exactly that long and contiguous, built from whole source
  blocks. A duration that cannot be composed from the day's blocks (90 minutes
  on an hourly day, for example) yields an unavailable entity rather than a
  split or interpolated block.
- Both features are off by default. An entity created while the feature was on
  stays in the registry as unavailable after it is switched off, preserving
  entity ids and customizations.

---

## Refresh model

| What | When |
|---|---|
| Today's prices | fetched once, when no complete day is cached |
| Tomorrow's prices | not requested before 12:00 market time; retried on the next quarter tick until published; backoff after repeated failures |
| Active block, current price, zones, optimal periods | recalculated on every quarter tick from cached blocks |
| Full refresh | `:00`, `:15`, `:30`, `:45` plus 3 seconds, wall clock |

A day is accepted as *complete* only when its blocks cover the real market
midnight-to-midnight span without gaps or overlaps. That is 92 blocks on the
spring-forward day (23 hours), 96 normally and 100 on the fall-back day
(25 hours), or 23/24/25 hourly blocks. Incomplete days are used for the current
update but never frozen in.

---

## Energy Dashboard

Use `sensor.<provider>_current_price` as the electricity **price** entity and
`sensor.<provider>_current_return_price` as the **return to grid** price. Both
carry `device_class: monetary` and `unit_of_measurement: EUR/kWh`, with no
`state_class` (an instantaneous price is not a cumulative total).

The all-in price for each block is on
`attributes.full_block.total_price_eur_kwh`; the current-price sensor's state
uses the provider's dynamic component as described above.

---

## Dashboards

`examples/apexcharts_quarter_prices_today.yaml` and
`examples/apexcharts_quarter_prices_tomorrow.yaml` render every block of the day
individually with ApexCharts. They use the Tibber entity ids; replace `tibber`
with `frank` for a Frank entry. Both cards need
[apexcharts-card](https://github.com/RomRider/apexcharts-card) and
[config-template-card](https://github.com/iantrich/config-template-card).

The chart reads each block's own `from` timestamp, so a negative price is drawn
below zero and the 23/25-hour days still plot correctly.

---

## Troubleshooting

**Zone entities are unavailable.** They require a complete market day. Check the
log for "Today is incomplete". If the provider is returning a partial day, the
integration keeps retrying on every quarter tick; nothing is misreported in the
meantime.

**Tomorrow stays unavailable after 13:00.** The day-ahead prices are published
after the SDAC auction. Check
`binary_sensor.<provider>_tomorrow_prices_available` and the log; the request is
retried every 15 minutes, with a 30-minute backoff after repeated errors.

**Tibber setup says the token is invalid.** Tokens are created at
<https://developer.tibber.com/> and start with `tibber_pat_`. Use the
**Reauthenticate** action on the entry to replace one.

**Tibber says the currency is unsupported.** The integration's price entities
are denominated in euro. Tibber homes priced in NOK, SEK or DKK are rejected
during setup rather than publishing a value under the wrong unit.

**Prices look shifted by an hour or a day.** The integration uses the provider's
market timezone, not Home Assistant's configured timezone. Timestamps in
attributes are rendered in Home Assistant local time, as usual. Diagnostics show
both the market timezone and the Home Assistant timezone.

**Download diagnostics** from the integration page. For Tibber the access token
is redacted automatically; no other secrets are stored.

---

## Development

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements-test.txt
.venv/bin/python -m pytest
.venv/bin/ruff check custom_components tests
.venv/bin/ruff format --check custom_components tests
```

Layout:

```text
custom_components/quarter_prices/
├── providers/
│   ├── base.py       # PriceProvider interface + normalized block contract
│   ├── frank.py      # public Frank Energie GraphQL market prices
│   ├── tibber.py     # authenticated Tibber GraphQL prices
│   └── __init__.py   # registry + factory
├── config_flow.py    # provider selection, Tibber token/home, reauth, options
├── coordinator.py    # provider-independent cache, completeness, zones, periods
├── sensor.py         # 16 sensors + optional optimal-period sensors
├── binary_sensor.py  # availability, zone membership, optimal-period membership
├── price.py          # feed-in and dynamic-price calculations
├── price_zone.py     # cheap/expensive zone derivation
├── optimal_period.py # exact-duration cheapest/most expensive window
├── market_time.py    # market calendar day, independent of HA's timezone
├── diagnostics.py    # redacted diagnostics
└── translations/     # en, nl
```

Adding a provider means implementing `PriceProvider`, registering it in
`providers/__init__.py`, and adding its setup step to the config flow. The
coordinator, platforms and pure calculation modules do not change.

Tests cover both providers' parsing (including DST), the config and options
flows, reauth, diagnostics redaction, entity contracts, the Recorder attribute
limit and the original integration's coordinator, zone and optimal-period
suites.

---

## Attribution and license

The original integration and most of the non-provider logic are by
[Bennie-JC](https://github.com/Bennie-JC/ha-frank-quarter-prices), MIT licensed.
The Tibber provider, the provider abstraction and the provider-selecting config
flow are additions in this fork. See [LICENSE](LICENSE).

Tibber and Frank Energie are trademarks of their respective owners. This project
is not affiliated with either company.

---

## Publishing this fork

`manifest.json` keeps the upstream project's `codeowners`, `documentation` and
`issue_tracker` so the attribution and the HACS/hassfest checks stay valid.
Before publishing this fork under your own GitHub account, point those three
fields at your repository (and update `hacs.json` if needed). The integration
itself does not depend on them.
