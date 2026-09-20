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
