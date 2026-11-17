# pylint: disable=line-too-long, abstract-method
"""SmartPI algorithm handler for the plugin runtime."""

import logging
import time
from typing import TYPE_CHECKING
from homeassistant.util import slugify
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.helpers import entity_registry as er
from homeassistant.core import Event, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.dispatcher import async_dispatcher_send
from datetime import timedelta

from .algo import SmartPI
from .state_store import SmartPIStateStore
from .smartpi.const import (
    SMARTPI_RECALC_INTERVAL_SEC,
    SmartPIPhase,
    SmartPICalibrationResult,
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
    CONF_SMART_PI_ALLOW_PWM_CYCLE_FORCE,
    CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION,
    CONF_SMART_PI_MIN_VALVE,
    CONF_SMART_PI_KNEE_DEMAND,
    CONF_SMART_PI_KNEE_VALVE,
    CONF_SMART_PI_MAX_VALVE,
    DEFAULT_OPTIONS,
    DIAGNOSTIC_SENSOR_UNIQUE_ID_PREFIX,
    DOMAIN,
    diag_update_signal,
    EventType,
    SIGNAL_SMARTPI_TARGET_UPDATED,
)
from .hvac_mode import VThermHvacMode_OFF, VThermHvacMode_HEAT, VThermHvacMode_COOL
from .commons import write_event_log
from .valve_config_check import apply_valve_config_check, clear_valve_config_issues
from .boiler_call import BOILER_BLINK_S, BOILER_PAUSE_CONFIRM_S, BOILER_VALVE_HOLD_S, central_boiler_entity_id, central_boiler_on
from .smartpi.valve_curve import ValveCurveParams

if TYPE_CHECKING:
    from vtherm_api.interfaces import InterfaceThermostatRuntime

_LOGGER = logging.getLogger(__name__)

