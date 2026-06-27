"""Constants for the vtherm_smartpi integration."""

from __future__ import annotations

from enum import Enum

DOMAIN = "vtherm_smartpi"
NAME = "Versatile Thermostat SmartPI"

CONF_TARGET_VTHERM = "target_vtherm_unique_id"
CONF_PROP_FUNCTION = "proportional_function"
CONF_MINIMAL_ACTIVATION_DELAY = "minimal_activation_delay"
CONF_MINIMAL_DEACTIVATION_DELAY = "minimal_deactivation_delay"
CONF_SMART_PI_DEADBAND = "smart_pi_deadband"
CONF_SMART_PI_HYSTERESIS_ON = "smart_pi_hysteresis_on"
CONF_SMART_PI_HYSTERESIS_OFF = "smart_pi_hysteresis_off"
CONF_SMART_PI_DEBUG = "smart_pi_debug"
CONF_SMART_PI_USE_SETPOINT_FILTER = "smart_pi_use_setpoint_filter"
CONF_SMART_PI_USE_FF3 = "smart_pi_use_ff3"
CONF_SMART_PI_RELEASE_TAU_FACTOR = "smart_pi_release_tau_factor"
CONF_SMART_PI_DEADBAND_ALLOW_P = "smart_pi_deadband_allow_p"
CONF_SMART_PI_ALLOW_PWM_CYCLE_FORCE = "smart_pi_allow_pwm_cycle_force"
CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION = "smart_pi_enable_valve_linearization"
CONF_SMART_PI_MIN_VALVE = "smart_pi_min_valve"
CONF_SMART_PI_KNEE_DEMAND = "smart_pi_knee_demand"
CONF_SMART_PI_KNEE_VALVE = "smart_pi_knee_valve"
CONF_SMART_PI_MAX_VALVE = "smart_pi_max_valve"

# --- Room connections / coupled thermal modelling ---
# Optional real power sensor (watts) for this room, aggregated across the
# connected component by the RoomCouplingCoordinator.
CONF_SMART_PI_POWER_SENSOR = "smart_pi_power_sensor"
# List of inter-room connections declared on this room. Each item is a dict
# of {CONF_CONN_NEIGHBOR_VTHERM, CONF_CONN_DOOR_SENSOR}.
CONF_SMART_PI_CONNECTIONS = "smart_pi_connections"
# Keys inside a single connection dict.
CONF_CONN_NEIGHBOR_VTHERM = "neighbor_vtherm_entity"
CONF_CONN_DOOR_SENSOR = "connection_door_sensor"

# New per-aperture connection keys (superset of the legacy two keys above).
CONF_CONN_TARGET_KIND = "target_kind"            # room | sensor | outside
CONF_CONN_NEIGHBOR_TEMP_SENSOR = "neighbor_temp_sensor"  # when target_kind == sensor
CONF_CONN_APERTURE_SENSOR = "aperture_sensor"    # binary open/closed; supersedes door_sensor
CONF_CONN_APERTURE_TYPE = "aperture_type"        # door | window
CONF_CONN_OPEN_POLICY = "open_policy"            # model | trip_off

# Node kinds and aperture policies (string values are stored verbatim in config
# and used directly as EdgeConfig.target_kind / open_policy).
CONN_TARGET_ROOM = "room"
CONN_TARGET_SENSOR = "sensor"
CONN_TARGET_OUTSIDE = "outside"
CONN_APERTURE_DOOR = "door"
CONN_APERTURE_WINDOW = "window"
CONN_POLICY_MODEL = "model"
CONN_POLICY_TRIP_OFF = "trip_off"

DEFAULT_MINIMAL_ACTIVATION_DELAY = 0
DEFAULT_MINIMAL_DEACTIVATION_DELAY = 0
DEFAULT_SMART_PI_DEADBAND = 0.05
DEFAULT_SMART_PI_HYSTERESIS_ON = 0.3
DEFAULT_SMART_PI_HYSTERESIS_OFF = 0.5
DEFAULT_SMART_PI_DEBUG = False
DEFAULT_SMART_PI_USE_SETPOINT_FILTER = True
DEFAULT_SMART_PI_USE_FF3 = True
DEFAULT_SMART_PI_RELEASE_TAU_FACTOR = 0.5
DEFAULT_SMART_PI_DEADBAND_ALLOW_P = False
DEFAULT_SMART_PI_ALLOW_PWM_CYCLE_FORCE = False
DEFAULT_ENABLE_VALVE_LINEARIZATION = False
DEFAULT_MIN_VALVE = 7.0
DEFAULT_KNEE_DEMAND = 80.0
DEFAULT_KNEE_VALVE = 15.0
DEFAULT_MAX_VALVE = 100.0

DEFAULT_OPTIONS: dict[str, float | bool | int] = {
    CONF_MINIMAL_ACTIVATION_DELAY: DEFAULT_MINIMAL_ACTIVATION_DELAY,
    CONF_MINIMAL_DEACTIVATION_DELAY: DEFAULT_MINIMAL_DEACTIVATION_DELAY,
    CONF_SMART_PI_DEADBAND: DEFAULT_SMART_PI_DEADBAND,
    CONF_SMART_PI_USE_SETPOINT_FILTER: DEFAULT_SMART_PI_USE_SETPOINT_FILTER,
    CONF_SMART_PI_USE_FF3: DEFAULT_SMART_PI_USE_FF3,
    CONF_SMART_PI_HYSTERESIS_ON: DEFAULT_SMART_PI_HYSTERESIS_ON,
    CONF_SMART_PI_HYSTERESIS_OFF: DEFAULT_SMART_PI_HYSTERESIS_OFF,
    CONF_SMART_PI_RELEASE_TAU_FACTOR: DEFAULT_SMART_PI_RELEASE_TAU_FACTOR,
    CONF_SMART_PI_DEADBAND_ALLOW_P: DEFAULT_SMART_PI_DEADBAND_ALLOW_P,
    CONF_SMART_PI_ALLOW_PWM_CYCLE_FORCE: DEFAULT_SMART_PI_ALLOW_PWM_CYCLE_FORCE,
    CONF_SMART_PI_DEBUG: DEFAULT_SMART_PI_DEBUG,
    CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION: DEFAULT_ENABLE_VALVE_LINEARIZATION,
    CONF_SMART_PI_MIN_VALVE: DEFAULT_MIN_VALVE,
    CONF_SMART_PI_KNEE_DEMAND: DEFAULT_KNEE_DEMAND,
    CONF_SMART_PI_KNEE_VALVE: DEFAULT_KNEE_VALVE,
    CONF_SMART_PI_MAX_VALVE: DEFAULT_MAX_VALVE,
    CONF_SMART_PI_CONNECTIONS: [],
}

HVAC_OFF_REASON_WINDOW_DETECTION = "hvac_off_window_detection"
PROP_FUNCTION_SMART_PI = "smartpi"

DATA_FACTORY_REGISTERED = "factory_registered"
DATA_SERVICES_REGISTERED = "services_registered"
SIGNAL_SMARTPI_TARGET_UPDATED = "vtherm_smartpi_target_updated"
DIAGNOSTIC_SENSOR_UNIQUE_ID_PREFIX = "smartpi_diag"

SERVICE_RESET_SMARTPI_LEARNING = "reset_smartpi_learning"
SERVICE_FORCE_SMARTPI_CALIBRATION = "force_smartpi_calibration"
SERVICE_RESET_SMARTPI_INTEGRAL = "reset_smartpi_integral"


class EventType(Enum):
    """Plugin events exposed by vtherm_smartpi."""

    SMART_PI_EVENT = "vtherm_smartpi_event"
