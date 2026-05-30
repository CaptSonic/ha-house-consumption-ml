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

Alle Sensoren erscheinen unter dem Gerät **„House Consumption ML"** in Einstellungen → Geräte & Dienste.

| Sensor (Entity-ID) | Einheit | Beschreibung |
|--------|---------|--------------|
| `sensor.hcml_prognose_heute` | kWh | Prognose heute |
| `sensor.hcml_prognose_morgen` | kWh | Morgen |
| `sensor.hcml_prognose_ubermorgen` | kWh | Übermorgen |
| `sensor.hcml_prognose_tag_3` … `_6` | kWh | Tage 3–6 (Wochentag dynamisch) |
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
5. Go to **Settings → Devices & Services → Add Integration** and search for *House Consumption ML*
6. Follow the setup wizard — all fields are optional, auto-discovery handles the rest

### Manual

1. Copy `custom_components/house_consumption_ml/` to your HA config directory
2. Restart Home Assistant
3. Go to **Settings → Devices & Services → Add Integration** → *House Consumption ML*

## Configuration

All settings are configured through the Home Assistant UI. After setup, click **Configure** on the integration card to open the options form:

| Option | Description |
|--------|-------------|
| **House power sensor** | Override auto-discovery. Must report total consumption in Watts (W, always ≥ 0). Leave empty for auto-discovery. |
| **Calendars** | Holiday/vacation `calendar.*` entities. Days with events are treated as non-workdays. **Do not add garbage-collection calendars.** |
| **Exclude devices** | One name fragment per line. Sensors whose entity_id or friendly name contains any fragment are excluded (solar inverters, batteries, …). |
| **DB path** | Path to the SQLite database (default: `/config/house_consumption_ml.db`). |
| **SFML DB path** | Optional read-only path to an SFML solar_forecast.db for bootstrap data. |

Changes take effect immediately — **no HA restart required**.

### Legacy YAML configuration

> **Deprecated:** YAML configuration is still supported for backward compatibility but is no longer required. If you have an existing YAML block, it is automatically imported into a config entry on first start. You can then safely remove it from `configuration.yaml`.

<details>
<summary>Show YAML example</summary>

```yaml
house_consumption_ml:
  house_power_sensor: sensor.sfml_house_power
  exclude_devices:
    - hyper_2000      # solar inverter
    - ab2000          # BKW battery
  calendars:
    - calendar.urlaub
    - calendar.feiertage_deutschland
  db_path: /config/house_consumption_ml.db
  sfml_db_path: /config/solar_forecast.db
```

</details>

### `house_power_sensor`

The sensor must return **actual house consumption in Watts (W), always ≥ 0**. Do not use a net-grid sensor (which goes negative during solar export). A template sensor that calculates `grid_import + solar_self_consumption` is ideal.

### `calendars`

A list of `calendar.*` entity IDs. Any day that has at least one event in any of the listed calendars is treated as a non-workday — the model uses the weekend/holiday consumption profile instead of the weekday profile.

**Important:** only add calendars that contain holidays or vacation. A garbage-collection calendar with entries on regular workdays would incorrectly suppress the workday profile on those days.

To find the right entity IDs, check the `calendars_available` attribute of `sensor.hcml_discovery`. It lists every calendar HA currently knows about.

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

Check `sensor.hcml_discovery` attributes to see what was found.

## Sensors created

All sensors are grouped under a single **"House Consumption ML"** device in Settings → Devices & Services → Devices. Entity IDs remain stable regardless of display name changes.

| Sensor | Unit | Description |
|--------|------|-------------|
| `sensor.hcml_prognose_heute` | kWh | Today's forecast |
| `sensor.hcml_prognose_morgen` | kWh | Tomorrow |
| `sensor.hcml_prognose_ubermorgen` | kWh | Day after tomorrow |
| `sensor.hcml_prognose_tag_3` … `_6` | kWh | Days 3–6 (German weekday name, dynamic) |
| `sensor.hcml_7_tage_prognose` | kWh | 7-day total |
| `sensor.hcml_modell_status` | % | Model R² (diagnostic) |
| `sensor.hcml_ist_verbrauch_gestern` | kWh | Yesterday's actual consumption (diagnostic) |
| `sensor.hcml_prognose_genauigkeit` | % | Yesterday's forecast accuracy + explanation (diagnostic) |
| `sensor.hcml_discovery` | — | Discovered entities incl. available calendars (diagnostic) |

### Day sensor attributes

```yaml
date: "2026-05-27"
day_name: "Wednesday"
day_name_de: "Mittwoch"
base_kwh: 3.2          # base load contribution
device_kwh: 1.8        # device pattern contribution
hourly_kwh: [0.18, 0.17, ...]          # 24 total forecast values (kWh)
hourly_base_kwh: [0.10, 0.09, ...]     # 24 base load values
hourly_device_kwh: [0.08, 0.08, ...]   # 24 device pattern values
hourly_actual_kwh: [0.21, 0.19, ...]   # today only: measured kWh per hour (null for future hours)
hourly_detail:
  "18:00":
    total: 0.32
    base: 0.18
    device: 0.14
peak_hour: "19:00"
night_kwh: 1.2
day_kwh: 5.4
```

