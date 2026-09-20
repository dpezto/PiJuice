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
import re
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
# What each LED function does (from the firmware: battery.c / led.c).
LED_FUNCTIONS_INFO = {
    'NOT_USED': 'Off.',
    'CHARGE_STATUS': 'Firmware shows the charge: above 50 % green (G), 15–50 % red+green, below 15 % red (R). '
                     'Blue (B) blinks while charging and stays on when full. The three values are the '
                     'brightness of each part; dimmed when the HAT is in low-power mode.',
    'USER_LED': 'Shows this colour until a script changes it (only this function accepts '
                'SetLedState / SetLedBlink). Use 0, 0, 0 for off until your script lights it.',
}

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


FIRMWARE_FILE_RE = re.compile(r'PiJuice-V(\d+)\.(\d+)_(\d+_\d+_\d+)\.elf\.binary$')
# Every released image (V1.0 .. V1.6) is 60-90 KB; the MCU flash is 128 KB.
FIRMWARE_SIZE_RANGE = (32 * 1024, 128 * 1024)


def firmware_error(returncode):
    """Human-readable reason for a non-zero ``pijuiceboot`` exit, or ``None``."""
    if returncode == 0:
        return None
    index = 256 - returncode
    reason = FIRMWARE_UPDATE_ERRORS[index] if 0 < index < len(FIRMWARE_UPDATE_ERRORS) else 'UNKNOWN'
    return (reason + '. ' + FIRMWARE_HINTS.get(reason, '')).strip()


def check_firmware_file(path):
    """Refuse an image the flasher would happily brick the HAT with."""
    if not FIRMWARE_FILE_RE.search(os.path.basename(path)):
        raise PiJuiceError('Not a PiJuice firmware file name (PiJuice-Vx.y_YYYY_MM_DD.elf.binary)', 'firmware')
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise PiJuiceError(str(exc), 'firmware')
    if not FIRMWARE_SIZE_RANGE[0] <= size <= FIRMWARE_SIZE_RANGE[1]:
        raise PiJuiceError('Image is %d bytes; a PiJuice image is 32-128 KB. Download it again.' % size, 'firmware')


def pack_version(text):
    """``'1.6'`` -> ``0x16`` (the int the library's profile selector wants); 0 if unparsable."""
    try:
        major, minor = str(text).split('.')
        return (int(major) << 4) + int(minor)
    except (TypeError, ValueError):
        return 0


def version_to_str(number):
    return '{}.{}'.format(number >> 4, number & 15)


# Button / system-event functions: label and what actually happens. The HARD
# ones are executed by the firmware itself (they work with the Pi off); the SYS
# ones are carried out by the pijuice service, so it must be running.
FUNCTIONS = {
    'NO_FUNC': ('No action', 'Nothing happens.'),
    'HARD_FUNC_POWER_ON': ('Power on', 'Firmware applies 5 V to the Pi (turns it on or wakes it).'),
    'HARD_FUNC_POWER_OFF': ('Hard power off', 'Firmware cuts 5 V to the Pi at once, without a shutdown. Not recommended.'),
    'HARD_FUNC_RESET': ('Hard reset', 'Firmware cycles 5 V to the Pi, forcing a reboot without a shutdown.'),
    'SYS_FUNC_HALT': ('Halt', 'The service shuts the OS down; the PiJuice keeps supplying 5 V.'),
    'SYS_FUNC_HALT_POW_OFF': ('Halt, then power off', 'Turns the system switch off, shuts the OS down, and cuts 5 V to the Pi 60 s later.'),
    'SYS_FUNC_SYS_OFF_HALT': ('System switch off, then halt', 'Turns the system switch (power to the GPIO header output) off and shuts the OS down; 5 V stays on.'),
    'SYS_FUNC_REBOOT': ('Reboot', 'The service reboots the OS.'),
    'USER_EVENT': ('User event', 'Not handled by the service; your own program reads it through the API.'),
}


