# pylint: disable=line-too-long, abstract-method
"""SmartPI algorithm handler for the plugin runtime."""

import logging
import time
from typing import TYPE_CHECKING
from homeassistant.util import slugify
from homeassistant.helpers.storage import Store
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.dispatcher import async_dispatcher_send
from datetime import timedelta

from .algo import SmartPI
from .cycle_utils import calculate_cycle_times
from .smartpi.const import (
    SMARTPI_RECALC_INTERVAL_SEC,
    SmartPIPhase,
    SmartPICalibrationPhase,
    SmartPICalibrationResult,
    NEAR_BAND_HYSTERESIS_C,
)
from .const import (
    CONF_TARGET_VTHERM,
    CONF_MINIMAL_ACTIVATION_DELAY,
    CONF_MINIMAL_DEACTIVATION_DELAY,
    CONF_SMART_PI_DEADBAND,
    CONF_SMART_PI_USE_SETPOINT_FILTER,
    CONF_SMART_PI_USE_FF3,
    CONF_SMART_PI_HYSTERESIS_ON,
    CONF_SMART_PI_HYSTERESIS_OFF,
    CONF_SMART_PI_RELEASE_TAU_FACTOR,
    CONF_SMART_PI_DEADBAND_ALLOW_P,
    CONF_SMART_PI_DEBUG,
    DEFAULT_OPTIONS,
    DOMAIN,
    EventType,
    SIGNAL_SMARTPI_TARGET_UPDATED,
)
from .hvac_mode import VThermHvacMode_OFF, VThermHvacMode_HEAT, VThermHvacMode_COOL
from .commons import write_event_log

if TYPE_CHECKING:
    from vtherm_api.interfaces import InterfaceThermostatRuntime

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = "vtherm_smartpi.{}"
LEGACY_STORAGE_KEY = "versatile_thermostat.smartpi.{}"

