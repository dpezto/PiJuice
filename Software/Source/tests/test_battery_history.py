"""Replay telemetry traces; no hardware writes or real battery discharge."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pijuice_battery import BatteryHistory, history_report

BATTERY = {'battery': 'NORMAL', 'powerInput': 'NOT_PRESENT', 'powerInput5vIo': 'NOT_PRESENT'}
POWERED = dict(BATTERY, powerInput='PRESENT')
PROFILE = {'capacity': 1000, 'regulationVoltage': 4200}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'history.json')
        self.history = BatteryHistory(self.path)
        self.tick = 0

    def sample(self, soc, status=BATTERY, step=5, **kwargs):
        self.tick += step
        self.history.observe(status, kwargs.pop('profile', PROFILE), soc,
                             kwargs.pop('io_mv', 5000), kwargs.pop('io_ma', 540),
                             kwargs.pop('bat_mv', 4000), now=1700000000 + self.tick,
                             monotonic=self.tick, **kwargs)

    def discharge(self):
        # 750mA battery-side estimate for 0.8h = 600mAh over 60%.
        for t in range(0, 2881, 5):
            self.sample(80 - 60 * t / 2880)

    def test_partial_cycles_accumulate_without_counting_recharge(self):
        for start in (80, 75):
            self.sample(start, POWERED)
            for soc in range(start, start - 26, -1): self.sample(soc)
        self.assertAlmostEqual(self.history.state['cycles'], .5)

    def test_health_uses_independent_output_energy(self):
        self.discharge()
        state = self.history.state
        self.assertAlmostEqual(state['cycles'], .6)
        self.assertEqual(len(state['samples']), 1)
        self.assertAlmostEqual(state['samples'][0]['capacity_mah'], 1000)
        self.history.save(1700000000 + self.tick)
        with patch('pijuice_battery.time.time', return_value=1700000000 + self.tick):
            report = history_report(self.path)
        self.assertAlmostEqual(report['health_percent'], 100)
        self.assertTrue(report['active'])
        self.assertIn('rough', report['text'])

    def test_realistic_capacity_loss_is_not_clamped_or_hidden(self):
        for t in range(0, 2161, 5): self.sample(80 - 60 * t / 2160)
        self.assertAlmostEqual(self.history.state['samples'][0]['capacity_mah'], 750)

    def test_reboot_preserves_totals_but_never_integrates_downtime(self):
        for soc in range(80, 69, -1): self.sample(soc)
        self.history.save()
        self.history = BatteryHistory(self.path)
        self.sample(20, step=3600)
        self.assertAlmostEqual(self.history.state['cycles'], .1)
        self.assertEqual(self.history.state['samples'], [])

    def test_gap_interrupts_health_session_and_does_not_count_missing_charge(self):
        self.sample(80)
        self.sample(79)
        self.sample(30, step=90)
        for soc in range(29, 19, -1): self.sample(soc)
        self.assertEqual(self.history.state['samples'], [])
        self.assertAlmostEqual(self.history.state['cycles'], .11)

    def test_gauge_noise_does_not_double_count(self):
        for soc in (80, 79, 80, 79, 80, 79, 78): self.sample(soc)
        self.assertAlmostEqual(self.history.state['cycles'], .02)

    def test_gauge_jump_is_not_a_cycle(self):
        self.sample(90)
        self.sample(20)
        self.assertEqual(self.history.state['cycles'], 0)
        self.assertEqual(self.history.state['samples'], [])

    def test_powered_and_absent_battery_never_count(self):
        for status in (POWERED, dict(BATTERY, battery='NOT_PRESENT')):
            for soc in range(90, 10, -1): self.sample(soc, status)
        self.assertEqual(self.history.state['cycles'], 0)
        self.assertEqual(self.history.state['samples'], [])

    def test_unknown_input_state_is_not_assumed_discharging(self):
        for soc in range(80, 19, -1): self.sample(soc, {'battery': 'NORMAL'})
        self.assertEqual(self.history.state['cycles'], 0)

    def test_invalid_load_does_not_invent_capacity(self):
        for soc in range(80, 19, -1): self.sample(soc, io_ma=-1)
        self.assertAlmostEqual(self.history.state['cycles'], .6)
        self.assertEqual(self.history.state['samples'], [])

    def test_reset_and_profile_changes_archive_prior_battery(self):
        self.sample(80)
        self.sample(79)
        self.sample(78, reset_token='replacement')
        self.assertEqual(self.history.state['cycles'], 0)
        self.assertAlmostEqual(self.history.state['archives'][0]['cycles'], .01)
        self.sample(77, profile={'capacity': 2000}, reset_token='replacement')
        self.assertEqual(len(self.history.state['archives']), 2)
        self.assertEqual(self.history.state['design_capacity'], 2000)

    def test_corrupt_history_is_preserved(self):
        Path(self.path).write_text('{broken')
        with self.assertRaises(ValueError): BatteryHistory(self.path)
        self.assertEqual(Path(self.path).read_text(), '{broken')
        self.assertIn('unavailable', history_report(self.path)['text'])

    def test_sensor_failure_breaks_session(self):
        self.sample(80)
        self.sample(79)
        pj = Mock()
        pj.status.GetStatus.return_value = {'error': 'NO_CONNECTION'}
        with self.assertRaises(RuntimeError): self.history.sample(pj)
        self.assertIsNone(self.history.run)
        self.sample(20)
        self.assertAlmostEqual(self.history.state['cycles'], .01)

    def test_missing_and_stale_history_are_explicit(self):
        self.assertIsNone(history_report(self.path)['cycles'])
        self.sample(80)
        self.assertFalse(history_report(self.path)['active'])
        self.assertIn('Paused', history_report(self.path)['text'])

    def test_learned_capacity_feeds_power_supply_only_after_a_session(self):
        self.assertIsNone(self.history.capacity_mah())
        self.sample(80)
        self.assertIsNone(self.history.capacity_mah())
        self.history.state['samples'] = [{'capacity_mah': 1500}, {'capacity_mah': 1700}, {'capacity_mah': 1600}]
        self.assertEqual(self.history.capacity_mah(), 1600)

    def test_write_frequency_is_bounded_and_snapshot_readable(self):
        with patch.object(self.history, 'save', wraps=self.history.save) as save:
            for i in range(30): self.sample(80)
        self.assertEqual(save.call_count, 3)
        self.assertEqual(Path(self.path).stat().st_mode & 0o777, 0o644)
        self.assertEqual(json.loads(Path(self.path).read_text())['version'], 1)


if __name__ == '__main__': unittest.main()
