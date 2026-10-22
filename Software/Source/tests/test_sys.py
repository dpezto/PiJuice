"""Daemon-side behaviour that needs no hardware: wakeup restore and the power_supply feed."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

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

    def test_power_supply_feed_writes_health_cycles_and_time_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon.POWER_SUPPLY_DIR = tmp
            daemon._batCapacityMah = 2000
            daemon._loadEmaMa = None
            history = Mock()
            history.state = {'cycles': 3.7}
            history.capacity_mah.return_value = 1800
            history.load_ma.return_value = 450.0
            daemon.batteryHistory = history
            self.pj.status.GetChargeLevel.return_value = ok(50)
            self.pj.status.GetBatteryVoltage.return_value = ok(3900)
            self.pj.status.GetBatteryCurrent.return_value = ok(-400)
            self.pj.status.GetBatteryTemperature.return_value = ok(24)
            self.pj.status.GetFaultStatus.return_value = ok({'charging_temperature_fault': 'SUSPEND'})
            read = lambda attr: Path(tmp, attr).read_text()
            daemon._UpdatePowerSupply({'battery': 'NORMAL', 'powerInput': 'NOT_PRESENT', 'powerInput5vIo': 'NOT_PRESENT'})
            self.assertEqual(read('cycle_count'), '3')
            self.assertEqual(read('health'), 'Good')
            self.assertEqual(read('charge_full'), '1800000')
            self.assertEqual(read('time_to_empty_now'), '7200')  # 900 mAh left at 450 mA
            self.pj.status.GetFaultStatus.assert_not_called()
            daemon._UpdatePowerSupply({'battery': 'NORMAL', 'isFault': True, 'powerInput': 'PRESENT'})
            self.assertEqual(read('health'), 'Cold')
            history.load_ma.return_value = None
            daemon._UpdatePowerSupply({'battery': 'NOT_PRESENT'})
            self.assertEqual(read('time_to_empty_now'), '0')
            self.assertEqual(read('health'), 'No battery')

    def test_pause_signal_holds_polling_for_two_minutes(self):
        import signal, time
        daemon._PausePolling(signal.SIGUSR1, None)
        self.assertGreater(daemon._pausedUntil, time.monotonic() + 100)
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
        daemon._halting = False
        daemon.chargeLevel = 50
        daemon.lowChgEn = daemon.lowBatVolEn = True
        daemon.configData = {'system_task': {'enabled': True},
                             'system_events': {e: {'enabled': True, 'function': 'SYS_FUNC_HALT'}
                                               for e in ('low_charge', 'low_battery_voltage')}}
        self.halt = daemon._SystemHalt = Mock(side_effect=lambda ev: setattr(daemon, '_halting', True))
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
        self.pj.status.GetBatteryVoltage.return_value = ok(3200)
        with self.assertLogs('pijuice', 'WARNING'):
            daemon._EvalBatVoltage(self.BATTERY)
        self.halt.assert_called_once_with('low_battery_voltage')

    def test_weak_input_keeps_voltage_guard_and_charge_guard_only_while_not_charging(self):
        daemon.configData['system_task']['min_charge'] = {'enabled': True, 'threshold': 10}
        daemon.configData['system_task']['min_bat_voltage'] = {'enabled': True, 'threshold': 3.4}
        self.pj.status.GetChargeLevel.return_value = ok(7)
        self.pj.status.GetBatteryVoltage.return_value = ok(3600)
        daemon.chargeLevel = 8
        charging = dict(self.BATTERY, battery='CHARGING_FROM_IN', powerInput='WEAK')
        daemon._EvalCharge(charging)
        daemon._EvalBatVoltage(charging)
        self.halt.assert_not_called()
        daemon._EvalCharge(dict(charging, battery='NORMAL'))
        self.halt.assert_called_once_with('low_charge')

    def test_present_input_skips_both_checks(self):
        powered = dict(self.BATTERY, powerInput='PRESENT')
        daemon._EvalCharge(powered)
        daemon._EvalBatVoltage(powered)
        self.pj.status.GetChargeLevel.assert_not_called()
        self.pj.status.GetBatteryVoltage.assert_not_called()

    def test_halt_logs_cause_and_fires_once(self):
        daemon.configData['system_task']['min_bat_voltage'] = {'enabled': True, 'threshold': 3.4}
        self.pj.status.GetBatteryVoltage.return_value = ok(3350)
        with self.assertLogs('pijuice', 'WARNING') as logs:
            daemon._EvalBatVoltage(self.BATTERY)
            daemon._EvalBatVoltage(self.BATTERY)
        self.assertEqual(self.halt.call_count, 1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('3.350 V below 3.40 V', logs.output[0])
        self.assertIn('SYS_FUNC_HALT', logs.output[0])


ORIGINAL_HALT = daemon._SystemHalt


if __name__ == '__main__':
    unittest.main()
