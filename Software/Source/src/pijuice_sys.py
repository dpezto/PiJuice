#!/usr/bin/python3
# -*- coding: utf-8 -*-
from __future__ import print_function

import grp
import json
import logging
import os
import pwd
import signal
import stat
import subprocess
import sys
import time
import re

from pijuice import PiJuice
from pijuice_battery import ChargeLimiter, BatteryHistory, rtc_fields_now



class _JournalFormatter(logging.Formatter):
    """Prefix each line with the sd-daemon <priority> journald parses from stdout."""
    PRIORITY = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3, logging.CRITICAL: 2}

    def format(self, record):
        return '<%d>%s' % (self.PRIORITY.get(record.levelno, 6), super().format(record))


log = logging.getLogger('pijuice')
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JournalFormatter('%(message)s'))
log.addHandler(_handler)
log.setLevel(logging.INFO)

pijuice = None
btConfig = {}

configPath = '/var/lib/pijuice/pijuice_config.JSON'  # os.getcwd() + '/pijuice_config.JSON'
# Virtual power_supply exposed by the pijuice_power kernel module, so btop /
# upower / desktops see the HAT battery. Absent if the module isn't loaded.
POWER_SUPPLY_DIR = '/sys/class/power_supply/pijuice'
configData = {'system_task': {'enabled': False}}
status = {}
sysEvEn = False
minChgEn = False
minBatVolEn = False
lowChgEn = False
lowBatVolEn = False
watchdogEn = False
chargeLevel = 50
noPowEn = False
PowEn = False
sysStartEvEn = False
sysStopEvEn = False
noPowCnt = 0  # 0 will trigger at boot without power, 3 will not trigger at boot without power
PowCnt = 3    # 0 will trigger at boot with power, 3 will not trigger at boot with power
dopoll = True
buttonsEn = True  # a button event needs the 1 s status poll
PID_FILE = '/run/pijuice/pijuice_sys.pid'
HALT_FILE = '/run/pijuice/pijuice_halt.flag'
I2C_ADDRESS_DEFAULT = 0x14
I2C_BUS_DEFAULT = 1
chargeLimiter = None
chargeLimitMessage = None
batteryHistory = None
batteryHistoryError = None

PROFILE_TTL_S = 300  # the battery profile only changes when a UI writes it
_profile = None


def _Value(result):
    return result.get('data') if result.get('error') == 'NO_ERROR' else None


def _ReadBattery(status):
    """Each value read once per 5 s tick and shared by every consumer (it was
    ~15 reads, several repeated). The near-static profile is re-read every
    5 min or on SIGHUP. None marks a failed read."""
    global _profile
    now = time.monotonic()
    if _profile is None or _profile['data'] is None or now - _profile['at'] >= PROFILE_TTL_S:
        validity = _Value(pijuice.config.GetBatteryProfileStatus()) or {}
        _profile = {'at': now, 'data': _Value(pijuice.config.GetBatteryProfile()),
                    'valid': validity.get('validity') == 'VALID'}
    get = pijuice.status
    return {'charge': _Value(get.GetChargeLevel()), 'bat_mv': _Value(get.GetBatteryVoltage()),
            'bat_ma': _Value(get.GetBatteryCurrent()), 'temp': _Value(get.GetBatteryTemperature()),
            'io_mv': _Value(get.GetIoVoltage()), 'io_ma': _Value(get.GetIoCurrent()),
            'faults': _Value(get.GetFaultStatus()) if status.get('isFault') else {},
            'profile': _profile['data'], 'profile_valid': _profile['valid']}


def _TrackBattery(status, reads):
    global batteryHistory, batteryHistoryError
    try:
        if batteryHistory is None:
            batteryHistory = BatteryHistory()
        try:
            if reads['profile'] is None or not reads['profile_valid']:
                raise ValueError('Battery profile is invalid or unreadable')
            batteryHistory.observe(status, reads['profile'], reads['charge'], reads['io_mv'], reads['io_ma'],
                                   reads['bat_mv'], reset_token=configData.get("battery_tracking", {}).get("reset_token"))
        except Exception:
            batteryHistory.interrupt()
            raise
        batteryHistoryError = None
    except Exception as exc:
        message = str(exc)
        if message != batteryHistoryError:
            log.warning("Battery tracking: %s", message)
            batteryHistoryError = message


