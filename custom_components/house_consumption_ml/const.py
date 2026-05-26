"""Constants for House Consumption ML Forecast."""

DOMAIN = "house_consumption_ml"

# Optional config keys (all have auto-discovered defaults)
CONF_DB_PATH            = "db_path"
CONF_SFML_DB_PATH       = "sfml_db_path"
CONF_HOUSE_POWER_SENSOR = "house_power_sensor"   # override auto-discovery
CONF_EXCLUDE_DEVICES    = "exclude_devices"       # list of name fragments to skip

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
