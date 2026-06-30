# -*- coding: utf-8 -*-
"""UI-agnostic service layer for the PiJuice HAT.

This module decouples I2C access and on-disk configuration from any particular
user interface. Both ``pijuice_cli`` (urwid) and the GTK4 GUI consume it instead
of talking to the ``pijuice`` library and the JSON config directly, so the I2C
domain logic lives in exactly one place.

Responsibilities:
  * Build a :class:`pijuice.PiJuice` interface from the persisted config
    (resolving ``i2c_addr`` / ``i2c_bus``), degrading gracefully when no HAT is
    present (``service.available is False``) so the module imports anywhere.
  * Consolidate the helpers that were copy-pasted into ``pijuice_cli.py`` and
    ``pijuice_gui.py``: config load/save and ``notify_service``.
  * Serialise every I2C transfer through a single worker thread so UI callbacks
    never block the event loop and concurrent access can't corrupt the bus.
  * Turn the library's ``{'data': ..., 'error': ...}`` dicts into return values
    or :class:`PiJuiceError` exceptions, so callers stop sniffing dicts.

Toolkit independence: :meth:`PiJuiceService.submit` returns a
``concurrent.futures.Future``. A GTK front-end marshals completion with
``GLib.idle_add``; an urwid front-end with its own event loop. The service knows
about neither.
"""

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

try:
    from pijuice import PiJuice
except Exception:  # noqa: BLE001 - smbus/i2c absent off-Pi, or lib import error
    # Keep the service importable on dev machines without python3-smbus; the HAT
    # simply reports unavailable (available is False) until run on the device.
    PiJuice = None

# ── Defaults / paths (previously duplicated as module globals in both UIs) ────
BUS_DEFAULT = 1
ADDRESS_DEFAULT = 0x14
CONFIG_PATH_DEFAULT = '/var/lib/pijuice/pijuice_config.JSON'
PID_FILE_DEFAULT = '/run/pijuice/pijuice_sys.pid'

# LED function names. The firmware encodes the function as the *index* into the
# full list (0..3); the UI historically hid ON_OFF_STATUS from the dropdown.
# Keep both here so encode/decode can never desync (see set_led_color note).
LED_FUNCTIONS = ['NOT_USED', 'CHARGE_STATUS', 'ON_OFF_STATUS', 'USER_LED']
LED_USER_SELECTABLE = ['NOT_USED', 'CHARGE_STATUS', 'USER_LED']


class PiJuiceError(Exception):
    """Raised when the HAT returns a non ``NO_ERROR`` status or is absent."""

    def __init__(self, error, context=''):
        self.error = error
        self.context = context
        msg = error if not context else '{}: {}'.format(context, error)
        super(PiJuiceError, self).__init__(msg)


def load_config(path=CONFIG_PATH_DEFAULT):
    """Return the parsed JSON config, or ``{}`` if missing/unreadable."""
    try:
        with open(path, 'r') as fh:
            return json.load(fh)
    except (IOError, OSError, ValueError):
        return {}


def save_config(data, path=CONFIG_PATH_DEFAULT):
    """Write *data* as pretty JSON, creating the parent dir if needed."""
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent)
    with open(path, 'w+') as fh:
        json.dump(data, fh, indent=2)


def notify_service(pid_file=PID_FILE_DEFAULT):
    """Tell the running ``pijuice`` service to reload its config (SIGHUP).

    Returns 0 on success, non-zero otherwise. UI-agnostic: callers decide how to
    present a failure (CLI prints, GUI shows a dialog).
    """
    try:
        with open(pid_file, 'r') as fh:
            pid = int(fh.read())
    except (IOError, OSError, ValueError):
        return -1
    # No shell: pid is validated as int, args passed directly to sudo/kill.
    with open(os.devnull, 'wb') as devnull:
        return subprocess.call(['sudo', 'kill', '-SIGHUP', str(pid)],
                               stdout=devnull, stderr=devnull)