def _EvalChargeLimit(status, charge):
    global chargeLimiter, chargeLimitMessage
    try:
        if chargeLimiter is None:
            chargeLimiter = ChargeLimiter()
        message = chargeLimiter.step(pijuice, configData.get('battery_management', {}), status, charge)
    except Exception as exc:
        message = 'Charge limiter: ' + str(exc)
    if message != chargeLimitMessage:
        (log.warning if message.startswith('Charge limiter:') else log.info)(message)
        chargeLimitMessage = message


_pausedUntil = 0.0


def _PausePolling(signum, _frame):
    """SIGUSR1: stay off the bus for a firmware flash (longer than the flasher's
    600 s timeout, so it cannot resume mid-flash); SIGUSR2: resume."""
    global _pausedUntil
    _pausedUntil = time.monotonic() + 660 if signum == signal.SIGUSR1 else 0.0
    log.info('Polling %s', 'paused for a firmware update' if _pausedUntil else 'resumed')


def _RestoreWakeup():
    """Re-arm what the HAT forgets after a full battery drain (upstream #1035,
    #760, #853): the RTC clock, the alarm + wakeup enable saved by the UIs, and
    wakeup-on-charge from System Task. Runs once at daemon start."""
    now = rtc_fields_now()
    rtc = pijuice.rtcAlarm.GetTime()
    if rtc.get('error') == 'NO_ERROR' and rtc['data'].get('year', 0) < 2020 <= now['year']:
        ret = pijuice.rtcAlarm.SetTime(now)
        (log.warning if ret.get('error') == 'NO_ERROR' else log.error)(
            'RTC had lost its time; set from the Pi clock: %s', ret.get('error'))
    wanted = configData.get('wakeup_alarm') or {}
    if wanted.get('enabled'):
        ctrl = pijuice.rtcAlarm.GetControlStatus()
        if ctrl.get('error') == 'NO_ERROR' and not ctrl['data'].get('alarm_wakeup_enabled'):
            errors = []
            if wanted.get('alarm'):
                errors.append(pijuice.rtcAlarm.SetAlarm(wanted['alarm']).get('error'))
            errors.append(pijuice.rtcAlarm.SetWakeupEnabled(True).get('error'))
            errors = [e for e in errors if e != 'NO_ERROR']
            if errors:
                log.error('Wakeup alarm was off on the device and could not be re-armed: %s', errors)
            else:
                log.warning('Wakeup alarm was off on the device; re-armed from the saved schedule')
    woc = configData.get('system_task', {}).get('wakeup_on_charge', {})
    if woc.get('enabled') and woc.get('trigger_level') not in (None, ''):
        try:
            ret = pijuice.power.SetWakeUpOnCharge(int(float(woc['trigger_level'])))
        except (TypeError, ValueError) as exc:
            ret = {'error': str(exc)}
        if ret.get('error') != 'NO_ERROR':
            log.error('Could not arm wakeup on charge: %s', ret.get('error'))


_halting = None  # the event that halted the system, for the battery session record


def _SystemHalt(event):
    global _halting
    _halting = event or 'halt'
    if (event in ('low_charge', 'low_battery_voltage', 'no_power')
        and configData.get('system_task', {}).get('wakeup_on_charge', {}).get('enabled', False)
        and 'trigger_level' in configData['system_task']['wakeup_on_charge']):

        try:
            tl = float(configData['system_task']['wakeup_on_charge']['trigger_level'])
            pijuice.power.SetWakeUpOnCharge(tl)
        except:
            tl = None
    pijuice.status.SetLedBlink('D2', 3, [150, 0, 0], 200, [0, 100, 0], 200)
    # Setting halt flag for 'pijuice_sys.py stop'
    with open(HALT_FILE, 'w') as f:
        pass
    subprocess.call(["sudo", "halt"])