class SmartPIHandler:
    """Handler for SmartPI-specific logic."""

    def __init__(self, thermostat: "InterfaceThermostatRuntime"):
        """Initialize handler with parent thermostat reference."""
        self._thermostat = thermostat
        self._store: SmartPIStateStore | None = None
        self._recalc_timer_remove = None
        # Remover of the applied-command receipt callback; None without receipts
        self._receipt_remove = None
        # State for learning
        self._last_temp = None
        self._last_ext_temp = None
        self._last_time = None
        self._last_on_percent = 0.0
        self._should_publish_intermediate: bool = True
        self._last_publish_signature = None
        self._pending_publish_signature = None
        self._allow_pwm_cycle_force: bool = False
        # Since when SmartPI has called with the central boiler off, without a break (monotonic
        # time), None while it does not.
        self._boiler_off_since: float | None = None
        # The boiler's state as it heats the room (``_boiler_heats_the_room``) as last given to
        # ``_follow_the_boiler`` (None: not counted, or unreadable), since when (monotonic time), and
        # whether the check scheduled for it found it held.
        self._boiler_read: bool | None = None
        self._boiler_read_since: float = 0.0
        self._boiler_held: bool = False
        # When the boiler last went off, from on, for the dead-time estimator (``SmartPI.boiler_on``),
        # while the room's heat flowed; None otherwise.
        self._boiler_seen_off_at: float | None = None
        # The check scheduled once a boiler's start has held ``BOILER_BLINK_S``.
        self._boiler_check_remove = None
        # A thermostat on a relay (``_drives_a_relay``): since when its device is on (monotonic
        # time, None while off) and when it last turned off, whether the check scheduled then found
        # it on ``BOILER_BLINK_S`` later, the boiler's state as it heats the room
        # (``_boiler_heats_the_room``), and the listener of the thermostat's own state, which
        # Versatile Thermostat writes at each change of the relay.
        self._device_on_since: float | None = None
        self._device_off_at: float | None = None
        self._device_held: bool = False
        self._device_check_remove = None
        self._relay_heat: bool | None = None
        self._thermostat_listener_remove = None
        # A relay: whether the room's heat flows (the relay and the boiler both on), and when it
        # last stopped flowing; the check scheduled ``BOILER_BLINK_S`` into a cycle's on part.
        self._heat_flowing: bool = False
        self._heat_flowed_until: float | None = None
        self._relay_heat_ended_at: float | None = None
        self._plan_check_remove = None
        # A valve (any thermostat not on a relay), or a relay whose heat flowed: when the boiler
        # went off while the room's heat flowed (None otherwise), held as heat for
        # ``BOILER_VALVE_HOLD_S``, the check scheduled then, and the moment of a cut so confirmed,
        # from which the pause counts.
        self._held_boiler_off_at: float | None = None
        self._hold_check_remove = None
        self._held_cut_at: float | None = None
        self._held_stopped_at: float | None = None
        # The check scheduled ``BOILER_PAUSE_CONFIRM_S`` after ``_boiler_off_since``, and the
        # ``_boiler_off_since`` it found due, so that the pause starts then, not at the next
        # calculation.
        self._pause_check_remove = None
        self._pause_check_since: float | None = None
        self._pause_due_since: float | None = None
        # The listener of Versatile Thermostat's central boiler state (``_listen_to_the_boiler``).
        self._boiler_listener_remove = None
        self._valve_linearization_configured: bool = False
        self._valve_curve_params: ValveCurveParams | None = None

    def init_algorithm(self):
        """Initialize SmartPI algorithm."""
        t = self._thermostat
        entry, _ = self._resolve_effective_config()

        # Initialize storage with slugified name to allow retrieval if re-created
        safe_name = slugify(t.name)
        self._store = SmartPIStateStore(t.hass, safe_name)

        # Use the thermostat's cycle_min property directly for consistency
        # (base_thermostat reads CONF_CYCLE_MIN in __init__)
        cycle_min = t.cycle_min
        minimal_activation_delay = entry[CONF_MINIMAL_ACTIVATION_DELAY]
        minimal_deactivation_delay = entry[CONF_MINIMAL_DEACTIVATION_DELAY]
        max_on_percent = getattr(t, "max_on_percent", None)

        # Update thermostat attributes directly (like TPIHandler)
        t.minimal_activation_delay = minimal_activation_delay
        t.minimal_deactivation_delay = minimal_deactivation_delay
        self._synchronize_scheduler_timing(
            minimal_activation_delay,
            minimal_deactivation_delay,
        )

        # SmartPI specific
        deadband = entry[CONF_SMART_PI_DEADBAND]
        use_setpoint_filter = entry[CONF_SMART_PI_USE_SETPOINT_FILTER]
        use_ff3 = entry[CONF_SMART_PI_USE_FF3]
        hyst_on = entry[CONF_SMART_PI_HYSTERESIS_ON]
        hyst_off = entry[CONF_SMART_PI_HYSTERESIS_OFF]
        release_tau_factor = entry[CONF_SMART_PI_RELEASE_TAU_FACTOR]
        deadband_allow_p = entry[CONF_SMART_PI_DEADBAND_ALLOW_P]
        self._allow_pwm_cycle_force = entry[CONF_SMART_PI_ALLOW_PWM_CYCLE_FORCE]
        self._valve_linearization_configured = entry[CONF_SMART_PI_ENABLE_VALVE_LINEARIZATION]
        valve_mode_enabled = bool(getattr(t.cycle_scheduler, "is_valve_mode", False))
        valve_linearization_enabled = (
            self._valve_linearization_configured
            and valve_mode_enabled
        )
        try:
            self._valve_curve_params = ValveCurveParams(
                min_valve=float(entry[CONF_SMART_PI_MIN_VALVE]),
                knee_demand=float(entry[CONF_SMART_PI_KNEE_DEMAND]),
                knee_valve=float(entry[CONF_SMART_PI_KNEE_VALVE]),
                max_valve=float(entry[CONF_SMART_PI_MAX_VALVE]),
            )
        except (TypeError, ValueError) as err:
            _LOGGER.error("%s - Invalid SmartPI valve curve parameters: %s", t, err)
            self._valve_linearization_configured = False
            valve_linearization_enabled = False
            self._valve_curve_params = None
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
            enable_valve_linearization=valve_linearization_enabled,
            valve_curve_params=self._valve_curve_params,
        )
        t.prop_algorithm.configure_valve_linearization(
            valve_linearization_enabled,
            self._valve_curve_params,
            valve_mode=valve_mode_enabled,
        )

        _LOGGER.info("%s - SmartPI Algorithm initialized", t)
        async_dispatcher_send(t.hass, SIGNAL_SMARTPI_TARGET_UPDATED, t.unique_id)

    def _resolve_effective_config(self) -> tuple[dict, object | None]:
        """Return the merged SmartPI configuration for the thermostat."""
        t = self._thermostat
        config = dict(DEFAULT_OPTIONS)
        config.update(t.entry_infos or {})

        plugin_entries = t.hass.config_entries.async_entries(DOMAIN, include_disabled=False)
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

        return config, entry_to_apply

    async def async_added_to_hass(self):
        """Load persistent data."""
        t = self._thermostat
        if self._store:
            try:
                data = await self._store.async_load()
                if data and t.prop_algorithm:
                    t.prop_algorithm.load_state(data)
                    _LOGGER.debug("%s - SmartPI state loaded", t)
            except Exception as e:
                _LOGGER.error("%s - Failed to load SmartPI state: %s", t, e)

    async def async_startup(self):
        """Startup actions."""
        apply_valve_config_check(self._thermostat, self._valve_linearization_configured)
        # Check if we need to start the periodic recalculation timer
        await self.on_state_changed(True)

    async def _async_save(self):
        """Write the SmartPI state to storage immediately."""
        t = self._thermostat
        if self._store and t.prop_algorithm:
            try:
                await self._store.save_now(t.prop_algorithm)
                _LOGGER.debug("%s - SmartPI state saved", t)
            except Exception as e:
                _LOGGER.error("%s - Failed to save SmartPI state: %s", t, e)

    def _fire_autocalib_event(self, event):
        """Fire an AutoCalib event: `entity_id` is the climate entity, `name` the thermostat name."""
        self._thermostat.hass.bus.async_fire(event.event_type, {**event.payload, "entity_id": self._thermostat.entity_id})
        _LOGGER.info("%s - AutoCalib event fired: %s", self._thermostat.name, event.event_type)

    def remove(self):
        """Cleanup and save state on removal."""
        t = self._thermostat
        if self._store and t.prop_algorithm:
            try:
                # Snapshot now; the write itself cannot be awaited from this sync method.
                t.hass.async_create_task(self._store.save_now(t.prop_algorithm))
            except Exception as e:
                _LOGGER.error("%s - Failed to save SmartPI state: %s", t, e)

        self._stop_recalc_timer()
        self._remove_receipt_callback()
        if self._boiler_listener_remove is not None:
            self._boiler_listener_remove()
            self._boiler_listener_remove = None
        if self._thermostat_listener_remove is not None:
            self._thermostat_listener_remove()
            self._thermostat_listener_remove = None
        self._cancel_the_boiler_check()
        self._cancel_the_device_check()
        self._cancel_the_plan_check()
        self._cancel_the_hold_check()
        self._cancel_the_pause_check()
        clear_valve_config_issues(t)

    def _remove_receipt_callback(self) -> None:
        """Unregister the applied-command receipt callback, if any."""
        if self._receipt_remove is not None:
            self._receipt_remove()
            self._receipt_remove = None

    def on_scheduler_ready(self, scheduler) -> None:
        """Register SmartPI learning callbacks on the cycle scheduler."""
        algo = self._thermostat.prop_algorithm
        if algo:
            self._synchronize_scheduler_timing(
                self._thermostat.minimal_activation_delay,
                self._thermostat.minimal_deactivation_delay,
                scheduler=scheduler,
            )
            algo.configure_valve_linearization(
                self._valve_linearization_configured
                and bool(getattr(scheduler, "is_valve_mode", False)),
                self._valve_curve_params,
                valve_mode=bool(getattr(scheduler, "is_valve_mode", False)),
            )
            self._remove_receipt_callback()
            # Capability check before any request_id reaches start_cycle (older VT raise TypeError)
            if hasattr(scheduler, "register_command_receipt_callback"):
                self._receipt_remove = scheduler.register_command_receipt_callback(
                    algo.on_command_receipt
                )
            else:
                _LOGGER.warning(
                    "%s - Versatile Thermostat does not report applied commands: "
                    "SmartPI runs without command ownership (no bumpless transfer, "
                    "no periodic feed-forward trim)",
                    self._thermostat,
                )
                scheduler.register_cycle_start_callback(self._on_legacy_cycle_started)
            algo.receipts_supported = self._receipt_remove is not None
            scheduler.register_cycle_end_callback(algo.on_cycle_completed)

    def _synchronize_scheduler_timing(
        self,
        minimal_activation_delay: int,
        minimal_deactivation_delay: int,
        *,
        scheduler=None,
    ) -> None:
        """Keep the scheduler timing inputs aligned with SmartPI configuration."""
        scheduler = (
            scheduler if scheduler is not None else self._thermostat.cycle_scheduler
        )
        if scheduler is None:
            return
        scheduler.min_activation_delay = int(minimal_activation_delay)
        scheduler.min_deactivation_delay = int(minimal_deactivation_delay)

    @staticmethod
    def _published_valve_power(thermostat) -> float | None:
        """Return the current valve opening published by the thermostat."""
        valve_open_percent = getattr(thermostat, "valve_open_percent", None)
        if valve_open_percent is None:
            return None
        try:
            return max(0.0, min(1.0, float(valve_open_percent) / 100.0))
        except (TypeError, ValueError):
            return None

    async def _on_legacy_cycle_started(
        self,
        on_time_sec: float,
        off_time_sec: float,
        on_percent: float,
        hvac_mode: str,
    ) -> None:
        """Forward a cycle start of a scheduler without receipts."""
        algo = self._thermostat.prop_algorithm
        if isinstance(algo, SmartPI):
            await algo.on_cycle_started(
                on_time_sec,
                off_time_sec,
                on_percent,
                hvac_mode,
                physical_on_percent=self._published_valve_power(self._thermostat),
            )

    def should_publish_intermediate(self) -> bool:
        """Return True when VT may publish the current control iteration."""
        if self._should_publish_intermediate:
            self._last_publish_signature = self._pending_publish_signature
        return self._should_publish_intermediate

    def _build_publish_signature(self):
        """Return the thermostat inputs that should refresh the climate state."""
        t = self._thermostat
        return (
            t.current_temperature,
            t.current_outdoor_temperature,
            t.target_temperature,
            t.last_temperature_slope,
            t.vtherm_hvac_mode,
            getattr(t, "last_temperature_measure", None),
            getattr(t, "last_ext_temperature_measure", None),
        )

    async def control_heating(self, timestamp=None, force=False):
        """Control heating using SmartPI."""
        t = self._thermostat
        from .smartpi.guards import GuardAction

        algo = t.prop_algorithm if isinstance(t.prop_algorithm, SmartPI) else None
        previous_committed = algo.committed_on_percent if algo is not None else None
        self._pending_publish_signature = self._build_publish_signature()
        input_changed = self._pending_publish_signature != self._last_publish_signature
        self._should_publish_intermediate = (
            algo is None or timestamp is not None or force or input_changed
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
                is_device_active=t.is_device_active,
                hvac_mode=t.vtherm_hvac_mode,
                is_calibration=(algo.phase == SmartPIPhase.CALIBRATION),
                is_bootstrap=(algo.phase == SmartPIPhase.HYSTERESIS),
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
                is_calibration=(algo.phase == SmartPIPhase.CALIBRATION),
                is_bootstrap=(algo.phase == SmartPIPhase.HYSTERESIS),
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
            safety = getattr(getattr(t, "safety_manager", None), "is_safety_detected", False)
            algo.vt_safety_override = safety is True
            self._listen_to_the_boiler()
            self._update_boiler_learning_pause(algo)
            t.prop_algorithm.calculate(
                target_temp=t.target_temperature,
                current_temp=t.current_temperature,
                ext_current_temp=t.current_outdoor_temperature,
                slope=t.last_temperature_slope,
                hvac_mode=t.vtherm_hvac_mode,
                power_shedding=t.is_overpowering_detected,
                cycle_boundary=cycle_boundary,
                off_reason=t.hvac_off_reason,
                temperature_measure_id=getattr(t, "last_temperature_measure", None),
            )

            # Force cycle restart on deadband and near-band transitions so the
            # physically engaged PWM follows the zone change quickly.
            if (
                self._allow_pwm_cycle_force
                and getattr(algo.deadband_mgr, "deadband_changed", False) is True
            ):
                _LOGGER.debug(
                    "%s - Deadband state transition detected, forcing cycle restart",
                    t,
                )
                algo._last_restart_reason = "deadband_transition"
                force = True

            if (
                self._allow_pwm_cycle_force
                and getattr(algo.deadband_mgr, "near_band_changed", False) is True
            ):
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
            elif t.cycle_scheduler and t.cycle_scheduler.is_cycle_running:
                # The master scheduler may still hold a pending heat cycle while
                # the physical device is already in its OFF phase.
                await t.cycle_scheduler.cancel_cycle()
        elif (on_percent := t.on_percent) is None:
            _LOGGER.info("%s - on_percent unknown (no temperature), keeping the switch state", t)
        else:
            # Check if on_percent has changed
            on_percent_changed = abs(on_percent - self._last_on_percent) > 0.001
            self._last_on_percent = on_percent
            was_cycle_running = bool(getattr(t.cycle_scheduler, "is_cycle_running", False))
            force_cycle = (
                force
                or (t.prop_algorithm.phase == SmartPIPhase.CALIBRATION)
                or (t.prop_algorithm.phase == SmartPIPhase.HYSTERESIS and on_percent_changed)
            )

            # An untagged request (no request_id) is never owned.
            request_kwargs = {}
            if algo is not None:
                if abs(on_percent - algo.on_percent) > 1e-6:
                    # VT override (e.g. safety default): not owned by the SmartPI request.
                    algo.reset_command_ownership("runtime_power_override")
                elif self._receipt_remove is not None:
                    request_kwargs = {"request_id": algo.stage_command(t.vtherm_hvac_mode)}

            await t.cycle_scheduler.start_cycle(
                t.vtherm_hvac_mode,
                on_percent,
                force_cycle,
                **request_kwargs,
            )
            if (
                algo is not None
                and self._receipt_remove is None
                and was_cycle_running
                and not force_cycle
                and getattr(t.cycle_scheduler, "is_valve_mode", False)
            ):
                # Without receipts the applied valve position is read back.
                algo.on_applied_power_updated(
                    self._published_valve_power(t), t.vtherm_hvac_mode
                )

        if (
            algo is not None
            and timestamp is None
            and not force
            and previous_committed is not None
        ):
            committed_changed = abs(algo.committed_on_percent - previous_committed) > 0.001
            self._should_publish_intermediate = self._should_publish_intermediate or committed_changed

        # Persist learning data through a debounced write
        if self._store and t.prop_algorithm:
            self._store.schedule_save(t.prop_algorithm)

        # --- AutoCalibTrigger: hourly check ---
        if t.prop_algorithm and isinstance(t.prop_algorithm, SmartPI):
            algo = t.prop_algorithm
            now_wall = time.time()

            # A calibration run ended since the previous pass
            completed_result = algo.calibration_mgr.take_completed_result()
            if completed_result is not None:
                ac_event = algo.autocalib.on_calibration_complete(
                    now_wall=now_wall,
                    algo=algo,
                    result=completed_result,
                )
                if ac_event is not None:
                    self._fire_autocalib_event(ac_event)

            # Hourly stagnation check
            ac_event = algo.autocalib.check_hourly(
                now_wall=now_wall,
                algo=algo,
                ext_temp=t.current_outdoor_temperature,
                current_temp=t.current_temperature,
            )
            if ac_event is not None:
                self._fire_autocalib_event(ac_event)
                if ac_event.should_trigger_calibration:
                    # AutoCalibTrigger decided to start a calibration
                    algo.calibration_mgr.request_calibration(phase=algo.phase)
                    _LOGGER.warning(
                        "%s - AutoCalib: calibration requested by supervisor", t.name
                    )

        if self._should_publish_intermediate:
            async_dispatcher_send(t.hass, diag_update_signal(t.unique_id))

    def _update_boiler_learning_pause(self, algo: SmartPI) -> bool:
        """Give SmartPI the central boiler's state for its dead-time estimator, and pause SmartPI's
        learning while it calls for heat and the boiler is off, or resume it; whether the room's
        heat changed with the boiler or the pause changed.

        The pause starts once this has held without a break for ``BOILER_PAUSE_CONFIRM_S``:
        when a thermostat's own start turns the boiler on, Versatile Thermostat does so only
        once the thermostat's device reports it. Checks in that time must not pause. A check is
        scheduled for that moment (``_schedule_the_pause_check``), so the pause starts then, not at
        the next calculation. Learning resumes once the boiler's start counts for the dead-time
        estimator, after ``BOILER_BLINK_S`` (``_follow_the_boiler``): a shorter run of the boiler
        gave the room no heat, so the time without heat goes on, and so does the pause. A change
        is published at once. For a thermostat on a relay, the boiler's state is the one it heats
        the room with (``_boiler_heats_the_room``).
        """
        now = time.monotonic()
        t = self._thermostat
        counted = getattr(t, "is_used_by_central_boiler", False) is True
        calls = self._calls_for_boiler_heat(algo)
        boiler_on = self._boiler_heats_the_room(algo, central_boiler_on(t.hass) if counted else None, calls, now)
        heat_changed = self._follow_the_boiler(algo, boiler_on, now)
        cut_at, self._held_cut_at = self._held_cut_at, None
        if calls and boiler_on is False:
            if self._boiler_off_since is None:
                # A cut confirmed after the hold counts from the boiler's stop, as before it.
                self._boiler_off_since = cut_at if cut_at is not None else now
        elif not calls or boiler_on is None or algo.boiler_on is True:
            self._boiler_off_since = None
        # Otherwise the boiler is back for less than BOILER_BLINK_S, a start the estimator has not
        # seen yet: the time without heat goes on, and a pause with it, but none starts now.
        since = self._boiler_off_since
        paused = since is not None and (
            algo.boiler_learning_paused
            or (boiler_on is False and (now - since >= BOILER_PAUSE_CONFIRM_S or self._pause_due_since == since))
        )
        if since is not None and boiler_on is False and not paused:
            self._schedule_the_pause_check(since, now)
        else:
            self._cancel_the_pause_check()
        if paused == algo.boiler_learning_paused:
            return heat_changed
        algo.set_boiler_learning_paused(paused)
        self._should_publish_intermediate = True
        return True

    def _drives_a_relay(self) -> bool:
        """Whether SmartPI drives the thermostat's device in on/off cycles (a relay): Versatile
        Thermostat's cycle scheduler, not in valve mode. A thermostat without a cycle scheduler
        (a climate underlying) is not one."""
        scheduler = getattr(self._thermostat, "cycle_scheduler", None)
        return scheduler is not None and getattr(scheduler, "is_valve_mode", None) is False

    def _boiler_heats_the_room(self, algo: SmartPI, boiler_on: bool | None, calls: bool, now: float) -> bool | None:
        """The boiler's state as it heats the room: the state read, a short stop held as heat while
        the room's heat flows (``_valve_heat``), and for a thermostat on a relay while SmartPI
        calls, the relay's rules below.

        Versatile Thermostat counts a relay for its boiler only while it is on, so with the relay
        alone the boiler goes off in each off part of SmartPI's cycle, and back as the relay
        turns on, milliseconds later. Neither is a change of the room's heat: the off part is the
        cycle's own, as SmartPI's command averages it. So, with the relay on, the boiler cuts the
        heat once the relay has been on for ``BOILER_BLINK_S`` with the boiler off (the other two
        ways to a cut: a stop held too long, and a relay off where its cycle has it on, below),
        and gives it back only while both are on; in between, the last state holds: in the relay's own off part, and
        while the boiler may still follow a relay just on. A heat cut stays cut while the boiler
        comes back in the relay's off part (for another room): the heat starts with the relay.

        The off part is the relay's own only where SmartPI's applied cycle has it off
        (``SmartPI.relay_plan``, from Versatile Thermostat's command receipts). A relay off where
        the cycle has it on, ``BOILER_BLINK_S`` into its on part (unavailable, or a pulse
        Versatile Thermostat left out), follows the boiler as read. A cut found after the room's
        heat last flowed (both on) is dated from then (``SmartPI.set_boiler_state``, ``ended_at``).

        The boiler going off while the room's heat flows, the relay on (another room's off part,
        with a threshold of devices), is held as heat for ``BOILER_VALVE_HOLD_S``, as for a valve:
        the water in the radiator goes on heating (``_relay_stop_held``). As for a valve, only once
        the dead-time estimator saw the heat, the boiler having run ``BOILER_BLINK_S``: a shorter
        run of the boiler gave the room no heat.
        """
        device_on = self._track_the_device(now)
        flowed = self._heat_flowing
        self._follow_the_heat(boiler_on is True and device_on, now)
        if boiler_on is None or not calls or not self._drives_a_relay():
            self._relay_heat = boiler_on
            self._cancel_the_plan_check()
            return self._valve_heat(algo, boiler_on, calls, now)
        before = self._relay_heat
        seen = flowed and algo.boiler_on is True  # a start the estimator saw, as for a valve
        held = self._relay_stop_held(boiler_on, device_on and seen, now)  # ends at the boiler's return
        if boiler_on and device_on:
            self._relay_heat = True
        elif held:
            pass  # held as heat, or the hold's end: a cut dated from the boiler's stop
        elif not boiler_on and device_on and (self._device_held or now - (self._device_on_since or now) >= BOILER_BLINK_S):
            self._relay_heat = False
        elif (not device_on and self._relay_off_against_its_cycle(algo, now)) or self._relay_heat is None:
            self._relay_heat = boiler_on
        if (
            before is True
            and self._relay_heat is False
            and self._held_stopped_at is None
            and self._heat_flowed_until is not None
            and self._heat_flowed_until < now - 1.0
        ):
            self._relay_heat_ended_at = self._heat_flowed_until
        return self._relay_heat

    def _relay_stop_held(self, boiler_on: bool, stopped_flowing: bool, now: float) -> bool:
        """Whether the boiler's state for a relay that calls is settled by the hold: the boiler
        gone off while the room's heat flowed (``stopped_flowing``: the relay on, the heat flowing
        until now, from a start the dead-time estimator saw) is held as heat for
        ``BOILER_VALVE_HOLD_S``, through the relay's own off part too; still off after it, it is a
        cut dated from the boiler's stop, and the pause counts from it (``_held_stopped_at``,
        ``_held_cut_at``). A boiler back within the hold is no cut and no start. A relay turned on
        with the boiler off is not held: the boiler did not start for it; nor is a run of the boiler
        shorter than ``BOILER_BLINK_S``."""
        if boiler_on is not False:
            self._held_boiler_off_at = None
            self._cancel_the_hold_check()
            return False
        if self._held_boiler_off_at is None:
            if not stopped_flowing:
                return False
            self._held_boiler_off_at = now
            self._schedule_the_hold_check()
        if now - self._held_boiler_off_at < BOILER_VALVE_HOLD_S:
            self._relay_heat = True
            return True
        self._held_stopped_at = self._held_cut_at = self._held_boiler_off_at
        self._held_boiler_off_at = None
        self._cancel_the_hold_check()
        self._relay_heat = False
        return True

    def _valve_heat(self, algo: SmartPI, boiler_on: bool | None, calls: bool, now: float) -> bool | None:
        """The boiler's state as it heats a room not on a relay (a valve): the state read, but a
        boiler that goes off while the room's heat flows is held as heat for
        ``BOILER_VALVE_HOLD_S``.

        Versatile Thermostat's boiler can go off for seconds while the room calls: with a
        threshold of devices, in each off part of another room's relay. The water in the emitter
        goes on heating then. Taken as a stop, each would end the dead-time estimator's heating
        detection and restart its minimum time without heat, so a room whose boiler goes off in
        every such cycle would never measure its heating dead time. A boiler
        back within the hold is no stop and no start. A boiler still off after it is a cut dated
        from the boiler's stop (``SmartPI.set_boiler_state``, ``stopped_at``), and the pause counts
        from that moment. Only a stop of heat that flows is held: a call with the boiler off
        waits for it, as any start."""
        if boiler_on is False and not calls and self._held_boiler_off_at is not None and algo.boiler_on is True:
            # The room stopped calling during the hold: its heat had stopped with the boiler.
            self._held_stopped_at = self._held_boiler_off_at
        if boiler_on is not False or not calls or algo.boiler_on is not True:
            self._held_boiler_off_at = None
            self._cancel_the_hold_check()
            return boiler_on
        if self._held_boiler_off_at is None:
            self._held_boiler_off_at = now
            self._schedule_the_hold_check()
        if now - self._held_boiler_off_at < BOILER_VALVE_HOLD_S:
            return True
        self._held_stopped_at = self._held_cut_at = self._held_boiler_off_at
        self._held_boiler_off_at = None
        self._cancel_the_hold_check()
        return False

    def _schedule_the_hold_check(self) -> None:
        """Check the boiler again once a hold has run (a valve's, or a relay's whose heat flowed),
        while its changes are followed."""
        self._cancel_the_hold_check()
        if self._boiler_listener_remove is None:
            return
        since = self._held_boiler_off_at

        @callback
        def _due(_now) -> None:
            if self._held_boiler_off_at != since:
                return  # the boiler came back, or the room stopped calling
            self._hold_check_remove = None
            self._on_boiler_change(None)

        self._hold_check_remove = async_call_later(self._thermostat.hass, BOILER_VALVE_HOLD_S, _due)

    def _cancel_the_hold_check(self) -> None:
        if self._hold_check_remove is not None:
            self._hold_check_remove()
            self._hold_check_remove = None

    def _schedule_the_pause_check(self, since: float, now: float) -> None:
        """Check the pause once SmartPI has called with the boiler off for
        ``BOILER_PAUSE_CONFIRM_S`` from ``since``, while the boiler's changes are followed: the
        pause then starts at that moment, not at the next calculation, up to a period later."""
        if self._pause_check_remove is not None and self._pause_check_since == since:
            return
        self._cancel_the_pause_check()
        if self._boiler_listener_remove is None:
            return

        @callback
        def _due(_now) -> None:
            if self._boiler_off_since != since:
                return  # the room's heat came back, or SmartPI stopped calling
            self._pause_check_remove = None
            self._pause_due_since = since
            self._on_boiler_change(None)

        self._pause_check_since = since
        self._pause_check_remove = async_call_later(
            self._thermostat.hass, max(0.0, since + BOILER_PAUSE_CONFIRM_S - now), _due
        )

    def _cancel_the_pause_check(self) -> None:
        if self._pause_check_remove is not None:
            self._pause_check_remove()
            self._pause_check_remove = None

    def _follow_the_heat(self, flows: bool, now: float) -> None:
        """Whether the room's heat flows (a relay on with the boiler on), and when it last did."""
        if flows:
            self._heat_flowing = True
        elif self._heat_flowing:
            self._heat_flowing, self._heat_flowed_until = False, now

    def _relay_off_against_its_cycle(self, algo: SmartPI, now: float) -> bool:
        """Whether the relay is off where SmartPI's applied cycle has it on, ``BOILER_BLINK_S``
        into its on part and before its end; at the on part's start, the check is scheduled for
        then (Versatile Thermostat reports a cycle's start just before it switches the relay).

        Versatile Thermostat keeps a relay off until it has been off for its minimal deactivation
        delay (``minimal_deactivation_delay``, 0 by default), and starts a cycle's on part later
        when the relay went off less than that before (a cycle started again soon after the
        relay's off part): the on part then counts from the end of that protection, not from the
        cycle's start. A relay held off by its own protection is not one that failed to switch."""
        plan = algo.relay_plan()
        if plan is None:
            return False
        start, on_s = plan
        protection = getattr(self._thermostat, "minimal_deactivation_delay", 0)
        on_from = start
        if isinstance(protection, (int, float)) and protection > 0 and self._device_off_at is not None:
            on_from = max(start, self._device_off_at + float(protection))
        if now < on_from + BOILER_BLINK_S < start + on_s - BOILER_BLINK_S:
            self._schedule_the_plan_check(on_from + BOILER_BLINK_S - now)
            return False
        return on_from + BOILER_BLINK_S <= now < start + on_s - BOILER_BLINK_S

    def _schedule_the_plan_check(self, delay: float) -> None:
        """Check the relay again once its cycle's on part has run ``BOILER_BLINK_S``, while the
        thermostat's changes are followed."""
        self._cancel_the_plan_check()
        if self._thermostat_listener_remove is None:
            return

        @callback
        def _due(_now) -> None:
            self._plan_check_remove = None
            self._on_boiler_change(None)

        self._plan_check_remove = async_call_later(self._thermostat.hass, max(0.0, delay), _due)

    def _cancel_the_plan_check(self) -> None:
        if self._plan_check_remove is not None:
            self._plan_check_remove()
            self._plan_check_remove = None

    def _track_the_device(self, now: float) -> bool:
        """Whether the thermostat's device is on (``is_device_active``), and since when; a check is
        scheduled ``BOILER_BLINK_S`` after it turned on."""
        on = getattr(self._thermostat, "is_device_active", False) is True
        if on and self._device_on_since is None:
            self._device_on_since, self._device_held = now, False
            self._schedule_the_device_check()
        elif not on and self._device_on_since is not None:
            self._device_on_since, self._device_held, self._device_off_at = None, False, now
            self._cancel_the_device_check()
        return on

    def _schedule_the_device_check(self) -> None:
        """Check the boiler again once the relay has been on ``BOILER_BLINK_S``, while the
        thermostat's changes are followed (``_listen_to_the_boiler``)."""
        self._cancel_the_device_check()
        if self._thermostat_listener_remove is None or not self._drives_a_relay():
            return
        since = self._device_on_since

        @callback
        def _held(_now) -> None:
            if self._device_on_since is None or self._device_on_since != since:
                return  # the relay changed since
            self._device_check_remove = None
            self._device_held = True
            self._on_boiler_change(None)

        self._device_check_remove = async_call_later(self._thermostat.hass, BOILER_BLINK_S, _held)

    def _cancel_the_device_check(self) -> None:
        if self._device_check_remove is not None:
            self._device_check_remove()
            self._device_check_remove = None

    def _follow_the_boiler(self, algo: SmartPI, boiler_on: bool | None, now: float) -> bool:
        """Give SmartPI the boiler's state its dead-time estimator follows
        (``SmartPI.set_boiler_state``); whether the room's heat stopped, started or came back.

        Versatile Thermostat can switch its boiler off and on again within milliseconds, a blink.
        A stop counts at once. A start counts once the boiler has run ``BOILER_BLINK_S`` without a
        break, checked then while the boiler's changes are followed, or at the next calculation;
        it is fed from the boiler's start. A boiler back within ``BOILER_BLINK_S`` of a stop the
        estimator saw (on, then off) while the room's heat flowed blinked: the room's heat did not
        stop. For a room whose heat did not flow, its return is a start. A first reading off,
        after a restart or once the state can be read, is no such stop. A state that becomes
        known or unknown counts at once.
        """
        if boiler_on != self._boiler_read:
            self._boiler_read, self._boiler_read_since, self._boiler_held = boiler_on, now, False
            self._schedule_the_boiler_check(boiler_on)
        followed = algo.boiler_on
        seen = boiler_on
        if (
            boiler_on is True
            and followed is False
            and not self._boiler_held
            and now - self._boiler_read_since < BOILER_BLINK_S
        ):
            seen = followed
        if seen == followed:
            return False
        blink = (
            seen is True
            and followed is False
            and self._boiler_seen_off_at is not None
            and self._boiler_read_since - self._boiler_seen_off_at < BOILER_BLINK_S
        )
        ended_at, self._relay_heat_ended_at = self._relay_heat_ended_at, None
        stopped_at, self._held_stopped_at = self._held_stopped_at, None
        if seen is False:
            # A blink needs a stop the estimator saw: on, then off, while the room's heat flowed
            # (the estimator saw power); not a state just read off, nor the stop of a boiler that
            # heated other rooms only: for this room its return is the onset of its heat. A stop
            # held as heat (a valve, or a relay whose heat flowed) counts from the boiler's stop, not
            # from its confirmation.
            heat_flowed = followed is True and algo._dead_time_power() > 0.01
            self._boiler_seen_off_at = (stopped_at if stopped_at is not None else now) if heat_flowed else None
        changed = algo.set_boiler_state(
            seen,
            current_temp=self._thermostat.current_temperature,
            since=self._boiler_read_since,
            blink=blink,
            ended_at=ended_at if seen is False else None,
            stopped_at=stopped_at if seen is False else None,
        )
        if changed:
            self._should_publish_intermediate = True
        return changed

    def _schedule_the_boiler_check(self, boiler_on: bool | None) -> None:
        """Check the boiler's start once it has held ``BOILER_BLINK_S``, while its changes are
        followed (``_listen_to_the_boiler``); a new change cancels the check."""
        self._cancel_the_boiler_check()
        if boiler_on is not True or self._boiler_listener_remove is None:
            return
        since = self._boiler_read_since

        @callback
        def _held(_now) -> None:
            if self._boiler_read is not True or self._boiler_read_since != since:
                return  # the boiler changed since: a later check is due, if any
            self._boiler_check_remove = None
            self._boiler_held = True
            self._on_boiler_change(None)

        self._boiler_check_remove = async_call_later(self._thermostat.hass, BOILER_BLINK_S, _held)

    def _cancel_the_boiler_check(self) -> None:
        if self._boiler_check_remove is not None:
            self._boiler_check_remove()
            self._boiler_check_remove = None

    def _calls_for_boiler_heat(self, algo: SmartPI) -> bool:
        """True while SmartPI calls for heat for a thermostat that Versatile Thermostat counts
        for its central boiler."""
        t = self._thermostat
        if getattr(t, "is_used_by_central_boiler", False) is not True:
            return False
        power = algo.committed_on_percent
        return isinstance(power, (int, float)) and power > 0.0

    def _listen_to_the_boiler(self) -> None:
        """Follow the changes of Versatile Thermostat's central boiler state, once its entity
        exists, so that its state and the pause are checked at the moment the boiler starts or
        stops; for a thermostat on a relay, follow its own state too, which Versatile Thermostat
        writes at each change of the relay (``_on_thermostat_change``)."""
        t = self._thermostat
        if self._boiler_listener_remove is not None or getattr(t, "is_used_by_central_boiler", False) is not True:
            return
        entity_id = central_boiler_entity_id(t.hass)
        if entity_id is not None:
            self._boiler_listener_remove = async_track_state_change_event(t.hass, [entity_id], self._on_boiler_change)
            if self._drives_a_relay() and getattr(t, "entity_id", None):
                self._thermostat_listener_remove = async_track_state_change_event(t.hass, [t.entity_id], self._on_thermostat_change)

    @callback
    def _on_thermostat_change(self, event: Event | None) -> None:
        """The thermostat's state changed: when its relay turned on or off, the boiler's state
        and the pause are checked now."""
        if (getattr(self._thermostat, "is_device_active", False) is True) != (self._device_on_since is not None):
            self._on_boiler_change(event)

    @callback
    def _on_boiler_change(self, _event: Event | None) -> None:
        """The boiler started or stopped: its state and the pause are checked now, not at the
        next calculation."""
        t = self._thermostat
        algo = t.prop_algorithm if isinstance(t.prop_algorithm, SmartPI) else None
        if algo is not None and self._update_boiler_learning_pause(algo):
            async_dispatcher_send(t.hass, diag_update_signal(t.unique_id))

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

        # Notify autocalib trigger about the cancellation (it never returns an event)
        algo.autocalib.on_calibration_complete(time.time(), algo, SmartPICalibrationResult.CANCELLED)

        # The cancellation is notified above: drop the pending completion
        algo.calibration_mgr.take_completed_result()

        # Update attributes and save state
        self.update_attributes()
        t.async_write_ha_state()
        await self._async_save()

    def update_attributes(self):
        """Keep climate attributes generic for SmartPI."""
        t = self._thermostat
        specific_states = t._attr_extra_state_attributes["specific_states"]
        diagnostic_entity_id = self._resolve_diagnostic_entity_id()
        specific_states["regulation_diagnostics"] = diagnostic_entity_id
        algo = t.prop_algorithm if isinstance(t.prop_algorithm, SmartPI) else None
        specific_states["smartpi_learning_enabled"] = (
            algo.learning_enabled if algo is not None else True
        )

    def _resolve_diagnostic_entity_id(self) -> str | None:
        """Resolve the SmartPI diagnostic sensor entity id from the registry."""
        t = self._thermostat
        registry = er.async_get(t.hass)
        diagnostic_unique_id = f"{DIAGNOSTIC_SENSOR_UNIQUE_ID_PREFIX}_{t.unique_id}"
        return registry.async_get_entity_id(SENSOR_DOMAIN, DOMAIN, diagnostic_unique_id)

    async def service_reset_smart_pi_learning(self):
        """Reset learning data."""
        t = self._thermostat
        if t.prop_algorithm and isinstance(t.prop_algorithm, SmartPI):
            t.prop_algorithm.reset_learning()
            t.hass.bus.async_fire(EventType.SMART_PI_EVENT.value, {
                 "entity_id": t.entity_id,
                 "type": "learning_reset"
             })
            write_event_log(_LOGGER, t, "SmartPI learning reset")
            await self.control_heating(force=True)
            self.update_attributes()
            t.async_write_ha_state()
            await self._async_save()

    async def service_set_smartpi_learning(self, learning_enabled: bool):
        """Enable or pause SmartPI thermal learning."""
        t = self._thermostat
        if t.prop_algorithm and isinstance(t.prop_algorithm, SmartPI):
            enabled = bool(learning_enabled)
            t.prop_algorithm.set_learning_enabled(enabled)
            t.hass.bus.async_fire(EventType.SMART_PI_EVENT.value, {
                "entity_id": t.entity_id,
                "type": "learning_enabled_changed",
                "learning_enabled": enabled,
            })
            write_event_log(
                _LOGGER,
                t,
                f"SmartPI learning {'enabled' if enabled else 'paused'}",
            )
            self.update_attributes()
            t.async_write_ha_state()
            async_dispatcher_send(t.hass, diag_update_signal(t.unique_id))
            await self._async_save()

    async def service_force_smartpi_calibration(self):
        """Force calibration."""
        t = self._thermostat
        if t.prop_algorithm and isinstance(t.prop_algorithm, SmartPI):
            event = t.prop_algorithm.force_calibration()
            t.hass.bus.async_fire(EventType.SMART_PI_EVENT.value, {
                 "entity_id": t.entity_id,
                 "type": "force_calibration"
             })
            if event is not None:
                self._fire_autocalib_event(event)

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
            t.hass.bus.async_fire(EventType.SMART_PI_EVENT.value, {
                "entity_id": t.entity_id,
                "type": "integral_reset"
            })
            write_event_log(_LOGGER, t, "SmartPI integral reset to zero")
            self.update_attributes()
            t.async_write_ha_state()
            await self._async_save()
