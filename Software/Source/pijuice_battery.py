"""Battery reporting and an opt-in, host-enforced charge ceiling.

The hardware retains responsibility for voltage, current and thermal protection.
Only volatile charging-enable writes are used; never change a battery profile to
implement a percentage limit. Ownership is persisted so a daemon restart can
release a pause without overriding charging that was already disabled manually.
"""
import json
import datetime
import statistics
import time
import math
import os
import re
import tempfile

LIMIT_STATE_PATH = '/var/lib/pijuice/charge_limit_state.json'
DEFAULT_POLICY = {'enabled': False, 'limit': 80, 'resume': 75}


def charge_policy(config):
    policy = dict(DEFAULT_POLICY)
    policy.update(config or {})
    if not isinstance(policy['enabled'], bool):
        raise ValueError('Charge limit enabled must be true or false.')
    for key in ('limit', 'resume'):
        value = policy[key]
        if isinstance(value, bool) or not str(value).isdigit():
            raise ValueError('Charge thresholds must be whole percentages.')
        policy[key] = int(value)
    if not 50 <= policy['limit'] <= 100:
        raise ValueError('Charge limit must be 50–100%.')
    if not 0 <= policy['resume'] <= policy['limit'] - 5:
        raise ValueError('Resume must be at least 5 percentage points below the limit.')
    return policy


def profile_label(profile):
    if profile == 'DEFAULT':
        return 'Automatic (board selector)'
    if profile == 'CUSTOM':
        return 'Existing custom profile'
    match = re.fullmatch(r'(.+)_(\d+)', profile)
    return '%s · %s mAh' % match.groups() if match else profile


def _data(result):
    if result.get('error') != 'NO_ERROR':
        raise RuntimeError(result.get('error', 'Invalid device response'))
    return result.get('data')


def battery_report(pj):
    """Report telemetry without treating configured capacity as measured health."""
    result = {}
    queries = {'status': pj.status.GetStatus, 'faults': pj.status.GetFaultStatus,
               'temperature': pj.status.GetBatteryTemperature, 'voltage': pj.status.GetBatteryVoltage,
               'current': pj.status.GetBatteryCurrent, 'charge': pj.status.GetChargeLevel,
               'charging': pj.config.GetChargingConfig,
               'profile': pj.config.GetBatteryProfile, 'profile_status': pj.config.GetBatteryProfileStatus}
    unavailable = []
    for key, getter in queries.items():
        try:
            result[key] = _data(getter())
        except Exception:
            result[key] = None
            unavailable.append(key)
    if result['temperature'] == -999:
        result['temperature'] = None
        unavailable.append('temperature')
    profile = result['profile'] if isinstance(result['profile'], dict) else {}
    capacity = profile.get('capacity')
    result['design_capacity'] = capacity if isinstance(capacity, int) and 0 < capacity < 0xffffffff else None
    faults = result['faults'] or {}
    issues = []
    if faults.get('battery_profile_invalid') or (result['profile_status'] or {}).get('validity') not in ('VALID', None):
        issues.append('Battery profile needs attention')
    thermal = faults.get('charging_temperature_fault')
    if thermal and thermal != 'NORMAL':
        issues.append('Charging temperature fault: ' + thermal.replace('_', ' ').lower())
    if (result['status'] or {}).get('battery') == 'NOT_PRESENT':
        result['condition'] = 'No battery detected'
    elif issues:
        result['condition'] = '; '.join(issues)
    elif unavailable:
        result['condition'] = 'Some readings unavailable; check connection and temperature sensing'
    else:
        result['condition'] = 'No battery faults reported'
    result['history'] = history_report()
    result['health'] = result['history']['text']
    return result


