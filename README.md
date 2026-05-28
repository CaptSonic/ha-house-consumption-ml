# House Consumption ML Forecast

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2023.1%2B-blue)](https://www.home-assistant.io)
[![Development Status](https://img.shields.io/badge/status-early%20alpha-red.svg)]()

> **⚠️ Early Development — Use at your own risk**
>
> This integration is in a **very early stage of development**. Expect bugs, breaking changes between versions, and incomplete features. It has only been tested on a single Home Assistant setup.
>
> **No liability is accepted** for any damage, data loss, incorrect forecasts, or other issues arising from the use of this software. See the [MIT License](LICENSE) for the full disclaimer.

---

## Deutsch

Eine Home Assistant Custom Integration, die den Haushalts-Stromverbrauch für die nächsten **7 Tage stundengenau** prognostiziert — mittels Ridge-Regression, vollständig lokal, ohne Cloud-Dienste oder externe Abhängigkeiten.

### Wie es funktioniert

Das Modell trennt den Verbrauch in zwei Komponenten:

- **Grundlast** (Ridge-Regression) — Dauerverbraucher, Heizung, Warmwasser, Standby. Gelernt aus Tageszeit, Wochentag, Saison, Anwesenheit und Wetter.
- **Gerätmuster** — Historischer Durchschnittsverbrauch jedes Geräts nach (Wochentag, Stunde). Nach einigen Wochen lassen sich Muster erkennen wie „Fernseher läuft typischerweise ab 18 Uhr an Werktagen".

**Prognose = Grundlast-Vorhersage + Σ(Gerät-Durchschnittswatt)**

Jeden Morgen zwischen 0:00 und 6:00 Uhr wird die Prognose für den laufenden Tag **eingefroren**. Am nächsten Morgen vergleicht die Integration automatisch Prognose vs. Ist-Verbrauch und liefert eine **Genauigkeitsangabe in %** sowie eine **Erklärung** welche Geräte die Abweichung verursacht haben — z. B. „Waschmaschine +1.2 kWh (unerwartet aktiv)".

Über die optionale **Kalender-Integration** lassen sich Urlaubs- und Feiertagskalender einbinden. An Tagen mit einem Kalender-Eintrag verwendet das Modell automatisch das Wochenend-/Feiertagsprofil statt des Werktag-Profils.

### Sensoren (Übersicht)

| Sensor | Einheit | Beschreibung |
|--------|---------|--------------|
| `sensor.hcml_prognose_heute` | kWh | Prognose heute |
| `sensor.hcml_prognose_morgen` | kWh | Morgen |
| `sensor.hcml_prognose_ubermorgen` | kWh | Übermorgen |
| `sensor.hcml_prognose_tag_3` … `_6` | kWh | Tage 3–6 |
| `sensor.hcml_7_tage_prognose` | kWh | 7-Tage-Summe |
| `sensor.hcml_modell_status` | % | Modell-R² (Diagnose) |
| `sensor.hcml_ist_verbrauch_gestern` | kWh | Tatsächlicher Verbrauch gestern |
| `sensor.hcml_prognose_genauigkeit` | % | Genauigkeit der gestrigen Prognose + Erklärung |
| `sensor.hcml_discovery` | — | Erkannte Entitäten inkl. verfügbarer Kalender (Diagnose) |

---

## English

A Home Assistant custom integration that predicts your household electricity consumption for the next 7 days, hour by hour — using Ridge regression, no cloud, no external dependencies.

## How it works

The model separates consumption into two components:

- **Base load** (Ridge regression) — always-on appliances, heating, hot water, standby. Learned from time-of-day, day-of-week, season, presence, and weather.
- **Device patterns** — each tracked appliance's historical average wattage by (weekday, hour). After a few weeks you can see patterns like "TV is typically on from 18:00 on weekdays."

**Forecast = base load prediction + Σ(device pattern watts)**

Every morning between 00:00 and 06:00 the current forecast for the day is **frozen** once. The following morning the integration automatically compares the frozen forecast against the actual consumption and stores an **accuracy percentage** together with a **device-level explanation** of the delta (e.g. "Washing machine +1.2 kWh (unexpectedly active)").

**Calendar integration** (optional): configure holiday and vacation calendars and the model will automatically switch to a weekend/holiday load profile on those days instead of the regular weekday profile.

### Features used for base load model

| Feature | Description |
|---------|-------------|
| `h_sin / h_cos` | Hour of day (cyclic) |
| `d_sin / d_cos` | Day of week (cyclic) |
| `m_sin / m_cos` | Month (cyclic) |
| `is_workday` | Workday flag (sensor or weekday fallback; overridden by calendar) |
| `any_home / n_home_n` | Presence (any person home, fraction) |
| `temperature_n` | Normalised outdoor temperature |
| `cloud_n` | Cloud cover 0–1 |
| `heat_deg` | Heating demand proxy: `max(0, 18 − T) / 10` |
| `cool_deg` | Cooling demand proxy: `max(0, T − 23) / 10` |

Training uses **exponential decay sample weights** (30-day half-life) so recent data has more influence than old bootstrap data.

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

  # Calendars for holidays and vacation days (optional)
  # Days with any event in these calendars are treated as non-workdays.
  # Tip: check sensor.hcml_discovery → calendars_available to see all available calendar IDs.
  calendars:
    - calendar.urlaub
    - calendar.feiertage_deutschland

  # Paths to SQLite databases (defaults shown)
  db_path: /config/house_consumption_ml.db
  sfml_db_path: /config/solar_forecast.db   # read-only if present
```

### `house_power_sensor`

The sensor must return **actual house consumption in Watts (W), always ≥ 0**. Do not use a net-grid sensor (which goes negative during solar export). A template sensor that calculates `grid_import + solar_self_consumption` is ideal.

### `exclude_devices`

Any sensor whose `entity_id` or friendly name contains one of these strings (case-insensitive) is excluded from the appliance list. Use this for solar inverters, batteries, or any device that is not a consumer load.

### `calendars`

A list of `calendar.*` entity IDs. Any day that has at least one event in any of the listed calendars is treated as a non-workday — the model uses the weekend/holiday consumption profile instead of the weekday profile. This applies both to training data collection and to the 7-day forecast.

**Important:** only add calendars that contain holidays or vacation. A garbage-collection calendar with entries on regular workdays would incorrectly suppress the workday profile on those days — just leave it out.

To find the right entity IDs, check the `calendars_available` attribute of `sensor.hcml_discovery` after a restart. It lists every calendar HA currently knows about.

## Auto-discovery

On first start the integration scans all HA entities and classifies them automatically:

| What | How |
|------|-----|
| **House power sensor** | `device_class: power` + keywords (house/haus/total/gesamt) |
| **Appliances** | Power sensors that are not solar/battery/grid/inverter |
| **Persons** | All `person.*` entities |
| **Weather** | Priority: fusion → forecast → openweathermap → bright_sky |
| **Workday** | `binary_sensor.*workday*` / `*werktag*` |
| **Calendars** | All `calendar.*` entities — listed in discovery for reference |

Check `sensor.hcml_discovery` attributes to see what was found. The `calendars_available` attribute shows every calendar HA knows about; copy the IDs you want into the `calendars:` config option.

## Sensors created

| Sensor | Unit | Description |
|--------|------|-------------|
| `sensor.hcml_prognose_heute` | kWh | Today's forecast |
| `sensor.hcml_prognose_morgen` | kWh | Tomorrow |
| `sensor.hcml_prognose_ubermorgen` | kWh | Day after tomorrow |
| `sensor.hcml_prognose_tag_3` … `_6` | kWh | Days 3–6 |
| `sensor.hcml_7_tage_prognose` | kWh | 7-day total |
| `sensor.hcml_modell_status` | % | Model R² (diagnostic) |
| `sensor.hcml_ist_verbrauch_gestern` | kWh | Yesterday's actual consumption (diagnostic) |
| `sensor.hcml_prognose_genauigkeit` | % | Yesterday's forecast accuracy + explanation (diagnostic) |
| `sensor.hcml_discovery` | — | Discovered entities incl. available calendars (diagnostic) |

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

### Accuracy sensor attributes

```yaml
# sensor.hcml_prognose_genauigkeit
state: 87.3   # %

date: "2026-05-27"
forecast_kwh: 18.4
actual_kwh: 21.1
delta_kwh: 2.7
frozen_at: "2026-05-27T00:12:34+00:00"
explanation:
  - label: "Waschmaschine +1.20 kWh (unerwartet aktiv)"
    name: "Waschmaschine"
    delta_kwh: 1.2
    direction: "unerwartet aktiv"
  - label: "Grundlast +1.50 kWh (höher als erwartet)"
    name: "Grundlast"
    delta_kwh: 1.5
    direction: "höher als erwartet"
```

## Dashboard example (ApexCharts)

```yaml
type: custom:apexcharts-card
graph_span: 24h
span:
  start: day
header:
  title: Hausverbrauch Prognose — Heute
apex_config:
  xaxis:
    type: datetime
    labels:
      format: HH:mm
update_interval: '3600'
series:
  - entity: sensor.hcml_prognose_heute
    name: Gesamt
    type: area
    color: '#FF6B35'
    data_generator: |
      const date = entity.attributes.date;
      return (entity.attributes.hourly_kwh || []).map((kwh, h) => {
        const ts = new Date(date + 'T' + String(h).padStart(2,'0') + ':00:00');
        return [ts.getTime(), kwh];
      });
  - entity: sensor.hcml_prognose_heute
    name: Grundlast
    type: line
    color: '#42A5F5'
    data_generator: |
      const date = entity.attributes.date;
      return (entity.attributes.hourly_base_kwh || []).map((kwh, h) => {
        const ts = new Date(date + 'T' + String(h).padStart(2,'0') + ':00:00');
        return [ts.getTime(), kwh];
      });
  - entity: sensor.hcml_prognose_heute
    name: Geräte
    type: line
    color: '#66BB6A'
    data_generator: |
      const date = entity.attributes.date;
      return (entity.attributes.hourly_device_kwh || []).map((kwh, h) => {
        const ts = new Date(date + 'T' + String(h).padStart(2,'0') + ':00:00');
        return [ts.getTime(), kwh];
      });
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