def user_function_names(config):
    """``{'USER_FUNC1': 'Backup', ...}`` from the config's ``user_function_names`` section."""
    return {k: str(v).strip() for k, v in ((config or {}).get('user_function_names') or {}).items() if str(v).strip()}


def function_label(name, names=None):
    """Human label for a button/event function; user scripts use their given name."""
    if name in FUNCTIONS:
        return FUNCTIONS[name][0]
    if name.startswith('USER_FUNC'):
        return (names or {}).get(name) or 'User script ' + name[9:]
    return readable(name)


def function_description(name, config=None):
    if name in FUNCTIONS:
        return FUNCTIONS[name][1]
    if name.startswith('USER_FUNC'):
        path = ((config or {}).get('user_functions') or {}).get(name)
        return 'Runs %s as the pijuice user.' % path if path else 'No script set for this slot (see User Scripts).'
    return ''


def led_white(config, led):
    """``[r, g, b]`` (1-255 each) the LED needs to show white, from ``led_white``.
    255/255/255 means uncalibrated. An older ``led_limits`` percentage migrates."""
    config = config or {}
    raw = (config.get('led_white') or {}).get(led)
    if raw is None:
        try:
            level = round(255 * int((config.get('led_limits') or {}).get(led, 100)) / 100)
        except (TypeError, ValueError):
            level = 255
        raw = [level] * 3
    try:
        white = [max(1, min(255, int(v))) for v in raw]
        return white if len(white) == 3 else [255, 255, 255]
    except (TypeError, ValueError):
        return [255, 255, 255]


def _scale_led(config, white, to_device):
    """Map a colour between the user's 0-255 space and the device, per channel,
    so that the user's 255/255/255 lands on the calibrated white point."""
    if not isinstance(config, dict) or not isinstance(config.get('parameter'), dict):
        return config
    parameter = {}
    for channel, value in config['parameter'].items():
        point = white['rgb'.index(channel)] if channel in 'rgb' else 255
        try:
            scaled = int(value) * (point / 255 if to_device else 255 / point)
            parameter[channel] = max(0, min(255, round(scaled)))
        except (TypeError, ValueError):
            parameter[channel] = value
    return dict(config, parameter=parameter)


def readable(value):
    """Enum -> label shared by both UIs so wording never drifts."""
    aliases = {'PRESENT': 'Connected', 'NOT_PRESENT': 'Not connected', 'NORMAL': 'On battery',
               'CHARGING_FROM_IN': 'Charging via USB', 'CHARGING_FROM_5V_IO': 'Charging via GPIO',
               'NOT_USED': 'Not used', 'USER_LED': 'Custom colour',
               'CHARGE_STATUS': 'Charge status', 'ON_OFF_STATUS': 'Power status'}
    value = str(value)
    if value in aliases:
        return aliases[value]
    if value in FUNCTIONS:
        return FUNCTIONS[value][0]
    if '_' not in value and not value.isupper():
        return value
    text = value.replace('_', ' ').capitalize()
    return re.sub(r'(?<=[a-z])(?=\d)', ' ', text)  # LONG_PRESS1 -> Long press 1


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


def signal_service(sig, pid_file=PID_FILE_DEFAULT):
    """Send *sig* (``'SIGHUP'`` reload, ``'SIGUSR1'`` pause polling, ``'SIGUSR2'``
    resume) to the running ``pijuice`` daemon. Returns 0 on success."""
    try:
        with open(pid_file, 'r') as fh:
            pid = int(fh.read())
    except (IOError, OSError, ValueError):
        return -1
    # No shell: pid is validated as int, args passed directly to sudo/kill.
    with open(os.devnull, 'wb') as devnull:
        return subprocess.call(['sudo', '-n', 'kill', '-' + sig, str(pid)],
                               stdout=devnull, stderr=devnull, timeout=10)