class ChargeLimiter:
    def __init__(self, path=LIMIT_STATE_PATH):
        self.path = path
        try:
            with open(path) as file:
                self.paused = json.load(file).get('paused') is True
        except (OSError, ValueError, AttributeError):
            self.paused = False

    def _remember(self, paused):
        parent = os.path.dirname(self.path)
        fd, temporary = tempfile.mkstemp(prefix='.charge-limit-', dir=parent)
        try:
            with os.fdopen(fd, 'w') as file:
                os.fchmod(file.fileno(), 0o640)
                json.dump({'paused': paused}, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
            self.paused = paused
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _set(pj, enabled):
        _data(pj.config.SetChargingConfig({'charging_enabled': enabled}, False))
        actual = _data(pj.config.GetChargingConfig())
        if actual.get('charging_enabled') is not enabled:
            raise RuntimeError('Charging change was not confirmed by the device')

    def release(self, pj):
        if self.paused:
            self._set(pj, True)
            self._remember(False)

    def step(self, pj, config, status):
        policy = charge_policy(config)
        if not policy['enabled']:
            self.release(pj)
            return 'Charge limit off'
        if not isinstance(status, dict) or 'battery' not in status:
            raise ValueError('Battery status unavailable')
        if status['battery'] == 'NOT_PRESENT':
            self.release(pj)
            return 'No battery detected'
        charge = _data(pj.status.GetChargeLevel())
        if isinstance(charge, bool) or not isinstance(charge, (int, float)) or not math.isfinite(charge) or not 0 <= charge <= 100:
            raise ValueError('Battery charge unavailable')
        enabled = _data(pj.config.GetChargingConfig()).get('charging_enabled')
        if not isinstance(enabled, bool):
            raise ValueError('Charging status unavailable')
        if self.paused and charge <= policy['resume']:
            self.release(pj)
            return 'Charging resumed'
        if charge >= policy['limit'] and enabled:
            if not self.paused:
                self._remember(True)  # Record ownership before the hardware write.
            self._set(pj, False)
            return 'Paused at charge limit'
        if self.paused and enabled:
            # Honour an active pause across restarts and external enable writes.
            self._set(pj, False)
        return 'Paused at charge limit' if self.paused else ('Charging allowed' if enabled else 'Charging disabled manually')


HISTORY_PATH = '/var/lib/pijuice/battery_history.json'


def _number(value, low, high):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and low <= value <= high)