def ExecuteFunc(func, event, param, reason=None):
    if func.startswith('SYS_FUNC'):
        if _halting and func != 'SYS_FUNC_REBOOT':
            return  # one halt per run; the 5 s checks keep matching until systemd stops us
        log.warning('%s: %s -> %s', event, reason or param, func)
    elif func != 'NO_FUNC':
        log.info('%s: %s -> %s', event, reason or param, func)
    if func == 'SYS_FUNC_HALT':
        _SystemHalt(event)
    elif func == 'SYS_FUNC_HALT_POW_OFF':
        pijuice.power.SetSystemPowerSwitch(0)
        pijuice.power.SetPowerOff(60)
        _SystemHalt(event)
    elif func == 'SYS_FUNC_SYS_OFF_HALT':
        pijuice.power.SetSystemPowerSwitch(0)
        _SystemHalt(event)
    elif func == 'SYS_FUNC_REBOOT':
        subprocess.call(["sudo", "reboot"])
    elif ('USER_FUNC' in func) and ('user_functions' in configData) and (func in configData['user_functions']):
        function=configData['user_functions'][func]
        # Check function is defined
        if function == "":
            return
        # Remove possible argumemts
        cmd = function.split()[0]

        # Check cmd is an executable file and the file owner belongs
        # to the pijuice group.
        # If so, execute the command as the file owner

        try:
            statinfo = os.stat(cmd)
        except:
            # File not found
            return
        # Get owner and ownergroup names
        owner = pwd.getpwuid(statinfo.st_uid).pw_name
        # Do not allow programs owned by root
        if owner == 'root':
            log.warning("root owned %s not allowed", cmd)
            return
        # Check cmd has executable permission
        if statinfo.st_mode & stat.S_IXUSR == 0:
            log.warning("%s is not executable", cmd)
            return
        # Owner of cmd must belong to mygroup ('pijuice')
        if os.getegid() not in os.getgrouplist(owner, statinfo.st_gid):
            log.warning("%s owner ('%s') does not belong to '%s'", cmd, owner, grp.getgrgid(os.getegid()).gr_name)
            return
        # All checks passed
        try:
            subprocess.call(["sudo", "-u", owner, cmd, str(event), str(param)])
        except OSError as exc:
            log.error('Failed to execute user func %s: %s', cmd, exc)


def _EvalButtonEvents():
    btEvents = pijuice.status.GetButtonEvents()
    if btEvents['error'] == 'NO_ERROR':
        for b in pijuice.config.buttons:
            ev = btEvents['data'][b]
            if ev != 'NO_EVENT':
                if btConfig[b][ev]['function'] != 'USER_EVENT':
                    pijuice.status.AcceptButtonEvent(b)
                    if btConfig[b][ev]['function'] != 'NO_FUNC':
                        ExecuteFunc(btConfig[b][ev]['function'], ev, b)
        return True
    else:
        return False


# System Task check -> (System Event it fires, default threshold the UIs offer)
LOW_GUARDS = {'min_charge': ('low_charge', 10), 'min_bat_voltage': ('low_battery_voltage', 3.3)}
_badThreshold = set()


def _Threshold(section):
    """Configured threshold, or the UI default when missing/invalid (logged once)."""
    value = configData.get('system_task', {}).get(section, {}).get('threshold')
    try:
        return float(value)
    except (TypeError, ValueError):
        if section not in _badThreshold:
            _badThreshold.add(section)
            log.warning('system_task.%s.threshold %r invalid; using %s', section, value, LOW_GUARDS[section][1])
        return float(LOW_GUARDS[section][1])


def _Mains(status):
    # Only a PRESENT input carries the load. WEAK may not: the battery can still
    # drain behind a weak charger, so it stays guarded.
    return status.get('powerInput') == 'PRESENT' or status.get('powerInput5vIo') == 'PRESENT'


def _EvalCharge(status, charge):
    # The charge gauge is only trusted to fall while the battery is not charging:
    # on a WEAK input with CHARGING_FROM_* it may sit low while energy flows in.
    # The voltage check below still covers a weak charger losing the race.
    if status['battery'] != 'NORMAL' or _Mains(status):
        return True
    if charge is not None:
        level = float(charge)
        global chargeLevel
        th = _Threshold('min_charge')
        if level == 0 or ((level < th) and ((chargeLevel-level) >= 0 and (chargeLevel-level) < 3)):
            if lowChgEn:
                # energy is low, take action
                ExecuteFunc(configData['system_events']['low_charge']['function'], 'low_charge', level,
                            'charge %.0f%% below %.0f%% (%s)' % (level, th, status.get('powerInput')))

        chargeLevel = level
        return True
    else:
        return False


