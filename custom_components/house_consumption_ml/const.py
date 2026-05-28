"""Constants for House Consumption ML Forecast."""

DOMAIN = "house_consumption_ml"

# Optional config keys (all have auto-discovered defaults)
CONF_DB_PATH            = "db_path"
CONF_SFML_DB_PATH       = "sfml_db_path"
CONF_HOUSE_POWER_SENSOR = "house_power_sensor"   # override auto-discovery
CONF_EXCLUDE_DEVICES    = "exclude_devices"       # list of name fragments to skip
CONF_CALENDARS          = "calendars"             # list of calendar entity IDs (holidays/vacation)

DEFAULT_DB_PATH      = "/config/house_consumption_ml.db"
DEFAULT_SFML_DB_PATH = "/config/solar_forecast.db"

# Update interval
UPDATE_INTERVAL_MINUTES = 60

# Forecast horizon
FORECAST_DAYS = 7

# Days of HA recorder history to import on first run
BOOTSTRAP_DAYS = 90

# Minimum stored rows before the model trains for the first time
MIN_SAMPLES = 24

# Ridge regularisation (higher → smoother, less sensitive to outliers)
RIDGE_ALPHA = 10.0

# Reject training rows further than this many σ from the mean
OUTLIER_STD = 3.0

# A device is considered "on" if its power reading exceeds this threshold (W)
DEVICE_ON_THRESHOLD_W = 10.0

# A device is considered "large" (washer / dryer / oven) above this threshold
LARGE_DEVICE_THRESHOLD_W = 1_500.0

# Minimum number of recorded hours before a daily snapshot is considered valid
# (guards against snapshotting days where HA was mostly offline)
SNAPSHOT_MIN_HOURS = 12

# Plausibility thresholds for incoming house power readings
# Values outside this range are logged as warnings and not stored
PLAUSIBILITY_MIN_W =    50.0    # Below this → likely sensor error / no real load
PLAUSIBILITY_MAX_W = 15_000.0   # Above this → likely sensor spike

# Drift detection: warn when rolling accuracy falls below this threshold
DRIFT_WARNING_THRESHOLD_PCT = 70.0   # %
DRIFT_MIN_DAYS              =  3     # Minimum accuracy data points before flagging