def history_report(path=HISTORY_PATH):
    """Read the daemon's atomic snapshot; readers never alter tracking state."""
    try:
        with open(path) as file:
            state = json.load(file)
        if state.get('version') != 1:
            raise ValueError('Unrecognised history format')
        samples = state['samples']
        capacity = statistics.median(s['capacity_mah'] for s in samples) if samples else None
        design = state.get('design_capacity')
        health = capacity / design * 100 if capacity and design else None
        updated = state.get('updated', 0)
        active = 0 <= time.time() - updated <= 180
        since = datetime.datetime.fromtimestamp(state['started'], datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        lines = ['Equivalent cycles: %.2f estimated since %s' % (state['cycles'], since),
                 ('Capacity health: ~%.0f%% (~%.0f mAh; %d qualifying discharge%s)' %
                  (health, capacity, len(samples), '' if len(samples) == 1 else 's')) if health is not None
                 else 'Capacity health: learning — needs an uninterrupted discharge from at least 80% to 20%.',
                 'Tracking: ' + (state.get('status', 'Collecting readings') if active else 'Paused — background service has not updated recently.'),
                 'Cycles cover observed use only. Health is a rough output-load estimate using 90% conversion efficiency; charge-gauge and load errors affect it.']
        if state.get('last_estimate'):
            lines.append('Last capacity estimate: ' + datetime.datetime.fromtimestamp(state['last_estimate'], datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
        return {'cycles': state['cycles'], 'capacity_mah': capacity, 'health_percent': health,
                'samples': len(samples), 'started': state['started'], 'active': active,
                'text': '\n'.join(lines)}
    except FileNotFoundError:
        message = 'Battery history: waiting for the background service to start tracking.'
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        message = 'Battery history unavailable — check the background service log.'
    return {'cycles': None, 'capacity_mah': None, 'health_percent': None,
            'samples': 0, 'active': False, 'text': message}


class BatteryHistory:
    """Persist observed cycles and independent load-based capacity estimates.

    Never integrate firmware battery current: it may be derived from RSoC and
    configured capacity. GPIO load current is independently sensed. Capacity
    extrapolates delivered charge / observed SOC fraction / assumed efficiency.
    This is explicitly a rough estimate, not a laboratory capacity measurement.
    Missing samples, restarts, charging and profile changes break health runs.
    """
    def __init__(self, path=HISTORY_PATH):
        self.path = path
        try:
            with open(path) as file:
                self.state = json.load(file)
            if (self.state.get('version') != 1 or not _number(self.state.get('cycles'), 0, 1e9)
                    or not isinstance(self.state.get('samples'), list)):
                raise ValueError('Invalid battery history; preserve it and inspect the service log')
        except FileNotFoundError:
            self.state = None
        self.previous = None
        self.run = None
        self.floor = None
        self.last_save = None

    def capacity_mah(self):
        """Median learned full capacity, or None until a session qualifies."""
        samples = (self.state or {}).get('samples') or []
        return statistics.median(s['capacity_mah'] for s in samples) if samples else None

    def save(self, now=None):
        if self.state is None:
            return
        self.state['updated'] = time.time() if now is None else now
        fd, temporary = tempfile.mkstemp(prefix='.battery-history-', dir=os.path.dirname(self.path))
        try:
            with os.fdopen(fd, 'w') as file:
                os.fchmod(file.fileno(), 0o644)
                json.dump(self.state, file, allow_nan=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def sample(self, pj, reset_token=None):
        """A failed poll still breaks continuity; it must never bridge an outage."""
        try:
            status = _data(pj.status.GetStatus())
            profile = _data(pj.config.GetBatteryProfile())
            validity = _data(pj.config.GetBatteryProfileStatus())
            if validity.get('validity') != 'VALID':
                raise ValueError('Battery profile is invalid')
            self.observe(status, profile, _data(pj.status.GetChargeLevel()),
                         _data(pj.status.GetIoVoltage()), _data(pj.status.GetIoCurrent()),
                         _data(pj.status.GetBatteryVoltage()), reset_token=reset_token)
        except Exception:
            self.previous = self.run = self.floor = None
            if self.state is not None:
                self.state['status'] = 'Waiting for valid battery readings'
            raise

    def observe(self, status, profile, soc, io_mv, io_ma, bat_mv,
                now=None, monotonic=None, reset_token=None):
        now = time.time() if now is None else now
        tick = time.monotonic() if monotonic is None else monotonic
        design = profile.get('capacity')
        if not _number(design, 1, 1000000) or not _number(soc, 0, 100):
            self.previous = self.run = self.floor = None
            raise ValueError('Invalid charge or design capacity')
        signature = json.dumps(profile, sort_keys=True)
        changed = (self.state is None or self.state.get('profile') != signature
                   or self.state.get('reset_token') != reset_token)
        if changed:
            archives = [] if self.state is None else (self.state.get('archives', []) +
                         [{k: v for k, v in self.state.items() if k != 'archives'}])[-10:]
            self.state = {'version': 1, 'started': now, 'updated': now, 'profile': signature,
                          'design_capacity': design, 'reset_token': reset_token, 'cycles': 0.0,
                          'samples': [], 'archives': archives}
            self.previous = self.run = self.floor = None
        discharging = (status.get('battery') == 'NORMAL'
                       and status.get('powerInput') == 'NOT_PRESENT'
                       and status.get('powerInput5vIo') == 'NOT_PRESENT')
        valid_load = (_number(io_mv, 4000, 5500) and _number(io_ma, 1, 5000)
                      and _number(bat_mv, 2000, 4600))
        prev = self.previous
        dt = tick - prev['tick'] if prev else 0
        continuous = prev is not None and 0 < dt <= 30 and prev['discharging'] and discharging
        # An abrupt gauge correction must not count as battery wear.
        plausible = continuous and -2 <= prev['soc'] - soc <= 2
        if not plausible:
            self.run = None
            self.floor = soc if discharging else None
        elif self.floor is not None:
            if soc < self.floor:
                self.state['cycles'] += (self.floor - soc) / 100.0
                self.floor = soc
            # A sustained rise denotes a correction or missed recharge. Don't
            # count a subsequent fall through the same charge range twice.
            if soc > self.floor + 2:
                self.run = None
        effective_ma = io_mv * io_ma / bat_mv / 0.90 if valid_load else None
        if not discharging or not valid_load:
            self.run = None
        elif self.run and plausible and prev.get('effective_ma') is not None:
            if soc > self.run['last_soc'] + 2:
                self.run = None
            else:
                self.run['mah'] += (effective_ma + prev['effective_ma']) / 2 * dt / 3600
                self.run['last_soc'] = min(soc, self.run['last_soc'])
                span = self.run['start_soc'] - soc
                if soc <= 20 and span >= 60:
                    capacity = self.run['mah'] * 100 / span
                    # Reject impossible/outlier sessions, don't clamp them to
                    # 100% and conceal an invalid load/profile/calibration.
                    if 0.1 * design <= capacity <= 1.5 * design:
                        self.state['samples'] = (self.state['samples'] +
                            [{'capacity_mah': capacity, 'at': now, 'soc_span': span}])[-5:]
                        self.state['last_estimate'] = now
                    self.run = None
        if self.run is None and discharging and valid_load and soc >= 80:
            self.run = {'start_soc': soc, 'last_soc': soc, 'mah': 0.0}
        self.state['status'] = ('No battery detected' if status.get('battery') == 'NOT_PRESENT' else
                                'Recording discharge (%.0f%% → %.0f%%)' % (self.run['start_soc'], soc) if self.run else
                                'Counting cycles; waiting for an 80% → 20% discharge' if discharging else
                                'Monitoring — waiting for battery use')
        self.previous = {'tick': tick, 'soc': soc, 'discharging': discharging, 'effective_ma': effective_ma}
        if changed or self.last_save is None or tick - self.last_save >= 60:
            self.save(now)
            self.last_save = tick