class SmartPIHandler:
    """Handler for SmartPI-specific logic."""

    def __init__(self, thermostat: "InterfaceThermostatRuntime"):
        """Initialize handler with parent thermostat reference."""
        self._thermostat = thermostat
        self._store: Store | None = None
        self._legacy_store: Store | None = None
        self._recalc_timer_remove = None
        # State for learning
        self._last_temp = None
        self._last_ext_temp = None
        self._last_time = None
        self._last_on_percent = 0.0
        self._should_publish_intermediate: bool = True
        # Track calibration state for completion detection
        self._prev_is_calibrating: bool = False

    def init_algorithm(self):
        """Initialize SmartPI algorithm."""
        t = self._thermostat
        entry = self._get_effective_config()

        # Initialize storage with slugified name to allow retrieval if re-created
        safe_name = slugify(t.name)
        self._store = Store(t.hass, STORAGE_VERSION, STORAGE_KEY.format(safe_name))
        self._legacy_store = Store(
            t.hass, STORAGE_VERSION, LEGACY_STORAGE_KEY.format(safe_name)
        )

        # Use the thermostat's cycle_min property directly for consistency
        # (base_thermostat reads CONF_CYCLE_MIN in __init__)
        cycle_min = t.cycle_min
        minimal_activation_delay = entry.get(CONF_MINIMAL_ACTIVATION_DELAY, 0)
        minimal_deactivation_delay = entry.get(CONF_MINIMAL_DEACTIVATION_DELAY, 0)
        max_on_percent = getattr(t, "max_on_percent", None)

        # Update thermostat attributes directly (like TPIHandler)
        t.minimal_activation_delay = minimal_activation_delay
        t.minimal_deactivation_delay = minimal_deactivation_delay

        # SmartPI specific
        deadband = entry.get(CONF_SMART_PI_DEADBAND, 0.05)
        use_setpoint_filter = entry.get(CONF_SMART_PI_USE_SETPOINT_FILTER, True)
        use_ff3 = entry.get(CONF_SMART_PI_USE_FF3, False)
        hyst_on = entry.get(CONF_SMART_PI_HYSTERESIS_ON, 0.3)
        hyst_off = entry.get(CONF_SMART_PI_HYSTERESIS_OFF, 0.5)
        release_tau_factor = entry.get(CONF_SMART_PI_RELEASE_TAU_FACTOR, 0.5)
        deadband_allow_p = entry.get(CONF_SMART_PI_DEADBAND_ALLOW_P, False)
        debug_mode = entry.get(CONF_SMART_PI_DEBUG, False)
        # Create SmartPI instance
        # Note: saved_state is loaded asynchronously later
        t.prop_algorithm = SmartPI(
            hass=t.hass,
            cycle_min=cycle_min,
            minimal_activation_delay=minimal_activation_delay,
            minimal_deactivation_delay=minimal_deactivation_delay,
            name=t.name,
            max_on_percent=max_on_percent,
            deadband_c=deadband,
            use_setpoint_filter=use_setpoint_filter,
            use_ff3=use_ff3,
            hysteresis_on=hyst_on,
            hysteresis_off=hyst_off,
            release_tau_factor=release_tau_factor,
            deadband_allow_p=deadband_allow_p,
            debug_mode=debug_mode,
        )

        _LOGGER.info("%s - SmartPI Algorithm initialized", t)
        async_dispatcher_send(t.hass, SIGNAL_SMARTPI_TARGET_UPDATED, t.unique_id)

    def _get_effective_config(self) -> dict:
        """Return the merged SmartPI configuration for the thermostat."""
        t = self._thermostat
        config = dict(DEFAULT_OPTIONS)
        config.update(t.entry_infos or {})

        plugin_entries = t.hass.config_entries.async_entries(DOMAIN)
        matching_entry = next(
            (
                entry
                for entry in plugin_entries
                if entry.data.get(CONF_TARGET_VTHERM) == t.unique_id
            ),
            None,
        )
        global_entry = next(
            (entry for entry in plugin_entries if entry.unique_id == DOMAIN),
            None,
        )
        entry_to_apply = matching_entry or global_entry
        if entry_to_apply is not None:
            config.update(entry_to_apply.data)
            config.update(entry_to_apply.options)

        return config

    async def async_added_to_hass(self):
        """Load persistent data."""
        t = self._thermostat
        if self._store:
            try:
                data = await self._store.async_load()
                if data is None and self._legacy_store is not None:
                    data = await self._legacy_store.async_load()
                    if data is not None:
                        t.hass.async_create_task(self._store.async_save(data))
                if data and t.prop_algorithm:
                    t.prop_algorithm.load_state(data)
                    _LOGGER.debug("%s - SmartPI state loaded", t)
            except Exception as e:
                _LOGGER.error("%s - Failed to load SmartPI state: %s", t, e)

    async def async_startup(self):
        """Startup actions."""
        # Initialize the cycle start state if possible to enable first-cycle learning
        t = self._thermostat
        if t.prop_algorithm and isinstance(t.prop_algorithm, SmartPI):
            # Check availability of sensors
            if t.current_temperature is not None and t.current_outdoor_temperature is not None:
                _LOGGER.debug("%s - SmartPI startup: ready for cycle management", t)

        # Check if we need to start the periodic recalculation timer
        await self.on_state_changed(True)

    async def _async_save(self):
        """Save SmartPI state to storage."""
        t = self._thermostat
        if self._store and t.prop_algorithm:
            try:
                data = t.prop_algorithm.save_state()
                t.hass.async_create_task(self._store.async_save(data))
                _LOGGER.debug("%s - SmartPI state saved", t)
            except Exception as e:
                _LOGGER.error("%s - Failed to save SmartPI state: %s", t, e)

    def remove(self):
        """Cleanup and save state on removal."""
        t = self._thermostat
        if self._store and t.prop_algorithm:
            # We can't await here easily, but we schedule save
            t.hass.async_create_task(self._async_save())

        self._stop_recalc_timer()

    def on_scheduler_ready(self, scheduler) -> None:
        """Register SmartPI learning callbacks on the cycle scheduler."""
        algo = self._thermostat.prop_algorithm
        if algo:
            scheduler.register_cycle_start_callback(algo.on_cycle_started)
            scheduler.register_cycle_end_callback(algo.on_cycle_completed)

    def should_publish_intermediate(self) -> bool:
        """Return True when VT may publish the current control iteration."""
        return self._should_publish_intermediate

    async def control_heating(self, timestamp=None, force=False):
        """Control heating using SmartPI."""
        t = self._thermostat
        from datetime import datetime
        from .smartpi.guards import GuardAction

        algo = t.prop_algorithm if isinstance(t.prop_algorithm, SmartPI) else None
        previous_committed = algo.committed_on_percent if algo is not None else None
        self._should_publish_intermediate = (
            algo is None or timestamp is not None or force
        )

        # When a forced recalculation is requested by the thermostat state machine
        # (setpoint/hvac/preset changes), close the running cycle first so the cycle-end
        # callback updates learning context before the next calculate().
        if force and t.cycle_scheduler and t.cycle_scheduler.is_cycle_running:
            if t.prop_algorithm and isinstance(t.prop_algorithm, SmartPI):
                t.prop_algorithm._last_restart_reason = "external_force"
            await t.cycle_scheduler.cancel_cycle()
            force = False

        if t.prop_algorithm:
            # Learning update
            current_temp = t.current_temperature

            # --- Guard Cut ---
            algo = t.prop_algorithm

            guard_cut_action = algo.guards.check_guard_cut(
                current_temp=current_temp,
                target_temp=t.target_temperature,
                near_band_above=algo.deadband_mgr.near_band_above_deg,
                in_near_band=algo.in_near_band,
                is_device_active=t.is_device_active,
                hvac_mode=t.vtherm_hvac_mode,
                is_calibration=(algo.phase == SmartPIPhase.CALIBRATION)
            )

            if guard_cut_action == GuardAction.CUT_TRIGGER:
                algo._last_restart_reason = "guard_cut"
                force = True

            # --- Guard Kick ---
            guard_kick_action = algo.guards.check_guard_kick(
                current_temp=current_temp,
                target_temp=t.target_temperature,
                near_band_below=algo.deadband_mgr.near_band_below_deg,
                in_near_band=algo.in_near_band,
                on_percent=algo.calculated_on_percent,
                hvac_mode=t.vtherm_hvac_mode,
                is_calibration=(algo.phase == SmartPIPhase.CALIBRATION)
            )

            if guard_kick_action == GuardAction.KICK_TRIGGER:
                algo._last_restart_reason = "guard_kick"
                force = True

            # Calculate uses current temp, ext temp, etc.
            # If guard_cut is active, calculate() will set on_percent=0.
            cycle_boundary = (
                force
                or t.cycle_scheduler is None
                or not t.cycle_scheduler.is_cycle_running
            )
            t.prop_algorithm.calculate(
                target_temp=t.target_temperature,
                current_temp=t.current_temperature,
                ext_current_temp=t.current_outdoor_temperature,
                slope=t.last_temperature_slope,
                hvac_mode=t.vtherm_hvac_mode,
                power_shedding=t.is_overpowering_detected,
                cycle_boundary=cycle_boundary,
                off_reason=t.hvac_off_reason,
            )

            # Force cycle restart on deadband and near-band transitions so the
            # physically engaged PWM follows the zone change quickly.
            if getattr(algo.deadband_mgr, "deadband_changed", False) is True:
                _LOGGER.debug(
                    "%s - Deadband state transition detected, forcing cycle restart",
                    t,
                )
                algo._last_restart_reason = "deadband_transition"
                force = True

            if getattr(algo.deadband_mgr, "near_band_changed", False) is True:
                _LOGGER.debug(
                    "%s - Near-band state transition detected, forcing cycle restart",
                    t,
                )
                algo._last_restart_reason = "near_band_transition"
                force = True

        # Stop here if we are off
        if t.vtherm_hvac_mode == VThermHvacMode_OFF:
            _LOGGER.debug("%s - End of cycle (HVAC_MODE_OFF)", t)
            setattr(t, "_on_time_sec", 0)
            setattr(t, "_off_time_sec", int(t.cycle_min * 60))
            if t.is_device_active:
                await t.async_underlying_entity_turn_off()
        else:
            on_percent = t.prop_algorithm.on_percent if t.prop_algorithm else 0.0

            # Check if on_percent has changed
            on_percent_changed = abs(on_percent - self._last_on_percent) > 0.001
            self._last_on_percent = on_percent
            was_cycle_running = bool(getattr(t.cycle_scheduler, "is_cycle_running", False))
            force_cycle = (
                force
                or (t.prop_algorithm.phase == SmartPIPhase.CALIBRATION)
                or (t.prop_algorithm.phase == SmartPIPhase.HYSTERESIS and on_percent_changed)
            )

            await t.cycle_scheduler.start_cycle(
                t.vtherm_hvac_mode,
                on_percent,
                force_cycle,
            )
            if (
                algo is not None
                and was_cycle_running
                and not force_cycle
                and bool(getattr(t.cycle_scheduler, "is_valve_mode", False))
            ):
                algo.on_applied_power_updated(
                    on_percent=on_percent,
                    hvac_mode=t.vtherm_hvac_mode,
                )

        if (
            algo is not None
            and timestamp is None
            and not force
            and previous_committed is not None
        ):
            self._should_publish_intermediate = (
                abs(algo.committed_on_percent - previous_committed) > 0.001
            )

        # Save state after cycle to persist learning data
        await self._async_save()

        # --- AutoCalibTrigger: hourly check ---
        if t.prop_algorithm and isinstance(t.prop_algorithm, SmartPI):
            algo = t.prop_algorithm
            now_wall = time.time()

            # Detect calibration completion (CALIBRATING -> IDLE transition)
            currently_calibrating = algo.calibration_state != SmartPICalibrationPhase.IDLE
            if self._prev_is_calibrating and not currently_calibrating:
                # Calibration just ended
                ac_event = algo.autocalib.on_calibration_complete(
                    now_wall=now_wall,
                    algo=algo,
                    result=algo.calibration_mgr.calibration_result,
                )
                if ac_event is not None:
                    t.hass.bus.async_fire(ac_event.event_type, ac_event.payload)
                    _LOGGER.info(
                        "%s - AutoCalib event fired: %s", t.name, ac_event.event_type
                    )
            self._prev_is_calibrating = currently_calibrating

            # Hourly stagnation check
            ac_event = algo.autocalib.check_hourly(
                now_wall=now_wall,
                algo=algo,
                ext_temp=t.current_outdoor_temperature,
                current_temp=t.current_temperature,
            )
            if ac_event is not None:
                t.hass.bus.async_fire(ac_event.event_type, ac_event.payload)
                _LOGGER.info(
                    "%s - AutoCalib event fired: %s", t.name, ac_event.event_type
                )
                if ac_event.should_trigger_calibration:
                    # AutoCalibTrigger decided to start a calibration
                    algo.calibration_mgr.request_calibration(phase=algo.phase)
                    _LOGGER.warning(
                        "%s - AutoCalib: calibration requested by supervisor", t.name
                    )

        # Dispatch an update signal to the diagnostic sensor so it records attributes for this cycle
        async_dispatcher_send(t.hass, f"smartpi_diag_update_{t.unique_id}")

    async def on_state_changed(self, changed: bool):
        """Handle state changes."""
        del changed
        t = self._thermostat
        if t.vtherm_hvac_mode in [VThermHvacMode_HEAT, VThermHvacMode_COOL]:
            # Check if we're resuming from OFF (timer was stopped)
            timer_was_stopped = self._recalc_timer_remove is None

            self._start_recalc_timer()

            # When resuming from OFF state (e.g., window close), reset the cycle start state
            # to prevent using stale learning window data.
            if timer_was_stopped and t.prop_algorithm and isinstance(t.prop_algorithm, SmartPI):
                t.prop_algorithm.reset_cycle_state()
                _LOGGER.debug("%s - SmartPI resumed from OFF: cycle and learning window reset", t.name)
        else:
            self._stop_recalc_timer()
            # Cancel any ongoing calibration when switching to OFF/SLEEP mode
            await self._cancel_calibration_if_active()

    def _start_recalc_timer(self):
        """Start the periodic recalculation timer."""
        t = self._thermostat
        if self._recalc_timer_remove:
            return

        async def _recalc_callback(now):
            _LOGGER.debug("%s - SmartPI periodic calculation trigger", t)
            # Force control_heating but without timestamp to avoid triggering learning
            # The learning should only be triggered by the cycle manager (every cycle_min)
            # automatic recalculation is done inside control_heating
            await t.async_control_heating(timestamp=None)

        self._recalc_timer_remove = async_track_time_interval(
            t.hass,
            _recalc_callback,
            timedelta(seconds=SMARTPI_RECALC_INTERVAL_SEC)
        )
        _LOGGER.debug("%s - SmartPI calc timer started", t)

    def _stop_recalc_timer(self):
        """Stop the periodic recalculation timer."""
        t = self._thermostat
        if self._recalc_timer_remove:
            self._recalc_timer_remove()
            self._recalc_timer_remove = None
            _LOGGER.debug("%s - SmartPI calc timer stopped", t)

    async def _cancel_calibration_if_active(self):
        """Cancel any ongoing calibration (manual or auto) when HVAC is turned off."""
        t = self._thermostat
        algo = t.prop_algorithm
        if not algo or not isinstance(algo, SmartPI):
            return

        if not algo.calibration_mgr.is_calibrating:
            return

        _LOGGER.info("%s - HVAC OFF: canceling ongoing calibration", t.name)

        # Reset the calibration manager
        algo.calibration_mgr.reset()

        # Notify autocalib trigger about the cancellation
        now_wall = time.time()
        event = algo.autocalib.on_calibration_complete(now_wall, algo, SmartPICalibrationResult.CANCELLED)

        # Fire event if autocalib returns one
        if event:
            t.hass.bus.fire(EventType.SMART_PI_EVENT.value, {"entity_id": t.entity_id, "type": event.event_type, "data": event.data or {}})

        # Reset tracking state
        self._prev_is_calibrating = False

        # Update attributes and save state
        self.update_attributes()
        t.async_write_ha_state()
        await self._async_save()

    def update_attributes(self):
        """Keep climate attributes generic for SmartPI."""

    async def service_reset_smart_pi_learning(self):
        """Reset learning data."""
        t = self._thermostat
        if t.prop_algorithm and isinstance(t.prop_algorithm, SmartPI):
            t.prop_algorithm.reset_learning()
            t.hass.bus.fire(EventType.SMART_PI_EVENT.value, {
                 "entity_id": t.entity_id,
                 "type": "learning_reset"
             })
            write_event_log(_LOGGER, t, "SmartPI learning reset")
            await self.control_heating(force=True)
            self.update_attributes()
            t.async_write_ha_state()
            await self._async_save()

    async def service_force_smartpi_calibration(self):
        """Force calibration."""
        t = self._thermostat
        if t.prop_algorithm and isinstance(t.prop_algorithm, SmartPI):
            event = t.prop_algorithm.force_calibration()
            t.hass.bus.fire(EventType.SMART_PI_EVENT.value, {
                 "entity_id": t.entity_id,
                 "type": "force_calibration"
             })
            if event is not None:
                t.hass.bus.fire(event.event_type, event.payload)

            write_event_log(_LOGGER, t, "SmartPI forced calibration triggered")
            # Force immediate recalculation to update state
            await self.control_heating(force=True)
            self.update_attributes()
            t.async_write_ha_state()
            await self._async_save()

            _LOGGER.debug("%s - AutoCalib: manual calibration triggered, will check exit on completion", t.name)

    async def service_reset_smartpi_integral(self):
        """Reset the integral accumulator to zero."""
        t = self._thermostat
        if t.prop_algorithm and isinstance(t.prop_algorithm, SmartPI):
            t.prop_algorithm.ctl.integral = 0.0
            t.prop_algorithm.ctl.clear_integral_hold()
            t.hass.bus.fire(EventType.SMART_PI_EVENT.value, {
                "entity_id": t.entity_id,
                "type": "integral_reset"
            })
            write_event_log(_LOGGER, t, "SmartPI integral reset to zero")
            self.update_attributes()
            t.async_write_ha_state()
            await self._async_save()
