"""Daemon-side behaviour that needs no hardware: wakeup restore and the power_supply feed."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SOURCE), str(SOURCE / 'src')]
import pijuice_sys as daemon


def ok(data=None):
    return {'error': 'NO_ERROR', 'data': data}


class SysTests(unittest.TestCase):
    def setUp(self):
        self.pj = Mock()
        daemon.pijuice = self.pj
        daemon.configData = {'system_task': {'enabled': True}}
        self.pj.rtcAlarm.GetTime.return_value = ok({'year': 2026})
        self.pj.rtcAlarm.SetAlarm.return_value = self.pj.rtcAlarm.SetWakeupEnabled.return_value = ok()
        self.pj.power.SetWakeUpOnCharge.return_value = ok()

    READS = {'charge': 50, 'bat_mv': 3900, 'bat_ma': -400, 'temp': 24, 'io_mv': 5000, 'io_ma': 400,
             'faults': {}, 'profile': {'capacity': 2000}, 'profile_valid': True}

    def test_power_supply_feed_writes_health_cycles_and_time_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon.POWER_SUPPLY_DIR = tmp
            daemon._loadEmaMa = None
            daemon._psLast.clear()
            history = Mock()
            history.state = {'cycles': 3.7}
            history.capacity_mah.return_value = 1800
            history.load_ma.return_value = 450.0
            daemon.batteryHistory = history
            read = lambda attr: Path(tmp, attr).read_text()
            daemon._UpdatePowerSupply({'battery': 'NORMAL', 'powerInput': 'NOT_PRESENT', 'powerInput5vIo': 'NOT_PRESENT'}, self.READS)
            self.assertEqual(read('cycle_count'), '3')
            self.assertEqual(read('health'), 'Good')
            self.assertEqual(read('charge_full'), '1800000')
            self.assertEqual(read('charge_full_design'), '2000000')
            self.assertEqual(read('time_to_empty_now'), '7200')  # 900 mAh left at 450 mA
            self.assertEqual(read('voltage_now'), '3900000')
            daemon._UpdatePowerSupply({'battery': 'NORMAL', 'isFault': True, 'powerInput': 'PRESENT'},
                                      dict(self.READS, faults={'charging_temperature_fault': 'SUSPEND'}))
            self.assertEqual(read('health'), 'Cold')
            history.load_ma.return_value = None
            daemon._UpdatePowerSupply({'battery': 'NOT_PRESENT'}, self.READS)
            self.assertEqual(read('time_to_empty_now'), '0')
            self.assertEqual(read('health'), 'No battery')
            self.pj.status.assert_not_called()  # the feed does no I2C of its own

    def test_power_supply_skips_unchanged_values_until_the_refresh(self):
        with tempfile.TemporaryDirectory() as tmp, patch('builtins.open', wraps=open) as opened:
            daemon.POWER_SUPPLY_DIR = tmp
            daemon.batteryHistory = None
            daemon._psLast.clear()
            daemon._psRefreshAt = float('inf')
            status = {'battery': 'NORMAL', 'powerInput': 'PRESENT'}
            daemon._UpdatePowerSupply(status, self.READS)
            first = opened.call_count
            daemon._UpdatePowerSupply(status, self.READS)
            self.assertEqual(opened.call_count, first)       # nothing changed, nothing written
            daemon._UpdatePowerSupply(status, dict(self.READS, charge=49))
            self.assertEqual(opened.call_count, first + 1)   # only capacity
            daemon._psRefreshAt = 0
            daemon._UpdatePowerSupply(status, self.READS)
            self.assertEqual(opened.call_count - first - 1, first)  # periodic full rewrite

    def test_one_read_per_value_per_tick_and_cached_profile(self):
        daemon._profile = None
        for name in ('GetChargeLevel', 'GetBatteryVoltage', 'GetBatteryCurrent', 'GetBatteryTemperature',
                     'GetIoVoltage', 'GetIoCurrent', 'GetFaultStatus'):
            getattr(self.pj.status, name).return_value = ok(1)
        self.pj.config.GetBatteryProfile.return_value = ok({'capacity': 1600})
        self.pj.config.GetBatteryProfileStatus.return_value = ok({'validity': 'VALID'})
        reads = daemon._ReadBattery({'isFault': False})
        daemon._ReadBattery({'isFault': True})
        self.assertEqual(reads['profile'], {'capacity': 1600})
        self.pj.config.GetBatteryProfile.assert_called_once()
        self.assertEqual(self.pj.status.GetChargeLevel.call_count, 2)
        self.pj.status.GetFaultStatus.assert_called_once()  # only when flagged
        self.pj.status.GetChargeLevel.return_value = {'error': 'COMMUNICATION_ERROR'}
        self.assertIsNone(daemon._ReadBattery({})['charge'])

    def test_button_poll_only_for_functions_the_daemon_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon.configPath = str(Path(tmp, 'config.json'))
            Path(daemon.configPath).write_text('{}')
            self.pj.config.buttons = ['SW1']
            for function, polled in (('HARD_FUNC_POWER_ON', False), ('USER_EVENT', False),
                                     ('SYS_FUNC_HALT', True), ('USER_FUNC1', True)):
                self.pj.config.GetButtonConfiguration.return_value = ok(
                    {'PRESS': {'function': function, 'parameter': 0}, 'RELEASE': {'function': 'NO_FUNC', 'parameter': 0}})
                with patch.object(daemon, 'PiJuice', return_value=self.pj):
                    daemon._LoadConfiguration()
                self.assertEqual(daemon.buttonsEn, polled, function)

    def test_pause_signal_outlasts_the_flasher_timeout(self):
        import signal, time
        daemon._PausePolling(signal.SIGUSR1, None)
        self.assertGreater(daemon._pausedUntil, time.monotonic() + 600)
        daemon._PausePolling(signal.SIGUSR2, None)
        self.assertEqual(daemon._pausedUntil, 0.0)

    def test_restore_rearms_only_when_device_lost_it(self):
        daemon.configData['wakeup_alarm'] = {'enabled': True, 'alarm': {'hour': 3, 'minute': 0}}
        self.pj.rtcAlarm.GetControlStatus.return_value = ok({'alarm_wakeup_enabled': True})
        daemon._RestoreWakeup()
        self.pj.rtcAlarm.SetAlarm.assert_not_called()
        self.pj.rtcAlarm.GetControlStatus.return_value = ok({'alarm_wakeup_enabled': False})
        daemon._RestoreWakeup()
        self.pj.rtcAlarm.SetAlarm.assert_called_once_with({'hour': 3, 'minute': 0})
        self.pj.rtcAlarm.SetWakeupEnabled.assert_called_once_with(True)

    def test_restore_leaves_a_deliberately_disabled_alarm_alone(self):
        daemon.configData['wakeup_alarm'] = {'enabled': False, 'alarm': {'hour': 3}}
        self.pj.rtcAlarm.GetControlStatus.return_value = ok({'alarm_wakeup_enabled': False})
        daemon._RestoreWakeup()
        self.pj.rtcAlarm.SetWakeupEnabled.assert_not_called()

    def test_restore_sets_lost_rtc_and_wakeup_on_charge(self):
        self.pj.rtcAlarm.GetTime.return_value = ok({'year': 2000})
        self.pj.rtcAlarm.SetTime.return_value = ok()
        daemon.configData['system_task']['wakeup_on_charge'] = {'enabled': True, 'trigger_level': '30'}
        daemon._RestoreWakeup()
        self.assertGreaterEqual(self.pj.rtcAlarm.SetTime.call_args.args[0]['year'], 2026)
        self.pj.power.SetWakeUpOnCharge.assert_called_once_with(30)


class LowEnergyTests(unittest.TestCase):
    """typhon 2026-10-21: low_charge enabled with no min_charge never fired, and a
    WEAK charger skipped every check."""
    BATTERY = {'battery': 'NORMAL', 'powerInput': 'NOT_PRESENT', 'powerInput5vIo': 'NOT_PRESENT'}

    def setUp(self):
        self.pj = Mock()
        daemon.pijuice = self.pj
        daemon._halting = None
        daemon.chargeLevel = 50
        daemon.lowChgEn = daemon.lowBatVolEn = True
        daemon.configData = {'system_task': {'enabled': True},
                             'system_events': {e: {'enabled': True, 'function': 'SYS_FUNC_HALT'}
                                               for e in ('low_charge', 'low_battery_voltage')}}
        self.halt = daemon._SystemHalt = Mock(side_effect=lambda ev: setattr(daemon, '_halting', ev))
        self.addCleanup(setattr, daemon, '_SystemHalt', ORIGINAL_HALT)

    def test_missing_check_gets_the_default_threshold(self):
        with self.assertLogs('pijuice', 'WARNING') as logs:
            self.assertTrue(daemon._GuardEnabled('min_charge', True))
        self.assertEqual(daemon.configData['system_task']['min_charge'], {'enabled': True, 'threshold': 10})
        self.assertIn('missing', logs.output[0])

    def test_switched_off_check_is_respected_but_logged(self):
        daemon.configData['system_task']['min_bat_voltage'] = {'enabled': False, 'threshold': 3.4}
        with self.assertLogs('pijuice', 'WARNING') as logs:
            self.assertFalse(daemon._GuardEnabled('min_bat_voltage', True))
        self.assertIn('never fire', logs.output[0])

    def test_none_threshold_falls_back_instead_of_crashing(self):
        daemon.configData['system_task']['min_bat_voltage'] = {'enabled': True, 'threshold': None}
        with self.assertLogs('pijuice', 'WARNING'):
            daemon._EvalBatVoltage(self.BATTERY, 3200)
        self.halt.assert_called_once_with('low_battery_voltage')

    def test_weak_input_keeps_voltage_guard_and_charge_guard_only_while_not_charging(self):
        daemon.configData['system_task']['min_charge'] = {'enabled': True, 'threshold': 10}
        daemon.configData['system_task']['min_bat_voltage'] = {'enabled': True, 'threshold': 3.4}
        daemon.chargeLevel = 8
        charging = dict(self.BATTERY, battery='CHARGING_FROM_IN', powerInput='WEAK')
        daemon._EvalCharge(charging, 7)
        daemon._EvalBatVoltage(charging, 3600)
        self.halt.assert_not_called()
        daemon._EvalBatVoltage(charging, 3350)  # a weak charger losing the race
        self.halt.assert_called_once_with('low_battery_voltage')
        daemon._halting = None
        daemon._EvalCharge(dict(charging, battery='NORMAL'), 7)
        self.assertEqual(self.halt.call_args.args, ('low_charge',))

    def test_present_input_skips_both_checks(self):
        powered = dict(self.BATTERY, powerInput='PRESENT')
        daemon._EvalCharge(powered, 0)
        daemon._EvalBatVoltage(powered, 3000)
        self.halt.assert_not_called()

    def test_halt_logs_cause_and_fires_once(self):
        daemon.configData['system_task']['min_bat_voltage'] = {'enabled': True, 'threshold': 3.4}
        with self.assertLogs('pijuice', 'WARNING') as logs:
            daemon._EvalBatVoltage(self.BATTERY, 3350)
            daemon._EvalBatVoltage(self.BATTERY, 3350)
        self.assertEqual(self.halt.call_count, 1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('3.350 V below 3.40 V', logs.output[0])
        self.assertIn('SYS_FUNC_HALT', logs.output[0])


ORIGINAL_HALT = daemon._SystemHalt


if __name__ == '__main__':
    unittest.main()