def _EvalBatVoltage(status, bat_mv):
    if status['battery'] == 'NOT_PRESENT' or _Mains(status):
        return True
    if bat_mv is not None:
        v = float(bat_mv) / 1000
        th = _Threshold('min_bat_voltage')
        if v < th and lowBatVolEn:
            ExecuteFunc(configData['system_events']['low_battery_voltage']['function'], 'low_battery_voltage', v,
                        'battery %.3f V below %.2f V (%s, %s)' % (v, th, status.get('battery'), status.get('powerInput')))
        return True
    else:
        return False

NO_POWER_STATUSES = ['NOT_PRESENT', 'BAD']
def _EvalPowerInputs(status):
    if (status['battery'] == 'NOT_PRESENT'): return
    global noPowCnt, PowCnt
    if status['powerInput'] in NO_POWER_STATUSES and status['powerInput5vIo'] in NO_POWER_STATUSES:
        # power is absent
        if noPowCnt:
            PowCnt = 0 # enable checking for return of power
        noPowCnt = min(noPowCnt + 1, 3)
        if noPowEn and noPowCnt == 2:
            # unplugged
            ExecuteFunc(configData['system_events']['no_power']['function'],
                                   'no_power', '')
    else:
        # power is present
        if PowCnt:
            noPowCnt = 0
        PowCnt = min(PowCnt + 1, 3)
        if PowEn and PowCnt == 2:
            ExecuteFunc(configData['system_events']['power']['function'],
                                   'power', '')

def _EvalFaultFlags(faults):
    if faults is not None:
        for f in (pijuice.status.faultEvents + pijuice.status.faults):
            if f in faults:
                if sysEvEn and (f in configData['system_events']) and ('enabled' in configData['system_events'][f]) and configData['system_events'][f]['enabled']:
                    if configData['system_events'][f]['function'] != 'USER_EVENT':
                        pijuice.status.ResetFaultFlags([f])
                        ExecuteFunc(configData['system_events'][f]['function'],
                                    f, faults[f])
        return True
    else:
        return False

def _ConfigureWatchdog(state):
    try:
        if state == 'ACTIVATE':
            if ('period' in configData['system_task']['watchdog']):
                p = int(configData['system_task']['watchdog']['period'])
                ret = pijuice.power.SetWatchdog(p)
            else:
                # Disable watchdog
                ret = pijuice.power.SetWatchdog(0)
                if ret['error'] != 'NO_ERROR':
                    time.sleep(0.05)
                    pijuice.power.SetWatchdog(0)
        else:
            # Disabling watchdog
            ret = pijuice.power.SetWatchdog(0)
            if ret['error'] != 'NO_ERROR':
                time.sleep(0.05)
                ret = pijuice.power.SetWatchdog(0)
    except:
        pass

def _GuardEnabled(section, event_enabled):
    """Whether the System Task check behind a low-energy event runs.

    An enabled event with no check configured gets the UI default threshold
    (it used to never fire, silently); a check switched off on purpose is
    respected but logged, as is System Task itself being off."""
    task = configData.setdefault('system_task', {})
    event = LOW_GUARDS[section][0]
    if event_enabled and not isinstance(task.get(section), dict):
        task[section] = {'enabled': True, 'threshold': LOW_GUARDS[section][1]}
        log.warning('%s is enabled but system_task.%s is missing; guarding at %s', event, section, LOW_GUARDS[section][1])
    enabled = task.get(section, {}).get('enabled', False)
    off = [name for name, on in (('System Task', task.get('enabled')), ('system_task.' + section, enabled)) if not on]
    if event_enabled and off:
        log.warning('%s is enabled but will never fire: %s off', event, ' and '.join(off))
    return enabled