def notify_service(pid_file=PID_FILE_DEFAULT):
    """Tell the running ``pijuice`` service to reload its config (SIGHUP)."""
    return signal_service('SIGHUP', pid_file)


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
        return self.save_sections(**{section: value})

    def save_sections(self, **sections):
        """Commit several sections in one atomic write and one service reload."""
        config = load_config(self.config_path)
        for section, value in sections.items():
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

    # The three diodes of an RGB LED share one current budget and differ in
    # efficiency, so the raw values that look white are not 255/255/255 (one
    # board needed 60/100/60). The white point is the calibration: every colour
    # is mapped through it before it reaches the firmware and mapped back on
    # read, so the UIs show the colour the user meant.
    def get_led_white(self, led):
        return led_white(load_config(self.config_path), led)

    def set_led_white(self, led, rgb):
        if len(rgb) != 3:
            raise ValueError('White point needs three values.')
        whites = dict(load_config(self.config_path).get('led_white') or {})
        whites[led] = [validate_number(v, 'int', 1, 255) for v in rgb]
        return self.save_section('led_white', whites)

    def set_led_state(self, led, rgb):
        """Live colour for scripts (function must be USER_LED), through the white point."""
        colour = _scale_led({'parameter': dict(zip('rgb', rgb))}, self.get_led_white(led), to_device=True)
        return _unwrap(self._require().status.SetLedState(led, [colour['parameter'][c] for c in 'rgb']), 'SetLedState')

    def get_led_config(self, led):
        pj = self._require()
        config = _unwrap(pj.config.GetLedConfiguration(led), 'GetLedConfiguration')
        return _scale_led(config, self.get_led_white(led), to_device=False)

    def set_led_config(self, led, config):
        pj = self._require()
        return _unwrap(pj.config.SetLedConfiguration(led, _scale_led(config, self.get_led_white(led), to_device=True)),
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
        result = _unwrap(self._require().rtcAlarm.SetAlarm(alarm), 'SetAlarm')
        self._remember_wakeup(alarm=alarm)
        return result

    def get_alarm_control(self):
        return _unwrap(self._require().rtcAlarm.GetControlStatus(), 'GetControlStatus')

    def set_wakeup_enabled(self, enabled):
        result = _unwrap(self._require().rtcAlarm.SetWakeupEnabled(bool(enabled)),
                         'SetWakeupEnabled')
        self._remember_wakeup(enabled=bool(enabled))
        return result

    def _remember_wakeup(self, **fields):
        """Keep the wanted alarm on disk: the HAT forgets it after a full battery
        drain, and the daemon re-arms it from here at start (upstream #1035)."""
        config = load_config(self.config_path)
        config.setdefault('wakeup_alarm', {}).update(fields)
        try:
            save_config(config, self.config_path)
            self.config = config
        except OSError as exc:
            raise PiJuiceError('Alarm set on the device, but not saved for restore: %s' % exc, 'config')

    # ── firmware domain ──────────────────────────────────────────────────────
    def flash_firmware(self, bin_file):
        """Run ``pijuiceboot`` on *bin_file*; raise :class:`PiJuiceError` with the
        reason and the flasher's last lines on failure, return 0 on success.

        Runs on the I2C worker so no other transfer touches the bus mid-flash;
        the daemon is asked to pause its polling meanwhile (SIGUSR1/SIGUSR2).
        ponytail: no live page-progress parsing -- a blocking flash with a final
        result is enough for a rare, manual operation.
        """
        check_firmware_file(bin_file)
        addr = self._require().config.interface.GetAddress()
        if not addr:
            raise PiJuiceError('NO_ADDRESS', 'firmware')
        bus, _addr = self._resolve_bus_addr()
        signal_service('SIGUSR1', self.pid_file)
        try:
            run = subprocess.run(['pijuiceboot', format(addr, 'x'), bin_file, str(bus)],
                                 capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PiJuiceError(str(exc), 'pijuiceboot')
        finally:
            signal_service('SIGUSR2', self.pid_file)
        if run.returncode:
            tail = ' | '.join(line for line in run.stdout.splitlines()[-3:] if line.strip())
            raise PiJuiceError('%s (%s)' % (firmware_error(run.returncode), tail or 'no output'), 'firmware')
        return 0
