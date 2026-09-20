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
    ``pijuice_gtk.py``: config load/save and ``notify_service``.
  * Serialise every I2C transfer through a single worker thread so UI callbacks
    never block the event loop and concurrent access can't corrupt the bus.
  * Turn the library's ``{'data': ..., 'error': ...}`` dicts into return values
    or :class:`PiJuiceError` exceptions, so callers stop sniffing dicts.

Toolkit independence: :meth:`PiJuiceService.submit` returns a
``concurrent.futures.Future``. A GTK front-end marshals completion with
``GLib.idle_add``; an urwid front-end with its own event loop. The service knows
about neither.
"""

from pijuice_battery import battery_report, charge_policy

import copy
import json
import os
import subprocess
import tempfile
import uuid
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

# ON_OFF_STATUS is firmware-driven; the UIs never offer it.
LED_USER_SELECTABLE = ['NOT_USED', 'CHARGE_STATUS', 'USER_LED']

# pijuiceboot exit codes (returncode = 256 - index).
FIRMWARE_UPDATE_ERRORS = ['NO_ERROR', 'I2C_BUS_ACCESS_ERROR', 'INPUT_FILE_OPEN_ERROR',
                          'STARTING_BOOTLOADER_ERROR', 'FIRST_PAGE_ERASE_ERROR', 'EEPROM_ERASE_ERROR',
                          'INPUT_FILE_READ_ERROR', 'PAGE_WRITE_ERROR', 'PAGE_READ_ERROR',
                          'PAGE_VERIFY_ERROR', 'CODE_EXECUTE_ERROR']
FIRMWARE_HINTS = {
    'I2C_BUS_ACCESS_ERROR': 'Check if I2C bus is enabled.',
    'INPUT_FILE_OPEN_ERROR': 'Firmware binary file might be missing or damaged.',
    'STARTING_BOOTLOADER_ERROR': 'Try to start bootloader manually. Press and hold button SW3 '
                                 'while powering up RPI and PiJuice.',
}


def firmware_error(returncode):
    """Human-readable reason for a non-zero ``pijuice_boot`` exit, or ``None``."""
    if returncode == 0:
        return None
    index = 256 - returncode
    reason = FIRMWARE_UPDATE_ERRORS[index] if 0 < index < len(FIRMWARE_UPDATE_ERRORS) else 'UNKNOWN'
    return (reason + '. ' + FIRMWARE_HINTS.get(reason, '')).strip()


def pack_version(text):
    """``'1.6'`` -> ``0x16`` (the int the library's profile selector wants); 0 if unparsable."""
    try:
        major, minor = str(text).split('.')
        return (int(major) << 4) + int(minor)
    except (TypeError, ValueError):
        return 0


def version_to_str(number):
    return '{}.{}'.format(number >> 4, number & 15)


def readable(value):
    """Enum -> label shared by both UIs so wording never drifts."""
    aliases = {'PRESENT': 'Connected', 'NOT_PRESENT': 'Not connected', 'NORMAL': 'On battery',
               'CHARGING_FROM_IN': 'Charging via USB', 'CHARGING_FROM_5V_IO': 'Charging via GPIO',
               'NO_FUNC': 'No action', 'NOT_USED': 'Not used', 'USER_LED': 'Custom colour',
               'CHARGE_STATUS': 'Charge status', 'ON_OFF_STATUS': 'Power status'}
    value = str(value)
    return aliases.get(value, value.replace('_', ' ').capitalize() if '_' in value else value)


def validate_number(text, kind, lo, hi):
    """Parse a complete int/float within [lo, hi]; never clamp silently."""
    import math
    try:
        value = int(text) if kind == 'int' else float(text)
        if not math.isfinite(value) or (lo is not None and value < lo) or (hi is not None and value > hi):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Enter a %s between %s and %s.' % ('whole number' if kind == 'int' else 'number', lo, hi))
    return value


def schedule_values(text, lo, hi, hours=False):
    """Alarm field: one value, or several separated by ';' (hours accept AM/PM)."""
    values = []
    for token in str(text).upper().split(';'):
        token = token.strip()
        if hours and token.endswith(('AM', 'PM')):
            hour = validate_number(token[:-2].strip(), 'int', 1, 12)
            values.append(hour % 12 + (12 if token.endswith('PM') else 0))
        else:
            values.append(validate_number(token, 'int', lo, hi))
    return values[0] if len(values) == 1 else ';'.join(str(v) for v in sorted(set(values)))


def alarm_fields(alarm):
    """Flatten the library's alarm dict into the fields both UIs edit."""
    alarm = alarm or {}
    fields = {'day_type': 1 if 'weekday' in alarm else 0, 'every_day': False, 'day': '',
              'every_hour': alarm.get('hour') == 'EVERY_HOUR', 'hour': '',
              'minute_type': 1 if 'minute_period' in alarm else 0, 'minute': '', 'second': ''}
    day = alarm.get('weekday', alarm.get('day'))
    if day == 'EVERY_DAY':
        fields['every_day'] = True
    elif day is not None:
        fields['day'] = str(day)
    if not fields['every_hour'] and 'hour' in alarm:
        fields['hour'] = str(alarm['hour'])
    minute = alarm.get('minute_period', alarm.get('minute'))
    if minute is not None:
        fields['minute'] = str(minute)
    if 'second' in alarm:
        fields['second'] = str(alarm['second'])
    return fields


def rtc_fields_now():
    """Current UTC time in the layout ``rtcAlarm.SetTime`` expects."""
    import datetime
    t = datetime.datetime.now(datetime.timezone.utc)
    fields = {key: getattr(t, key) for key in ('second', 'minute', 'hour', 'day', 'month', 'year')}
    fields.update(weekday=(t.weekday() + 1) % 7 + 1, subsecond=0)
    return fields


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
    fd, temporary = tempfile.mkstemp(prefix='.pijuice-', dir=parent or '.')
    try:
        if os.path.exists(path):
            previous = os.stat(path)
            # Keep the shared pijuice group so the daemon can read desktop saves.
            os.fchown(fd, -1, previous.st_gid)
            os.fchmod(fd, previous.st_mode & 0o777)
        with os.fdopen(fd, 'w') as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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
        return subprocess.call(['sudo', '-n', 'kill', '-SIGHUP', str(pid)],
                               stdout=devnull, stderr=devnull, timeout=10)


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
                 pid_file=PID_FILE_DEFAULT, connect=True):
        self.config_path = config_path
        self.pid_file = pid_file
        self.config = load_config(config_path)
        self._bus_override = bus
        self._addr_override = address
        self.pj = None
        self.firmware_version = None
        # Single worker so all I2C transfers are serialised (the bus is shared).
        self._executor = ThreadPoolExecutor(max_workers=1)
        if connect:
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

    def close(self):
        self._executor.shutdown(wait=False)

    # ── config / service ─────────────────────────────────────────────────────
    def save_section(self, section, value):
        """Commit one validated form without publishing a failed or partial draft."""
        config = load_config(self.config_path)
        config[section] = copy.deepcopy(value)
        save_config(config, self.config_path)
        self.config = config
        return self.retry_notify()

    def retry_notify(self):
        try:
            return notify_service(self.pid_file)
        except (OSError, subprocess.TimeoutExpired):
            return -1

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

    def get_watchdog(self):
        """``(minutes, non_volatile)``; the library returns them side by side."""
        ret = self._require().power.GetWatchdog()
        return _unwrap(ret, 'GetWatchdog'), bool(ret.get('non_volatile'))

    def set_watchdog(self, minutes, non_volatile=False):
        return _unwrap(self._require().power.SetWatchdog(int(minutes), non_volatile), 'SetWatchdog')

    def get_wakeup_on_charge(self):
        """``(level_or_'DISABLED', non_volatile)``."""
        ret = self._require().power.GetWakeUpOnCharge()
        return _unwrap(ret, 'GetWakeUpOnCharge'), bool(ret.get('non_volatile'))

    def set_wakeup_on_charge(self, level, non_volatile=False):
        arg = 'DISABLED' if level == 'DISABLED' else int(float(level))
        return _unwrap(self._require().power.SetWakeUpOnCharge(arg, non_volatile), 'SetWakeUpOnCharge')

    # ── general/board domain (CLI only) ──────────────────────────────────────
    def get_run_pin(self):
        return _unwrap(self._require().config.GetRunPinConfig(), 'GetRunPinConfig')

    def set_run_pin(self, config):
        return _unwrap(self._require().config.SetRunPinConfig(config), 'SetRunPinConfig')

    def get_power_inputs(self):
        return _unwrap(self._require().config.GetPowerInputsConfig(), 'GetPowerInputsConfig')

    def set_power_inputs(self, config, non_volatile=True):
        return _unwrap(self._require().config.SetPowerInputsConfig(config, non_volatile),
                       'SetPowerInputsConfig')

    def get_power_regulator_mode(self):
        return _unwrap(self._require().config.GetPowerRegulatorMode(), 'GetPowerRegulatorMode')

    def set_power_regulator_mode(self, mode):
        return _unwrap(self._require().config.SetPowerRegulatorMode(mode), 'SetPowerRegulatorMode')

    def get_id_eeprom_write_protect(self):
        return _unwrap(self._require().config.GetIdEepromWriteProtect(), 'GetIdEepromWriteProtect')

    def set_id_eeprom_write_protect(self, status):
        return _unwrap(self._require().config.SetIdEepromWriteProtect(status), 'SetIdEepromWriteProtect')

    def get_id_eeprom_address(self):
        return _unwrap(self._require().config.GetIdEepromAddress(), 'GetIdEepromAddress')

    def set_id_eeprom_address(self, hex_address):
        return _unwrap(self._require().config.SetIdEepromAddress(hex_address), 'SetIdEepromAddress')

    def get_address(self, slave):
        return _unwrap(self._require().config.GetAddress(slave), 'GetAddress')

    def set_address(self, slave, hex_address):
        return _unwrap(self._require().config.SetAddress(slave, hex_address), 'SetAddress')

    def set_default_configuration(self):
        return _unwrap(self._require().config.SetDefaultConfiguration(), 'SetDefaultConfiguration')

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

    # ── LED domain ───────────────────────────────────────────────────────────
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

    # ── battery domain ───────────────────────────────────────────────────────
    @property
    def fw_int(self):
        """Firmware version as the ``(major << 4) | minor`` int the lib expects."""
        return pack_version((self.firmware_version or {}).get('version'))

    def get_battery_report(self):
        report = battery_report(self._require())
        report['policy'] = self.get_charge_policy()
        return report

    def reset_battery_history(self):
        return self.save_section('battery_tracking', {'reset_token': str(uuid.uuid4())})

    def get_charge_policy(self):
        return charge_policy(load_config(self.config_path).get('battery_management', {}))

    def set_charge_policy(self, policy):
        return self.save_section('battery_management', charge_policy(policy))

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

    def get_battery_profile(self):
        return _unwrap(self._require().config.GetBatteryProfile(), 'GetBatteryProfile')

    def set_custom_battery_profile(self, profile):
        return _unwrap(self._require().config.SetCustomBatteryProfile(profile), 'SetCustomBatteryProfile')

    def get_battery_ext_profile(self):
        return _unwrap(self._require().config.GetBatteryExtProfile(), 'GetBatteryExtProfile')

    def set_custom_battery_ext_profile(self, profile):
        return _unwrap(self._require().config.SetCustomBatteryExtProfile(profile),
                       'SetCustomBatteryExtProfile')

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

    # ── firmware domain ──────────────────────────────────────────────────────
    def flash_firmware(self, bin_file):
        """Run ``pijuiceboot <addr> <bin_file>``; return its exit code (0 = ok).

        Runs on the I2C worker so no other transfer touches the bus mid-flash.
        ponytail: no live page-progress parsing -- a blocking flash with a final
        result is enough for a rare, manual operation; add a callback-fed
        progress channel only if the UI needs a bar.
        """
        addr = self._require().config.interface.GetAddress()
        if not addr:
            raise PiJuiceError('NO_ADDRESS', 'firmware')
        return subprocess.call(['pijuiceboot', format(addr, 'x'), bin_file])
