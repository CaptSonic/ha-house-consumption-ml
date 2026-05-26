# House Consumption ML Forecast

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2023.1%2B-blue)](https://www.home-assistant.io)

A Home Assistant custom integration that predicts your household electricity consumption for the next 7 days, hour by hour — using Ridge regression, no cloud, no external dependencies.

## How it works

The model separates consumption into two components:

- **Base load** (Ridge regression) — always-on appliances, heating, hot water, standby. Learned from time-of-day, day-of-week, season, presence, and weather.
- **Device patterns** — each tracked appliance's historical average wattage by (weekday, hour). After a few weeks you can see patterns like "TV is typically on from 18:00 on weekdays."

**Forecast = base load prediction + Σ(device pattern watts)**

### Features used for base load model

| Feature | Description |
|---------|-------------|
| `h_sin / h_cos` | Hour of day (cyclic) |
| `d_sin / d_cos` | Day of week (cyclic) |
| `m_sin / m_cos` | Month (cyclic) |
| `is_workday` | Binary workday sensor |
| `any_home / n_home_n` | Presence (any person home, fraction) |
| `temperature_n` | Normalised outdoor temperature |
| `cloud_n` | Cloud cover 0–1 |

## Requirements

- Home Assistant 2023.1+
- `recorder` integration (built-in, enabled by default)
- No pip dependencies — pure numpy (already bundled with HA)

## Installation

### Via HACS (recommended)

1. Open HACS → Integrations → ⋮ → Custom repositories
2. Add this repository URL, category: **Integration**
3. Install **House Consumption ML Forecast**
4. Restart Home Assistant

### Manual

Copy `custom_components/house_consumption_ml/` to your HA config directory and restart.

## Configuration

Add to `configuration.yaml` (everything is optional — the integration auto-discovers all entities):

```yaml
house_consumption_ml:
  # Override the house power sensor (recommended if auto-discovery picks the wrong one)
  house_power_sensor: sensor.sfml_house_power

  # Exclude devices by name fragment (entity_id or friendly name, case-insensitive)
  exclude_devices:
    - hyper_2000      # solar inverter
    - ab2000          # BKW battery
    - my_inverter

  # Paths to SQLite databases (defaults shown)
  db_path: /config/house_consumption_ml.db
  sfml_db_path: /config/solar_forecast.db   # read-only if present
```

### `house_power_sensor`

The sensor must return **actual house consumption in Watts (W), always ≥ 0**. Do not use a net-grid sensor (which goes negative during solar export). A template sensor that calculates `grid_import + solar_self_consumption` is ideal.

### `exclude_devices`

Any sensor whose `entity_id` or friendly name contains one of these strings (case-insensitive) is excluded from the appliance list. Use this for solar inverters, batteries, or any device that is not a consumer load.

## Auto-discovery

On first start the integration scans all HA entities and classifies them automatically:

| What | How |
|------|-----|
| **House power sensor** | `device_class: power` + keywords (house/haus/total/gesamt) |
| **Appliances** | Power sensors that are not solar/battery/grid/inverter |
| **Persons** | All `person.*` entities |
| **Weather** | Priority: fusion → forecast → openweathermap → bright_sky |
| **Workday** | `binary_sensor.*workday*` / `*werktag*` |

Check `sensor.hcml_discovery` attributes to see what was found.

## Sensors created

| Sensor | Unit | Description |
|--------|------|-------------|
| `sensor.hcml_prognose_heute` | kWh | Today's forecast |
| `sensor.hcml_prognose_morgen` | kWh | Tomorrow |
| `sensor.hcml_prognose_ubermorgen` | kWh | Day after tomorrow |
| `sensor.hcml_prognose_tag_3` … `_6` | kWh | Days 3–6 |
| `sensor.hcml_7_tage_prognose` | kWh | 7-day total |
| `sensor.hcml_modell_status` | % | Model R² (diagnostic) |
| `sensor.hcml_discovery` | — | Discovered entities (diagnostic) |

### Day sensor attributes

```yaml
date: "2026-05-27"
day_name: "Wednesday"
base_kwh: 3.2          # base load contribution
device_kwh: 1.8        # device pattern contribution
hourly_kwh: [0.18, 0.17, ...]          # 24 total values
hourly_base_kwh: [0.10, 0.09, ...]     # 24 base load values
hourly_device_kwh: [0.08, 0.08, ...]   # 24 device values
hourly_detail:
  "18:00":
    total: 0.32
    base: 0.18
    device: 0.14
peak_hour: "19:00"
night_kwh: 1.2
day_kwh: 5.4
```

## Dashboard example (ApexCharts)

```yaml
type: custom:apexcharts-card
graph_span: 7d
header:
  title: House Consumption Forecast
series:
  - entity: sensor.hcml_7_tage_prognose
    data_generator: |
      const days = entity.attributes.days || [];
      return days.flatMap(day =>
        day.hourly_kwh.map((kwh, h) => {
          const ts = new Date(day.date + 'T' + String(h).padStart(2,'0') + ':00:00');
          return [ts.getTime(), kwh];
        })
      );
    name: Forecast (kWh/h)
    type: bar
    color: '#FF6B35'
```

## Model quality

The R² score improves over time as live data (with device states and presence) accumulates:

| Timeframe | What the model learns |
|-----------|-----------------------|
| Day 1 | Bootstrap from 90 days of HA recorder history |
| Week 1 | Presence patterns, daily rhythms |
| Week 4 | Weekday/weekend patterns stable |
| Month 3 | Seasonal effects visible |

The bootstrap data from the recorder contains no device information, so initial R² may be low (20–30%). This improves as hourly live data with device readings fills in.
