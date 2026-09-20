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
from pijuice_battery import ChargeLimiter, BatteryHistory



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
PID_FILE = '/run/pijuice/pijuice_sys.pid'
HALT_FILE = '/run/pijuice/pijuice_halt.flag'
I2C_ADDRESS_DEFAULT = 0x14
I2C_BUS_DEFAULT = 1
chargeLimiter = None
chargeLimitMessage = None
batteryHistory = None
batteryHistoryError = None

def _TrackBattery():
    global batteryHistory, batteryHistoryError
    try:
        if batteryHistory is None:
            batteryHistory = BatteryHistory()
        batteryHistory.sample(pijuice, configData.get("battery_tracking", {}).get("reset_token"))
        batteryHistoryError = None
    except Exception as exc:
        message = str(exc)
        if message != batteryHistoryError:
            log.warning("Battery tracking: %s", message)
            batteryHistoryError = message


def _EvalChargeLimit(status):
    global chargeLimiter, chargeLimitMessage
    try:
        if chargeLimiter is None:
            chargeLimiter = ChargeLimiter()
        message = chargeLimiter.step(pijuice, configData.get('battery_management', {}), status)
    except Exception as exc:
        message = 'Charge limiter: ' + str(exc)
    if message != chargeLimitMessage:
        (log.warning if message.startswith('Charge limiter:') else log.info)(message)
        chargeLimitMessage = message


def _SystemHalt(event):
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

def ExecuteFunc(func, event, param):
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


def _EvalCharge(status):
    if  ((status['battery'] == 'NOT_PRESENT')
      or (status['powerInput'] == 'PRESENT')
      or (status['powerInput5vIo'] == 'PRESENT')):
        return True
    charge = pijuice.status.GetChargeLevel()
    if charge['error'] == 'NO_ERROR':
        level = float(charge['data'])
        global chargeLevel
        if ('threshold' in configData['system_task']['min_charge']):
            th = float(configData['system_task']['min_charge']['threshold'])
            if level == 0 or ((level < th) and ((chargeLevel-level) >= 0 and (chargeLevel-level) < 3)):
                global lowChgEn
                if lowChgEn:
                    # energy is low, take action
                    ExecuteFunc(configData['system_events']['low_charge']['function'],
                                'low_charge', level)

        chargeLevel = level
        return True
    else:
        return False


def _EvalBatVoltage(status):
    if  ((status['battery'] == 'NOT_PRESENT')
      or (status['powerInput'] == 'PRESENT')
      or (status['powerInput5vIo'] == 'PRESENT')):
        return True
    bv = pijuice.status.GetBatteryVoltage()
    if bv['error'] == 'NO_ERROR':
        v = float(bv['data']) / 1000
        try:
            th = float(configData['system_task'].get('min_bat_voltage', {}).get('threshold'))
        except ValueError:
            th = None
        if th is not None and v < th:
            global lowBatVolEn
            if lowBatVolEn:
                # Battery voltage below thresholdw, take action
                ExecuteFunc(configData['system_events']['low_battery_voltage']['function'], 'low_battery_voltage', v)

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

def _EvalFaultFlags():
    faults = pijuice.status.GetFaultStatus()
    if faults['error'] == 'NO_ERROR':
        faults = faults['data']
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
    minChgEn = configData.get('system_task', {}).get('min_charge', {}).get('enabled', False)
    minBatVolEn = configData.get('system_task', {}).get('min_bat_voltage', {}).get('enabled', False)
    watchdogEn = configData.get('system_task', {}).get('enabled') and configData.get('system_task', {}).get('watchdog', {}).get('enabled', False)
    lowChgEn = sysEvEn and configData.get('system_events', {}).get('low_charge', {}).get('enabled', False)
    lowBatVolEn = sysEvEn and configData.get('system_events', {}).get('low_battery_voltage', {}).get('enabled', False)
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

def reload_settings(signum=None, frame=None):
    _LoadConfiguration() # Update configuration
    global _batCapacityMah
    _batCapacityMah = None
    global watchdogEn
    if watchdogEn: _ConfigureWatchdog('ACTIVATE') # Update watchdog setting

