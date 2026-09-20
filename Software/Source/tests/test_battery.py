"""Charge-limiter transitions and honest battery reporting, without hardware."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pijuice_battery import ChargeLimiter, battery_report, charge_policy, profile_label


def ok(data=None): return {'error': 'NO_ERROR', 'data': data}


class BatteryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'state.json')
        self.enabled = True
        self.level = 85
        self.writes = []
        self.config = Mock()
        self.config.GetChargingConfig.side_effect = lambda: ok({'charging_enabled': self.enabled})
        def change(config, nonvolatile):
            self.writes.append((config['charging_enabled'], nonvolatile))
            self.enabled = config['charging_enabled']
            return ok()
        self.config.SetChargingConfig.side_effect = change
        self.status = Mock()
        self.status.GetChargeLevel.side_effect = lambda: ok(self.level)
        self.pj = SimpleNamespace(config=self.config, status=self.status)
        self.policy = {'enabled': True, 'limit': 80, 'resume': 75}
        self.battery = {'battery': 'NORMAL', 'powerInput': 'PRESENT'}
        self.limiter = ChargeLimiter(self.path)

    def tearDown(self): self.tmp.cleanup()

    def step(self): return self.limiter.step(self.pj, self.policy, self.battery)

    def test_defaults_are_opt_in(self):
        self.assertFalse(charge_policy({})['enabled'])
        self.limiter.step(self.pj, {}, self.battery)
        self.assertEqual(self.writes, [])

    def test_hysteresis_and_no_repeated_or_persistent_writes(self):
        self.step()
        self.assertFalse(self.enabled)
        for value in (85, 81, 80, 79, 76):
            self.level = value
            self.step()
        self.assertEqual(self.writes, [(False, False)])
        self.level = 75
        self.step()
        self.assertTrue(self.enabled)
        self.assertEqual(self.writes, [(False, False), (True, False)])
        self.level = 79
        self.step()
        self.assertEqual(len(self.writes), 2)

    def test_manual_disable_is_never_claimed_or_overridden(self):
        self.enabled = False
        self.step()
        self.level = 20
        self.step()
        self.policy['enabled'] = False
        self.step()
        self.assertFalse(self.enabled)
        self.assertEqual(self.writes, [])

    def test_restart_recovers_ownership_and_disabled_policy_releases(self):
        self.step()
        self.limiter = ChargeLimiter(self.path)
        self.assertTrue(self.limiter.paused)
        self.policy['enabled'] = False
        self.step()
        self.assertTrue(self.enabled)
        self.assertFalse(ChargeLimiter(self.path).paused)

    def test_stop_only_restores_owned_pause(self):
        self.limiter.release(self.pj)
        self.assertEqual(self.writes, [])
        self.step()
        ChargeLimiter(self.path).release(self.pj)
        self.assertTrue(self.enabled)

    def test_invalid_readings_do_not_change_charging(self):
        for level in (None, -1, 101, float('nan'), True, '80'):
            self.level = level
            with self.assertRaises(ValueError): self.step()
        self.assertEqual(self.writes, [])

    def test_sensor_failure_does_not_resume_charging(self):
        self.step()
        self.status.GetChargeLevel.side_effect = lambda: {'error': 'NO_CONNECTION'}
        with self.assertRaises(RuntimeError): self.step()
        self.assertFalse(self.enabled)
        self.assertTrue(self.limiter.paused)

    def test_failed_write_can_be_retried_and_keeps_recovery_marker(self):
        self.config.SetChargingConfig.side_effect = lambda *_args: {'error': 'WRITE_FAILED'}
        with self.assertRaises(RuntimeError): self.step()
        self.assertTrue(ChargeLimiter(self.path).paused)
        self.assertTrue(self.enabled)

    def test_failed_resume_retains_ownership(self):
        self.step()
        self.level = 75
        self.config.SetChargingConfig.side_effect = lambda *_args: {'error': 'WRITE_FAILED'}
        with self.assertRaises(RuntimeError): self.step()
        self.assertTrue(ChargeLimiter(self.path).paused)

    def test_unchanged_hardware_is_not_reported_as_success(self):
        self.config.SetChargingConfig.side_effect = lambda *_args: ok()
        with self.assertRaisesRegex(RuntimeError, 'not confirmed'): self.step()

    def test_no_hardware_change_if_marker_cannot_be_saved(self):
        with patch.object(self.limiter, '_remember', side_effect=OSError('full')):
            with self.assertRaises(OSError): self.step()
        self.assertEqual(self.writes, [])

    def test_no_battery_releases_owned_pause(self):
        self.step()
        self.battery['battery'] = 'NOT_PRESENT'
        self.step()
        self.assertTrue(self.enabled)

    def test_policy_bounds_and_gap(self):
        for policy in ({'limit': 49}, {'limit': 101}, {'limit': 80, 'resume': 79}, {'enabled': 'true'}, {'limit': 80.5}):
            with self.assertRaises(ValueError): charge_policy(policy)

    def test_health_does_not_claim_configured_capacity_is_measured_health(self):
        self.status.GetStatus.return_value = ok(self.battery)
        self.status.GetFaultStatus.return_value = ok({})
        self.status.GetBatteryTemperature.return_value = ok(24)
        self.status.GetBatteryVoltage.return_value = ok(4000)
        self.status.GetBatteryCurrent.return_value = ok(300)
        self.config.GetBatteryProfile.return_value = ok({'capacity': 1820})
        self.config.GetBatteryProfileStatus.return_value = ok({'validity': 'VALID'})
        report = battery_report(self.pj)
        self.assertEqual(report['condition'], 'No battery faults reported')
        self.assertEqual(report['design_capacity'], 1820)
        self.assertIn('history', report)
        self.assertNotIn('not measured by this interface', report['health'])
        self.status.GetBatteryTemperature.return_value = ok(-999)
        self.assertIn('unavailable', battery_report(self.pj)['condition'])
        self.status.GetFaultStatus.return_value = ok({'charging_temperature_fault': 'HOT'})
        self.assertIn('temperature fault', battery_report(self.pj)['condition'])

    def test_profile_labels_preserve_model_identity(self):
        self.assertEqual(profile_label('BP7X_1820'), 'BP7X · 1820 mAh')
        self.assertEqual(profile_label('PJLIPO_2500'), 'PJLIPO · 2500 mAh')


if __name__ == '__main__': unittest.main()
