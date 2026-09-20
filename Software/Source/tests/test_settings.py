"""GTK interaction tests. Run with a virtual X display; no PiJuice hardware required.

GDK_BACKEND=x11 GSETTINGS_BACKEND=memory xvfb-run -a /usr/bin/python3 -m unittest discover -s Software/Source/tests -v
"""
import copy
import json
import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SOURCE), str(SOURCE / 'src')]
from pijuice import PiJuiceConfig
from pijuice_service import PiJuiceService, PiJuiceError
from pijuice_gtk import (Adw, Gtk, GLib, _View, PiJuiceWindow, LedView, BatteryView,
                         WakeupView, SystemTaskView, ButtonsView, IoView, FirmwareView)


class FakeService(PiJuiceService):
    def __init__(self, path):
        super().__init__(config_path=str(path), connect=False)
        self.pj = SimpleNamespace(config=PiJuiceConfig(None))
        self.firmware_version = {'version': '1.6'}
        self.calls = []
        self.failure = None
        self.queued = []
        self.hold = False
        self.alarm = {'day': 'EVERY_DAY', 'hour': 8, 'minute': 30, 'second': 0}
        self.led_config = {'function': 'CHARGE_STATUS', 'parameter': {'r': 12, 'g': 24, 'b': 36}}
        self.charging = True
        self.wakeup = False
        self.online = True

    def submit(self, fn, *args, **kwargs):
        future = Future()
        def run():
            try:
                future.set_result(fn(*args, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
        if self.hold:
            self.queued.append(run)
        else:
            run()
        return future

    def write(self, name, *args):
        self.calls.append((name, copy.deepcopy(args)))
        if self.failure:
            raise self.failure

    def connect(self):
        self.pj = SimpleNamespace(config=PiJuiceConfig(None)) if self.online else None
        return self.online

    def get_status(self):
        if not self.online:
            raise PiJuiceError('NO_CONNECTION')
        return {'battery': 'CHARGING_FROM_IN', 'powerInput': 'PRESENT', 'powerInput5vIo': 'NOT_PRESENT'}

    def get_charge_level(self): return 75
    def get_battery_voltage(self): return 4000
    def get_battery_temperature(self): return 24
    def get_io_voltage(self): return 5000
    def get_io_current(self): return 400
    def get_fault_status(self): return {}
    def get_system_power_switch(self): return 2100
    def get_led_config(self, led): return copy.deepcopy(self.led_config)
    def set_led_config(self, led, cfg): self.write('led', led, cfg)
    def get_button_config(self, button):
        return {event: {'function': 'NO_FUNC', 'parameter': 100} for event in self.button_events}
    def set_button_config(self, button, cfg): self.write('button', button, cfg)
    def get_battery_report(self):
        return {'condition': 'No battery faults reported', 'temperature': 24,
                'design_capacity': 1820, 'profile': {'chargeCurrent': 925, 'regulationVoltage': 4180}}
    def get_battery_profile_status(self): return {'profile': 'BP7X', 'validity': 'VALID', 'source': 'PREDEFINED'}
    def get_battery_temp_sense(self): return self.battery_temp_sense_options[0]
    def get_rsoc_estimation(self): return self.rsoc_estimation_options[0]
    def get_charging_config(self): return {'charging_enabled': self.charging}
    def set_system_power_switch(self, value): self.write('switch', value)
    def set_charging_config(self, state):
        self.write('charging', state)
        self.charging = state
    def get_io_config(self, pin): return {'mode': 'NOT_USED', 'pull': 'NOPULL'}
    def set_io_config(self, pin, cfg): self.write('io', pin, cfg)
    def get_alarm(self): return copy.deepcopy(self.alarm)
    def get_alarm_control(self): return {'alarm_wakeup_enabled': self.wakeup}
    def get_rtc_time(self): return dict(year=2026, month=9, day=17, hour=8, minute=0, second=0)
    def set_alarm(self, alarm):
        self.write('alarm', alarm)
        self.alarm = alarm
    def set_wakeup_enabled(self, state):
        self.write('wakeup', state)
        self.wakeup = state
    def retry_notify(self): return 0


def drain():
    context = GLib.MainContext.default()
    for _ in range(1000):
        if not context.pending():
            break
        context.iteration(False)


class SettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Adw.init()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = FakeService(Path(self.tmp.name) / 'config.json')
        self.views = []

    def tearDown(self):
        for view in self.views:
            view.dispose_view()
        self.service.close()
        drain()
        self.tmp.cleanup()

    def view(self, cls):
        view = cls(self.service)
        self.views.append(view)
        drain()
        return view

    def test_all_pages_build_connected_and_offline(self):
        for online in (True, False):
            self.service.online = online
            self.service.connect()
            window = PiJuiceWindow(self.service)
            drain()
            self.assertEqual(window.stack.get_pages().get_n_items(), 10)
            self.assertTrue(all(not v.dirty for v in window._views()))
            window._cleanup()
            window.destroy()

    def test_led_background_read_preserves_draft_and_discard_restores(self):
        view = self.view(LedView)
        self.assertEqual(view._rows['D2']['r'].get_value_as_int(), 12)
        view._rows['D1']['r'].set_value(99)
        self.assertTrue(view.dirty)
        self.assertTrue(view._actions.get_visible())
        self.assertEqual(view._rows['D1']['function'].get_selected(), 2)  # Custom colour
        view.refresh()
        drain()
        self.assertEqual(view._rows['D1']['r'].get_value_as_int(), 99)
        view._discard()
        drain()
        self.assertEqual(view._rows['D1']['r'].get_value_as_int(), 12)
        self.assertFalse(view.dirty)

    def test_failed_write_keeps_draft_and_reenables_form(self):
        view = self.view(LedView)
        view._rows['D1']['r'].set_value(99)
        self.service.failure = OSError('disk or device unavailable')
        view._on_apply(None)
        drain()
        self.assertTrue(view.dirty)
        self.assertTrue(view.get_sensitive())
        self.assertIn('retry', view._status.get_text())
        self.service.failure = None
        view._on_apply(None)
        drain()
        self.assertFalse(view.dirty)
        self.assertEqual([c[1][0] for c in self.service.calls[-2:]], ['D1', 'D2'])

    def test_duplicate_write_is_not_submitted(self):
        view = self.view(LedView)
        self.service.hold = True
        view._on_apply(None)
        view._on_apply(None)
        self.assertEqual(len(self.service.queued), 1)
        self.service.queued.pop()()
        drain()
        self.assertTrue(view.get_sensitive())

    def test_alarm_rejects_invalid_fields_without_writing(self):
        view = self.view(WakeupView)
        self.assertEqual(self.service.calls, [])  # loading must never enable wakeup
        view._minute.set_text('oops')
        with self.assertRaises(ValueError):
            view._on_set_alarm(None)
        self.assertEqual(self.service.calls, [])
        view._minute.set_text('61')
        with self.assertRaises(ValueError):
            view._on_set_alarm(None)
        self.assertTrue(view._minute.has_css_class('error'))
        view._minute.set_text('45')
        view._on_set_alarm(None)
        drain()
        self.assertEqual(self.service.alarm['minute'], 45)
        self.assertFalse(view.dirty)
        self.assertFalse(self.service.wakeup)

    def test_alarm_multiple_values_and_ampm_round_trip(self):
        view = self.view(WakeupView)
        view._every_day.set_active(False)
        view._daytype.set_selected(1)
        view._day.set_text('2;4;6')
        view._hour.set_text('8 AM;12PM;6PM')
        view._on_set_alarm(None)
        drain()
        self.assertEqual(self.service.alarm['weekday'], '2;4;6')
        self.assertEqual(self.service.alarm['hour'], '8;12;18')

    def test_service_reload_failure_is_distinct_from_save_failure(self):
        view = self.view(SystemTaskView)
        self.service.retry_notify = lambda: -1
        view._enabled.set_active(True)
        view._applying = True
        view._on_apply(None)
        view._applying = False
        drain()
        self.assertFalse(view.dirty)
        self.assertTrue(view._retry_notify.get_visible())
        self.assertIn('Saved, but', view._status.get_text())
        self.assertTrue(json.loads(Path(self.service.config_path).read_text())['system_task']['enabled'])

    def test_immediate_switch_failure_rolls_back_and_preserves_other_draft(self):
        view = self.view(WakeupView)
        view._minute.set_text('42')
        self.service.failure = PiJuiceError('NO_CONNECTION')
        view._enabled.set_active(True)
        drain()
        self.assertFalse(view._enabled.get_active())
        self.assertTrue(view.dirty)
        self.service.failure = None
        view._enabled.set_active(True)
        drain()
        self.assertTrue(view._enabled.get_active())
        self.assertTrue(view.dirty)
        self.assertEqual(view._minute.get_text(), '42')

    def test_buttons_validate_all_before_first_write(self):
        view = self.view(ButtonsView)
        list(view._cells.values())[-1][1].set_text('bad')
        self.assertTrue(view.dirty)
        with self.assertRaises(ValueError):
            view._on_apply(None)
        self.assertEqual(self.service.calls, [])

    def test_task_validation_does_not_mutate_shared_config(self):
        view = self.view(SystemTaskView)
        original = copy.deepcopy(self.service.config)
        view._enabled.set_active(True)
        view._rows['watchdog'][0].set_active(True)
        view._rows['watchdog'][1].set_text('invalid')
        with self.assertRaises(ValueError):
            view._on_apply(None)
        self.assertEqual(self.service.config, original)

    def test_io_dynamic_fields_are_tracked_and_validated(self):
        view = self.view(IoView)
        pin = view._pins[1]
        pin['mode'].set_selected(pin['modes'].index('PWM_OUT_PUSHPULL'))
        fields = {cfg['name']: widget for cfg, widget in pin['params']}
        fields['period'].set_text('1000')
        fields['duty_cycle'].set_text('nan')
        with self.assertRaises(ValueError):
            view._on_apply(None)
        self.assertEqual(self.service.calls, [])
        fields['duty_cycle'].set_text('50')
        view._on_apply(None)
        drain()
        self.assertFalse(view.dirty)
        fields['duty_cycle'].set_text('60')
        self.assertTrue(view.dirty)

    def test_reconnect_builds_missing_pages_and_retains_draft(self):
        self.service.online = False
        self.service.connect()
        window = PiJuiceWindow(self.service)
        drain()
        scripts = window.stack.get_child_by_name('userscripts')
        scripts._entries['USER_FUNC1'].set_text('/tmp/draft')
        self.service.online = True
        window._check_connection()
        drain()
        self.assertTrue(window.stack.get_child_by_name('wakeup')._built_available)
        self.assertTrue(scripts.dirty)
        window._cleanup()
        window.destroy()

    def test_firmware_failure_allows_retry(self):
        view = self.view(FirmwareView)
        view._bin_file = '/tmp/test-firmware.bin'
        def fail(_p): raise PiJuiceError('PAGE_VERIFY_ERROR (verify failed 11)', 'firmware')
        self.service.flash_firmware = fail
        view._do_flash(None)
        drain()
        self.assertTrue(view._update_btn.get_sensitive())
        self.assertFalse(view._spinner.get_spinning())
        self.assertIn('retry', view._status.get_text())

    def test_preview_restores_saved_configuration_and_keeps_draft(self):
        view = self.view(LedView)
        view._rows['D1']['r'].set_value(99)
        with patch('time.sleep'):
            view._on_test(None, 'D1')
        drain()
        self.assertEqual(self.service.calls[-1][1][1], self.service.led_config)
        self.assertTrue(view.dirty)

    def test_charge_limit_is_immediate_and_preserves_profile_draft(self):
        view = self.view(BatteryView)
        view._profile.set_selected(1)
        view._limit.set_active(True)
        drain()
        self.assertTrue(view._limit.get_active())
        self.assertFalse(view._charging.get_sensitive())
        self.assertTrue(view.dirty)
        saved = json.loads(Path(self.service.config_path).read_text())
        self.assertEqual(saved['battery_management'], {'enabled': True, 'limit': 80, 'resume': 75})

    def test_status_switch_choice_is_not_a_draft_and_dialogs_do_not_stack(self):
        from pijuice_gtk import StatusView
        view = self.view(StatusView)
        view._switch.set_selected(0)
        self.assertFalse(view.dirty)
        view._on_set_switch(None)
        view._on_set_switch(None)
        self.assertIsNotNone(view._dialog)
        view._dialog.emit('response', 'cancel')
        self.assertIsNone(view._dialog)
        self.assertEqual([c for c in self.service.calls if c[0] == 'switch'], [])

    def test_atomic_save_failure_preserves_file_and_memory(self):
        self.service.save_section('system_task', {'enabled': False})
        before = Path(self.service.config_path).read_text()
        with patch('pijuice_service.os.replace', side_effect=OSError('full')):
            with self.assertRaises(OSError):
                self.service.save_section('system_task', {'enabled': True})
        self.assertEqual(Path(self.service.config_path).read_text(), before)
        self.assertFalse(self.service.config['system_task']['enabled'])
        self.assertEqual(len(list(Path(self.tmp.name).iterdir())), 1)


if __name__ == '__main__':
    unittest.main()