def _LoadConfiguration():
    global pijuice
    global configData
    global btConfig
    global sysEvEn
    global minChgEn
    global minBatVolEn
    global watchdogEn
    global lowChgEn
    global lowBatVolEn
    global noPowEn
    global PowEn
    global sysStartEvEn
    global sysStopEvEn

    with open(configPath, 'r') as outputConfig:
        config_dict = json.load(outputConfig)
        configData.update(config_dict)

    sysEvEn = 'system_events' in configData
    watchdogEn = configData.get('system_task', {}).get('enabled') and configData.get('system_task', {}).get('watchdog', {}).get('enabled', False)
    lowChgEn = sysEvEn and configData.get('system_events', {}).get('low_charge', {}).get('enabled', False)
    lowBatVolEn = sysEvEn and configData.get('system_events', {}).get('low_battery_voltage', {}).get('enabled', False)
    minChgEn = _GuardEnabled('min_charge', lowChgEn)
    minBatVolEn = _GuardEnabled('min_bat_voltage', lowBatVolEn)
    noPowEn = sysEvEn and configData.get('system_events', {}).get('no_power', {}).get('enabled', False)
    PowEn = sysEvEn and configData.get('system_events', {}).get('power', {}).get('enabled', False)
    sysStartEvEn = sysEvEn and configData.get('system_events', {}).get('sys_start', {}).get('enabled', False)
    sysStopEvEn = sysEvEn and configData.get('system_events', {}).get('sys_stop', {}).get('enabled', False)

    try:
        addr = I2C_ADDRESS_DEFAULT
        bus = I2C_BUS_DEFAULT

        if 'board' in configData and 'general' in configData['board']:
            if 'i2c_addr' in configData['board']['general']:
                addr = int(configData['board']['general']['i2c_addr'], 16)
            if 'i2c_bus' in configData['board']['general']:
                bus = configData['board']['general']['i2c_bus']
        pijuice = PiJuice(bus, addr)
    except:
        sys.exit(0)

    try:
        for b in pijuice.config.buttons:
            conf = pijuice.config.GetButtonConfiguration(b)
            if conf['error'] == 'NO_ERROR':
                btConfig[b] = conf['data']
    except:
        pass
    global buttonsEn
    # Hardware functions run in the firmware and USER_EVENT is for other
    # programs: only system/user functions need the 1 s poll.
    buttonsEn = any(str((cfg or {}).get('function', '')).startswith(('SYS_FUNC', 'USER_FUNC'))
                    for events in btConfig.values() for cfg in events.values())

def reload_settings(signum=None, frame=None):
    _LoadConfiguration() # Update configuration
    global _profile
    _profile = None  # a UI may have changed the battery profile
    global watchdogEn
    if watchdogEn: _ConfigureWatchdog('ACTIVATE') # Update watchdog setting

_psWriteErrors = set()   # (attr, errno) already reported, so the journal isn't spammed
_psMissingReported = False
_psLast = {}             # attr -> value last written; unchanged values are skipped
_psRefreshAt = 0.0
PS_REFRESH_S = 60        # rewrite everything this often, so a module reload is refilled
_loadEmaMa = None        # smoothed discharge load for time_to_empty_now