> `hourly_actual_kwh` is only populated for `sensor.hcml_prognose_heute` (today). It allows overlaying actual vs. forecast on the same chart.

### Accuracy sensor attributes

```yaml
# sensor.hcml_prognose_genauigkeit
state: 87.3   # %

date: "2026-05-27"
forecast_kwh: 18.4
actual_kwh: 21.1
delta_kwh: 2.7
frozen_at: "2026-05-27T00:12:34+00:00"
hourly_wh: [310, 290, ...]   # frozen forecast Wh per hour (for yesterday comparison chart)
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

### Snapshot sensor attributes

```yaml
# sensor.hcml_ist_verbrauch_gestern
date: "2026-05-27"
hours_count: 24
hourly_wh: [272, 288, ...]   # actual measured Wh per hour
recorded_at: "2026-05-28T00:31:00+00:00"
```

## Dashboard example (ApexCharts)

### Today — Forecast + Actual

```yaml
type: custom:apexcharts-card
graph_span: 24h
span:
  start: day
header:
  show: false
update_interval: '3600'
apex_config:
  xaxis:
    type: datetime
    labels:
      format: HH:mm
  stroke:
    width: [0, 2, 2, 0]
series:
  - entity: sensor.hcml_prognose_heute
    name: Prognose Gesamt
    type: area
    color: '#FF6B35'
    opacity: 0.4
    data_generator: |
      const h = entity.attributes.hourly_kwh || [];
      const dt = entity.attributes.date;
      if (!dt || !h.length) return [];
      return h.map((v, i) => {
        const d = new Date(dt + 'T00:00:00'); d.setHours(i);
        return [d.getTime(), +(v).toFixed(3)];
      });
  - entity: sensor.hcml_prognose_heute
    name: Grundlast
    type: line
    color: '#42A5F5'
    data_generator: |
      const h = entity.attributes.hourly_base_kwh || [];
      const dt = entity.attributes.date;
      if (!dt || !h.length) return [];
      return h.map((v, i) => {
        const d = new Date(dt + 'T00:00:00'); d.setHours(i);
        return [d.getTime(), +(v).toFixed(3)];
      });
  - entity: sensor.hcml_prognose_heute
    name: Geräte
    type: line
    color: '#66BB6A'
    data_generator: |
      const h = entity.attributes.hourly_device_kwh || [];
      const dt = entity.attributes.date;
      if (!dt || !h.length) return [];
      return h.map((v, i) => {
        const d = new Date(dt + 'T00:00:00'); d.setHours(i);
        return [d.getTime(), +(v).toFixed(3)];
      });
  - entity: sensor.hcml_prognose_heute
    name: Tatsächlich
    type: column
    color: '#26C6DA'
    opacity: 0.8
    data_generator: |
      const h = entity.attributes.hourly_actual_kwh || [];
      const dt = entity.attributes.date;
      if (!dt) return [];
      return h.reduce((acc, v, i) => {
        if (v !== null && v !== undefined) {
          const d = new Date(dt + 'T00:00:00'); d.setHours(i);
          acc.push([d.getTime(), +(v).toFixed(3)]);
        }
        return acc;
      }, []);
```

### Yesterday — Frozen Forecast vs. Actual

```yaml
type: custom:apexcharts-card
graph_span: 24h
span:
  start: day
  offset: '-1d'
header:
  show: false
update_interval: '3600'
apex_config:
  xaxis:
    type: datetime
    labels:
      format: HH:mm
  stroke:
    width: [0, 2]
series:
  - entity: sensor.hcml_ist_verbrauch_gestern
    name: Tatsächlich
    type: column
    color: '#26C6DA'
    opacity: 0.8
    data_generator: |
      const h = entity.attributes.hourly_wh || [];
      const dt = entity.attributes.date;
      if (!dt || !h.length) return [];
      return h.reduce((acc, v, i) => {
        if (v !== null && v !== undefined) {
          const d = new Date(dt + 'T00:00:00'); d.setHours(i);
          acc.push([d.getTime(), +(v / 1000).toFixed(3)]);
        }
        return acc;
      }, []);
  - entity: sensor.hcml_prognose_genauigkeit
    name: Prognose (gefroren)
    type: line
    color: '#FF6B35'
    data_generator: |
      const h = entity.attributes.hourly_wh || [];
      const dt = entity.attributes.date;
      if (!dt || !h.length) return [];
      return h.map((v, i) => {
        const d = new Date(dt + 'T00:00:00'); d.setHours(i);
        return [d.getTime(), +((v || 0) / 1000).toFixed(3)];
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