def _unwrap(result, context=''):
    """Return ``result['data']`` or raise :class:`PiJuiceError`.

    Some setters legitimately return only ``{'error': 'NO_ERROR'}`` (no data);
    in that case ``None`` is returned.
    """
    if not isinstance(result, dict):
        raise PiJuiceError('BAD_RESPONSE', context)
    error = result.get('error', 'NO_ERROR')
    if error != 'NO_ERROR':
        raise PiJuiceError(error, context)
    return result.get('data')


class PiJuiceService(object):
    """Serialised, UI-agnostic facade over :class:`pijuice.PiJuice`."""

    def __init__(self, bus=None, address=None, config_path=CONFIG_PATH_DEFAULT,
                 pid_file=PID_FILE_DEFAULT):
        self.config_path = config_path
        self.pid_file = pid_file
        self.config = load_config(config_path)
        self._bus_override = bus
        self._addr_override = address
        self.pj = None
        self.firmware_version = None
        # Single worker so all I2C transfers are serialised (the bus is shared).
        self._executor = ThreadPoolExecutor(max_workers=1)
        self.connect()

    # ── connection ───────────────────────────────────────────────────────────
    def _resolve_bus_addr(self):
        bus = self._bus_override
        addr = self._addr_override
        general = (self.config.get('board', {}) or {}).get('general', {}) or {}
        if bus is None:
            bus = general.get('i2c_bus', BUS_DEFAULT)
        if addr is None:
            raw = general.get('i2c_addr')
            addr = int(raw, 16) if raw is not None else ADDRESS_DEFAULT
        return bus, addr

    def connect(self):
        """(Re)build the PiJuice interface. Sets ``available`` accordingly."""
        try:
            if PiJuice is None:
                raise PiJuiceError('NO_LIBRARY', 'pijuice')
            bus, addr = self._resolve_bus_addr()
            self.pj = PiJuice(bus, addr)
            # A real transfer is the only reliable presence check.
            self.firmware_version = self.get_firmware_version()
        except Exception:
            self.pj = None
            self.firmware_version = None
        return self.available

    @property
    def available(self):
        return self.pj is not None

    def _require(self):
        """Return the live PiJuice interface or raise if absent."""
        if self.pj is None:
            raise PiJuiceError('NO_CONNECTION', 'PiJuice')
        return self.pj

    # ── async plumbing ───────────────────────────────────────────────────────
    def submit(self, fn, *args, **kwargs):
        """Run ``fn(*args, **kwargs)`` on the I2C worker, return a ``Future``.

        UIs attach completion via ``future.add_done_callback`` and marshal back
        to their main loop (GTK: ``GLib.idle_add``; urwid: event-loop alarm).
        """
        return self._executor.submit(fn, *args, **kwargs)

    def submit_method(self, name, *args, **kwargs):
        """Convenience: ``submit`` a named service method by string."""
        return self.submit(getattr(self, name), *args, **kwargs)

    def close(self):
        self._executor.shutdown(wait=False)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    # ── config / service ─────────────────────────────────────────────────────
    def reload_config(self):
        self.config = load_config(self.config_path)
        return self.config

    def save_and_notify(self):
        """Persist the in-memory config and SIGHUP the service. Returns notify rc."""
        save_config(self.config, self.config_path)
        return notify_service(self.pid_file)

    # ── status domain ────────────────────────────────────────────────────────
    def get_firmware_version(self):
        pj = self._require()
        return _unwrap(pj.config.GetFirmwareVersion(), 'GetFirmwareVersion')

    def get_status(self):
        pj = self._require()
        return _unwrap(pj.status.GetStatus(), 'GetStatus')

    def get_charge_level(self):
        pj = self._require()
        return _unwrap(pj.status.GetChargeLevel(), 'GetChargeLevel')

    def get_battery_voltage(self):
        pj = self._require()
        return _unwrap(pj.status.GetBatteryVoltage(), 'GetBatteryVoltage')

    def get_battery_current(self):
        pj = self._require()
        return _unwrap(pj.status.GetBatteryCurrent(), 'GetBatteryCurrent')

    def get_battery_temperature(self):
        pj = self._require()
        return _unwrap(pj.status.GetBatteryTemperature(), 'GetBatteryTemperature')

    def get_fault_status(self):
        pj = self._require()
        return _unwrap(pj.status.GetFaultStatus(), 'GetFaultStatus')

    def get_io_voltage(self):
        pj = self._require()
        return _unwrap(pj.status.GetIoVoltage(), 'GetIoVoltage')

    def get_io_current(self):
        pj = self._require()
        return _unwrap(pj.status.GetIoCurrent(), 'GetIoCurrent')

    # ── power domain ─────────────────────────────────────────────────────────
    def get_system_power_switch(self):
        pj = self._require()
        return _unwrap(pj.power.GetSystemPowerSwitch(), 'GetSystemPowerSwitch')

    def set_system_power_switch(self, milliamps):
        pj = self._require()
        return _unwrap(pj.power.SetSystemPowerSwitch(int(milliamps)),
                       'SetSystemPowerSwitch')

    # ── button domain ────────────────────────────────────────────────────────
    @property
    def buttons(self):
        return self._require().config.buttons

    @property
    def button_events(self):
        return self._require().config.buttonEvents

    def get_button_config(self, button):
        pj = self._require()
        return _unwrap(pj.config.GetButtonConfiguration(button),
                       'GetButtonConfiguration')

    def set_button_config(self, button, config):
        pj = self._require()
        return _unwrap(pj.config.SetButtonConfiguration(button, config),
                       'SetButtonConfiguration')

    # ── LED domain (carries the B1a "red disables green" fix) ────────────────
    @property
    def leds(self):
        return self._require().config.leds

    def get_led_config(self, led):
        pj = self._require()
        return _unwrap(pj.config.GetLedConfiguration(led),
                       'GetLedConfiguration')

    def set_led_config(self, led, config):
        pj = self._require()
        return _unwrap(pj.config.SetLedConfiguration(led, config),
                       'SetLedConfiguration')

    def get_led_state(self, led):
        pj = self._require()
        return _unwrap(pj.status.GetLedState(led), 'GetLedState')

    def set_led_color(self, led, rgb, ensure_user_led=True):
        """Drive *led* to ``rgb`` ([r, g, b], 0-255 each).

        B1a: the reported "turning on red disables green" almost always means the
        LED's *function* is CHARGE_STATUS, so the firmware keeps re-driving R/G
        for charge state and stomps on live writes. Direct ``SetLedState`` only
        sticks when the function is USER_LED. With ``ensure_user_led`` we switch
        the function to USER_LED first (persisting it), then write the colour.
        The library already sends R/G/B as three independent bytes, so the
        channels themselves are never coupled in software.
        """
        pj = self._require()
        rgb = [int(c) & 0xFF for c in rgb]
        if ensure_user_led:
            cfg = self.get_led_config(led) or {}
            if cfg.get('function') != 'USER_LED':
                self.set_led_config(led, {
                    'function': 'USER_LED',
                    'parameter': {'r': rgb[0], 'g': rgb[1], 'b': rgb[2]},
                })
        return _unwrap(pj.status.SetLedState(led, rgb), 'SetLedState')

    # ── battery domain ───────────────────────────────────────────────────────
    @property
    def fw_int(self):
        """Firmware version as the ``(major << 4) | minor`` int the lib expects."""
        try:
            major, minor = self.firmware_version['version'].split('.')
            return (int(major) << 4) + int(minor)
        except (TypeError, KeyError, ValueError, AttributeError):
            return 0

    def get_battery_profiles(self):
        """Predefined profile names for the connected firmware (no I2C)."""
        pj = self._require()
        pj.config.SelectBatteryProfiles(self.fw_int)
        return list(pj.config.batteryProfiles)

    @property
    def battery_temp_sense_options(self):
        return list(self._require().config.batteryTempSenseOptions)

    @property
    def rsoc_estimation_options(self):
        return list(self._require().config.rsocEstimationOptions)

    def get_battery_profile_status(self):
        return _unwrap(self._require().config.GetBatteryProfileStatus(),
                       'GetBatteryProfileStatus')

    def set_battery_profile(self, profile):
        return _unwrap(self._require().config.SetBatteryProfile(profile),
                       'SetBatteryProfile')

    def get_battery_temp_sense(self):
        return _unwrap(self._require().config.GetBatteryTempSenseConfig(),
                       'GetBatteryTempSenseConfig')

    def set_battery_temp_sense(self, value):
        return _unwrap(self._require().config.SetBatteryTempSenseConfig(value),
                       'SetBatteryTempSenseConfig')

    def get_rsoc_estimation(self):
        return _unwrap(self._require().config.GetRsocEstimationConfig(),
                       'GetRsocEstimationConfig')

    def set_rsoc_estimation(self, value):
        return _unwrap(self._require().config.SetRsocEstimationConfig(value),
                       'SetRsocEstimationConfig')

    def get_charging_config(self):
        return _unwrap(self._require().config.GetChargingConfig(), 'GetChargingConfig')

    def set_charging_config(self, enabled, non_volatile=True):
        return _unwrap(
            self._require().config.SetChargingConfig(
                {'charging_enabled': bool(enabled)}, non_volatile),
            'SetChargingConfig')

    # ── IO domain ────────────────────────────────────────────────────────────
    @property
    def io_pull_options(self):
        return list(self._require().config.ioPullOptions)

    @property
    def io_config_params(self):
        return self._require().config.ioConfigParams

    def io_supported_modes(self, pin):
        return list(self._require().config.ioSupportedModes[pin])

    def get_io_config(self, pin):
        return _unwrap(self._require().config.GetIoConfiguration(pin),
                       'GetIoConfiguration')

    def set_io_config(self, pin, config, non_volatile=True):
        return _unwrap(
            self._require().config.SetIoConfiguration(pin, config, non_volatile),
            'SetIoConfiguration')

    # ── RTC / wakeup domain ──────────────────────────────────────────────────
    def get_rtc_time(self):
        return _unwrap(self._require().rtcAlarm.GetTime(), 'GetTime')

    def set_rtc_time(self, fields):
        return _unwrap(self._require().rtcAlarm.SetTime(fields), 'SetTime')

    def get_alarm(self):
        return _unwrap(self._require().rtcAlarm.GetAlarm(), 'GetAlarm')

    def set_alarm(self, alarm):
        return _unwrap(self._require().rtcAlarm.SetAlarm(alarm), 'SetAlarm')

    def get_alarm_control(self):
        return _unwrap(self._require().rtcAlarm.GetControlStatus(), 'GetControlStatus')

    def set_wakeup_enabled(self, enabled):
        return _unwrap(self._require().rtcAlarm.SetWakeupEnabled(bool(enabled)),
                       'SetWakeupEnabled')

    def clear_alarm_flag(self):
        return _unwrap(self._require().rtcAlarm.ClearAlarmFlag(), 'ClearAlarmFlag')

    # ── firmware domain ──────────────────────────────────────────────────────
    def get_i2c_address(self):
        return self._require().config.interface.GetAddress()

    def flash_firmware(self, bin_file):
        """Run ``pijuiceboot <addr> <bin_file>``; return its exit code (0 = ok).

        Runs on the I2C worker so no other transfer touches the bus mid-flash.
        ponytail: no live page-progress parsing -- a blocking flash with a final
        result is enough for a rare, manual operation; add a callback-fed
        progress channel only if the UI needs a bar.
        """
        addr = self.get_i2c_address()
        if not addr:
            raise PiJuiceError('NO_ADDRESS', 'firmware')
        return subprocess.call(['pijuiceboot', format(addr, 'x'), bin_file])