def _TimeToEmpty(charge, capacity_mah):
    """Seconds left at the smoothed GPIO load; 0 when not discharging or unknown."""
    global _loadEmaMa
    load = batteryHistory.load_ma() if batteryHistory is not None else None
    if load is None or not isinstance(charge, int) or not capacity_mah:
        _loadEmaMa = None
        return 0
    # ponytail: EMA over 5 s samples; a real coulomb counter would replace this.
    _loadEmaMa = load if _loadEmaMa is None else 0.7 * _loadEmaMa + 0.3 * load
    seconds = charge / 100 * capacity_mah / _loadEmaMa * 3600
    return int(seconds // 60 * 60)  # minute steps keep uevents quiet


def _Health(status, temperature, faults):
    """Kernel health text from the HAT fault flags (read only when flagged)."""
    if status.get('battery') == 'NOT_PRESENT':
        return 'No battery'
    if not status.get('isFault'):
        return 'Good'
    if faults is None:
        return 'Unknown'
    if faults.get('battery_profile_invalid'):
        return 'Unspecified failure'
    thermal = faults.get('charging_temperature_fault', 'NORMAL')
    if thermal == 'SUSPEND':
        return 'Overheat' if isinstance(temperature, int) and temperature > 30 else 'Cold'
    return {'WARM': 'Warm', 'COOL': 'Cool'}.get(thermal, 'Good')


def _write_power_supply(attr, val):
    val = str(val)
    if _psLast.get(attr) == val:
        return
    try:
        with open(os.path.join(POWER_SUPPLY_DIR, attr), 'w') as f:
            f.write(val)
        _psLast[attr] = val
        _psWriteErrors.difference_update({k for k in _psWriteErrors if k[0] == attr})
    except OSError as e:
        if (attr, e.errno) not in _psWriteErrors:
            _psWriteErrors.add((attr, e.errno))
            log.error('pijuice_power: cannot write %s: %s', attr, e)


def _UpdatePowerSupply(status, reads):
    """Feed the pijuice_power module from this tick's reads (no I2C here)."""
    global _psMissingReported, _psRefreshAt
    if not os.path.isdir(POWER_SUPPLY_DIR):
        if not _psMissingReported:
            _psMissingReported = True
            log.warning('pijuice_power: %s missing, is the pijuice_power module loaded?', POWER_SUPPLY_DIR)
        _psLast.clear()
        return
    if _psMissingReported:
        _psMissingReported = False
        log.info('pijuice_power: %s present again', POWER_SUPPLY_DIR)
    if time.monotonic() >= _psRefreshAt:
        _psLast.clear()
        _psRefreshAt = time.monotonic() + PS_REFRESH_S
    design = (reads['profile'] or {}).get('capacity')
    design = design if isinstance(design, int) and 0 < design < 0xffffffff else None
    learned = batteryHistory.capacity_mah() if batteryHistory is not None else None
    full_mah = int(learned or design or 0)
    if design:
        _write_power_supply('charge_full_design', design * 1000)          # mAh -> uAh
        _write_power_supply('charge_full', full_mah * 1000)
    if batteryHistory is not None and batteryHistory.state:
        _write_power_supply('cycle_count', int(batteryHistory.state.get('cycles', 0)))
    bat = status.get('battery')
    charge = reads['charge']
    _write_power_supply('time_to_empty_now', _TimeToEmpty(charge, full_mah))
    if bat == 'NOT_PRESENT':
        _write_power_supply('present', 0)
        _write_power_supply('status', 'Unknown')
    else:
        _write_power_supply('present', 1)
        if bat in ('CHARGING_FROM_IN', 'CHARGING_FROM_5V_IO'):
            st = 'Charging'
        elif isinstance(charge, int) and charge >= 100:
            st = 'Full'
        elif status.get('powerInput') == 'PRESENT' or status.get('powerInput5vIo') == 'PRESENT':
            st = 'Not charging'
        else:
            st = 'Discharging'
        _write_power_supply('status', st)
    if isinstance(charge, int):
        _write_power_supply('capacity', charge)
    for attr, key, scale in (('voltage_now', 'bat_mv', 1000),   # mV   -> uV
                             ('current_now', 'bat_ma', 1000),   # mA   -> uA
                             ('temp', 'temp', 10)):             # degC -> 0.1 degC
        if isinstance(reads[key], int):
            _write_power_supply(attr, reads[key] * scale)
    _write_power_supply('health', _Health(status, reads['temp'], reads['faults']))


def main():
    global pijuice
    global configData
    global status
    global minChgEn
    global minBatVolEn
    global watchdogEn
    global noPowEn
    global PowEn
    global sysStartEvEn
    global sysStopEvEn

    pid = str(os.getpid())
    with open(PID_FILE, 'w') as pid_f:
        pid_f.write(pid)

    if not os.path.exists(configPath):
        with open(configPath, 'w+') as conf_f:
            conf_f.write(json.dumps(configData))

    _LoadConfiguration()

    # Handle SIGHUP signal to reload settings
    signal.signal(signal.SIGHUP, reload_settings)

    if len(sys.argv) > 1 and str(sys.argv[1]) == 'stop':
        try:
            ChargeLimiter().release(pijuice)
        except Exception as exc:
            log.error('Unable to release charge limit on stop: %s', exc)
        if os.path.isdir(POWER_SUPPLY_DIR):
            _write_power_supply('present', 0)  # nobody feeds it now; don't show a stale battery

        if sysStopEvEn:
            ExecuteFunc(configData['system_events']['sys_stop']['function'], 'sys_stop', configData)

        isHalting = False
        if os.path.exists(HALT_FILE):   # Created in _SystemHalt() called in main pijuice_sys process
            isHalting = True
            os.remove(HALT_FILE)

        if watchdogEn: _ConfigureWatchdog('DEACTIVATE')

        sysJobTargets = subprocess.check_output(["sudo", "systemctl", "list-jobs"]).decode('utf-8')
        reboot = True if re.search('reboot.target.*start', sysJobTargets) is not None else False                      # reboot.target exists
        swStop = True if re.search('(?:halt|shutdown).target.*start', sysJobTargets) is not None else False           # shutdown | halt exists
        causePowerOff = True if (swStop and not reboot) else False
        ret = pijuice.status.GetStatus()
        if ( ret['error'] == 'NO_ERROR'
            and not isHalting
            and causePowerOff                                # proper time to power down (!rebooting)
            and configData.get('system_task',{}).get('ext_halt_power_off', {}).get('enabled',False)
            ):
            # Set duration for when pijuice will cut power (Recommended 30+ sec, for halt to complete)
            try:
                powerOffDelay = int(configData['system_task']['ext_halt_power_off'].get('period',30))
                pijuice.power.SetPowerOff(powerOffDelay)
            except ValueError:
                pass
        sys.exit(0)

    # First check if rtc is operational when the rtc_ds1307 module is loaded.
    # If not, then reload the module
    # This can happen when the Pi is off and the PiJuice is in low power mode.
    # Then when applying power to the Pi, the PiJuice firmware may start too late
    # for the os probe of the rtc to succeed.

    # Check if rtc_ds1307 module is loaded
    with open('/proc/modules', 'r') as f:
        lines = f.readlines()

    rtcModuleFound = False
    for l in lines:
        if l.startswith('rtc_ds1307'):
            rtcModuleFound = True
            break

    # Nothing to do if rtc_ds1307 module is not loaded
    if rtcModuleFound:
        # Check for /dev/rtc (means rtc is operational)
        if os.path.exists('/dev/rtc'):
            log.info('RTC os-support OK')
        else:
            # Remove and reload the rtc_ds1307 module
            ret = os.system('sudo modprobe -r rtc_ds1307')
            if ret != 0:
                log.error('Remove rtc_ds1307 module failed')
            else:
                ret = os.system('sudo modprobe rtc_ds1307')
                if (ret != 0):
                    log.error('Reload rtc_ds1307 module failed')
                else:
                    if os.path.exists('/dev/rtc'):
                        log.info('rtc_ds1307 module reloaded and RTC os-support OK')
                    else:
                        log.warning('RTC os-support not available')

    if watchdogEn: _ConfigureWatchdog('ACTIVATE')
    try:
        _RestoreWakeup()
    except Exception as exc:  # never keep the daemon from polling
        log.error('Wakeup restore failed: %s', exc)

    if sysStartEvEn:
        ExecuteFunc(configData['system_events']['sys_start']['function'], 'sys_start', configData)

    def stop_tracking(_signum, _frame):
        global dopoll
        dopoll = False
    signal.signal(signal.SIGTERM, stop_tracking)
    signal.signal(signal.SIGUSR1, _PausePolling)
    signal.signal(signal.SIGUSR2, _PausePolling)
    tick = 0
    while dopoll:
        if time.monotonic() < _pausedUntil:
            time.sleep(1)
            continue
        task = configData.get('system_task', {}).get('enabled')
        if tick and not (task and buttonsEn):
            # The 1 s status read only serves button functions; without any,
            # stay off the bus between the 5 s checks.
            tick = (tick + 1) % 5
            time.sleep(1)
            continue
        ret = pijuice.status.GetStatus()
        if ret['error'] != 'NO_ERROR':
            log.error('Status read failed: %s', ret['error'])
            time.sleep(1)
            continue
        status = ret['data']
        if task and status['isButton']:
            _EvalButtonEvents()
        if tick == 0:
            # Every 5 s, one read of each value for all consumers. Safety
            # checks first, before anything that may write to the SD card.
            # Battery tracking, the charge limit and the power_supply feed run
            # regardless of the System Task switch.
            reads = _ReadBattery(status)
            if task:
                if status.get('isFault'):
                    _EvalFaultFlags(reads['faults'])
                if minChgEn:
                    _EvalCharge(status, reads['charge'])
                if minBatVolEn:
                    _EvalBatVoltage(status, reads['bat_mv'])
                if noPowEn or PowEn:
                    _EvalPowerInputs(status)
            _TrackBattery(status, reads)
            _EvalChargeLimit(status, reads['charge'])
            _UpdatePowerSupply(status, reads)
        tick = (tick + 1) % 5
        time.sleep(1)

    if batteryHistory is not None:
        batteryHistory.close_session('halt (%s)' % _halting if _halting else 'service stopped')
        batteryHistory.save()


if __name__ == '__main__':
    main()
