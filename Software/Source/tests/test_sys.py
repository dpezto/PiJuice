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


if __name__ == '__main__':
    unittest.main()