_psWriteErrors = set()   # (attr, errno) already reported, so the journal isn't spammed
_psMissingReported = False
_batCapacityMah = None   # battery profile capacity, read once from the HAT


def _write_power_supply(attr, val):
    try:
        with open(os.path.join(POWER_SUPPLY_DIR, attr), 'w') as f:
            f.write(str(val))
        _psWriteErrors.difference_update({k for k in _psWriteErrors if k[0] == attr})
    except OSError as e:
        if (attr, e.errno) not in _psWriteErrors:
            _psWriteErrors.add((attr, e.errno))
            log.error('pijuice_power: cannot write %s: %s', attr, e)


def _UpdatePowerSupply(status):
    # ponytail: called from the 5s poll block, not the 1s path, so the extra
    # voltage/current/temp I2C reads stay cheap. Tighten if a UI lags.
    global _psMissingReported, _batCapacityMah
    if not os.path.isdir(POWER_SUPPLY_DIR):
        if not _psMissingReported:
            _psMissingReported = True
            log.warning('pijuice_power: %s missing, is the pijuice_power module loaded?', POWER_SUPPLY_DIR)
        return
    if _psMissingReported:
        _psMissingReported = False
        log.info('pijuice_power: %s present again', POWER_SUPPLY_DIR)
    if _batCapacityMah is None:
        prof = pijuice.config.GetBatteryProfile()
        if prof.get('error') == 'NO_ERROR' and isinstance(prof['data'].get('capacity'), int):
            _batCapacityMah = prof['data']['capacity']
    if _batCapacityMah:
        # Written every poll (no I2C) so a module reload picks it up again; the
        # module only raises a uevent when a value actually changes.
        learned = batteryHistory.capacity_mah() if batteryHistory is not None else None
        _write_power_supply('charge_full_design', _batCapacityMah * 1000)          # mAh -> uAh
        _write_power_supply('charge_full', int(learned or _batCapacityMah) * 1000)
    bat = status.get('battery')
    charge = pijuice.status.GetChargeLevel().get('data')
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
    v = pijuice.status.GetBatteryVoltage().get('data')      # mV   -> uV
    if isinstance(v, int):
        _write_power_supply('voltage_now', v * 1000)
    i = pijuice.status.GetBatteryCurrent().get('data')      # mA   -> uA
    if isinstance(i, int):
        _write_power_supply('current_now', i * 1000)
    t = pijuice.status.GetBatteryTemperature().get('data')  # degC -> 0.1 degC
    if isinstance(t, int):
        _write_power_supply('temp', t * 10)


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

    if sysStartEvEn:
        ExecuteFunc(configData['system_events']['sys_start']['function'], 'sys_start', configData)

    def stop_tracking(_signum, _frame):
        global dopoll
        dopoll = False
    signal.signal(signal.SIGTERM, stop_tracking)
    tick = 0
    while dopoll:
        ret = pijuice.status.GetStatus()
        if ret['error'] != 'NO_ERROR':
            log.error('Status read failed: %s', ret['error'])
            time.sleep(1)
            continue
        status = ret['data']
        task = configData.get('system_task', {}).get('enabled')
        if task and status['isButton']:
            _EvalButtonEvents()
        if tick == 0:
            # Every 5 s. Battery tracking, the charge limit and the power_supply
            # feed run regardless of the System Task switch.
            _TrackBattery()
            _EvalChargeLimit(status)
            _UpdatePowerSupply(status)
            if task:
                if status.get('isFault'):
                    _EvalFaultFlags()
                if minChgEn:
                    _EvalCharge(status)
                if minBatVolEn:
                    _EvalBatVoltage(status)
                if noPowEn or PowEn:
                    _EvalPowerInputs(status)
        tick = (tick + 1) % 5
        time.sleep(1)

    if batteryHistory is not None:
        batteryHistory.save()


if __name__ == '__main__':
    main()
