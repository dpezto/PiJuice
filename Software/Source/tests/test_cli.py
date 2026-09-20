"""Terminal interaction regressions; no display or hardware needed."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SOURCE), str(SOURCE / 'src')]
import pijuice_cli as cli
import urwid


def ok(data=None):
    return {'error': 'NO_ERROR', 'data': data}


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cli.PiJuiceConfigDataPath = str(Path(self.tmp.name) / 'config.json')
        self.original = {'system_task': {'enabled': False}, 'user_functions': {'USER_FUNC1': ''}, 'cli_settings': {}}
        Path(cli.PiJuiceConfigDataPath).write_text(json.dumps(self.original))
        cli.pijuiceConfigData = copy.deepcopy(self.original)
        cli._dirty = cli._in_dialog = cli._suppress_hoist = False
        cli._drafts = {}
        cli._errors = {}
        cli._baseline = cli._active_tab = cli._current_back = None
        cli._last_choice = None
        cli._location = 'Settings'
        cli._notice = ('muted', 'Ready')
        cli.current_fw_version = 0x16
        cli.frame = cli.linebox = None
        cli.main = cli._ContentArea(cli.menu('Settings', cli.choices), left=1, right=1)
        cli.frame = urwid.Frame(cli.main)
        cli.loop = Mock()
        cli.loop.widget = cli.frame
        cli._render_header()
        self.config = Mock()
        self.config.GetLedConfiguration.return_value = ok({'function': 'CHARGE_STATUS', 'parameter': dict(r=10, g=20, b=30)})
        self.config.SetLedConfiguration.return_value = ok()
        self.config.GetFirmwareVersion.return_value = ok({'version': '1.6'})
        self.config.GetButtonConfiguration.return_value = ok({event: {'function': 'NO_FUNC', 'parameter': 100} for event in cli.ButtonsTab.EVENTS})
        self.config.SetButtonConfiguration.return_value = ok()
        self.config.GetIoConfiguration.return_value = ok({'mode': 'NOT_USED', 'pull': 'NOPULL'})
        self.config.SetIoConfiguration.return_value = ok()
        self.rtc = Mock()
        self.rtc.GetAlarm.return_value = ok({'day': 'EVERY_DAY', 'hour': 8, 'minute': 30, 'second': 0})
        self.rtc.GetControlStatus.return_value = ok({'alarm_wakeup_enabled': False, 'alarm_flag': False})
        self.rtc.GetTime.return_value = ok(dict(year=2026, month=9, day=17, hour=8, minute=0, second=0))
        self.rtc.SetAlarm.return_value = self.rtc.SetWakeupEnabled.return_value = ok()
        power = Mock()
        power.GetWatchdog.return_value = {'error': 'NO_ERROR', 'non_volatile': False}
        power.GetWakeUpOnCharge.return_value = {'error': 'NO_ERROR', 'non_volatile': False}
        power.GetSystemPowerSwitch.return_value = ok(2100)
        status = Mock()
        status.GetStatus.return_value = ok(dict(battery='CHARGING_FROM_IN',powerInput='PRESENT',powerInput5vIo='NOT_PRESENT'))
        for name,value in [('GetChargeLevel',74),('GetBatteryVoltage',4000),('GetBatteryTemperature',24),('GetFaultStatus',{})]:
            getattr(status,name).return_value = ok(value)
        cli.pijuice = SimpleNamespace(config=self.config, rtcAlarm=self.rtc, power=power,status=status)
        self.init_patch = patch.object(cli, '_InitPiJuiceInterface')
        self.init_patch.start()

    def tearDown(self):
        self.init_patch.stop()
        self.tmp.cleanup()

    def click(self, label):
        for widget in cli._walk_widgets(cli.main.original_widget):
            if isinstance(widget, urwid.Button) and widget.label == label:
                urwid.emit_signal(widget, 'click', widget)
                return
        self.fail('No button: ' + label)

    def test_menu_and_forms_render_at_common_sizes(self):
        for choice in ('Status','LEDs','Buttons','IO','Battery care','Wakeup Alarm','System Task','System Events','User Scripts','Settings'):
            cli.item_chosen(choice)
            self.assertIsNotNone(cli._active_tab, choice + ': ' + str(cli._notice))
            for size in ((64,12),(80,24),(120,36)):
                cli.frame.render(size, focus=True)
            cli.main_menu()
        screen = cli.ResponsiveScreen(cli.frame)
        screen.render((40,8))

    def test_custom_profile_rejects_invalid_values_before_any_write(self):
        tab = cli.BatteryProfileTab.__new__(cli.BatteryProfileTab)
        tab.custom_values = True
        tab.chemistries_idx = 0
        values = [1820, 925, 50, 4180, 3000, 0, 10, 45, 60, 3380, 10000,
                  3600, 3800, 4100, 0.1, 0.1, 0.1]
        tab.param_edits = [urwid.Edit(edit_text=str(v)) for v in values]
        profile, _ext = tab._validated_custom_values()
        self.assertEqual(profile['chargeCurrent'], 925)
        tab.param_edits[1].set_edit_text('999')
        with self.assertRaisesRegex(ValueError, 'steps'):
            tab._apply_settings()
        self.config.SetBatteryTempSenseConfig.assert_not_called()
        self.config.SetCustomBatteryProfile.assert_not_called()

    def test_battery_limit_toggle_saves_only_its_policy(self):
        cli.item_chosen('Battery care')
        checkbox = next(w for w in cli._walk_widgets(cli.main.original_widget) if isinstance(w,urwid.CheckBox))
        with patch.object(cli,'notify_service',return_value=0):
            checkbox.set_state(True)
        saved = json.loads(Path(cli.PiJuiceConfigDataPath).read_text())
        self.assertEqual(saved['battery_management'], {'enabled':True, 'limit':80, 'resume':75})
        self.assertEqual(saved['system_task'], self.original['system_task'])

    def test_numeric_input_never_silently_clamps(self):
        for value in ('', 'abc', '-1', '256', 'nan'):
            with self.assertRaises(ValueError):
                cli.validate_value(value, 'int', 0, 255, 20)
        self.assertEqual(cli.validate_value('200', 'int', 0, 255, 20), '200')

    def test_led_invalid_input_blocks_apply_without_losing_text(self):
        cli.item_chosen('LEDs')
        tab = cli._active_tab
        tab.configure_led(None,0)
        edit = next(w for w in cli._walk_widgets(cli.main.original_widget) if isinstance(w,urwid.Edit))
        edit.set_edit_text('999')
        cli.apply_draft()
        self.config.SetLedConfiguration.assert_not_called()
        self.assertEqual(edit.edit_text,'999')
        edit.set_edit_text('100')
        self.assertFalse(cli._errors)
        cli.go_back()
        cli.apply_draft()
        self.assertEqual(self.config.SetLedConfiguration.call_count,2)

    def test_nested_back_and_menu_preserve_led_draft(self):
        cli.item_chosen('LEDs')
        tab = cli._active_tab
        tab.configure_led(None,0)
        edit = next(w for w in cli._walk_widgets(cli.main.original_widget) if isinstance(w,urwid.Edit))
        edit.set_edit_text('99')
        cli.go_back()
        self.assertTrue(cli._dirty)
        cli.go_back()
        self.assertIn('LEDs',cli._drafts)
        cli.item_chosen('LEDs')
        self.assertIs(cli._active_tab,tab)
        self.assertEqual(tab.current_config[0]['color'][0],'99')

    def test_saving_one_json_section_does_not_save_another_draft(self):
        cli.item_chosen('User Scripts')
        edit = next(w for w in cli._walk_widgets(cli.main.original_widget) if isinstance(w,urwid.Edit))
        edit.set_edit_text('/tmp/not-yet-a-script')
        cli.go_back()
        cli.item_chosen('System Events')
        with patch.object(cli,'notify_service',return_value=0):
            cli.savePiJuiceConfig()
        saved=json.loads(Path(cli.PiJuiceConfigDataPath).read_text())
        self.assertEqual(saved['user_functions']['USER_FUNC1'],'')
        self.assertIn('User Scripts',cli._drafts)
        self.assertEqual(cli.pijuiceConfigData['user_functions']['USER_FUNC1'],'/tmp/not-yet-a-script')

    def test_failed_save_preserves_page_and_draft(self):
        cli.item_chosen('System Events')
        cli.pijuiceConfigData['system_events']['low_charge']['enabled']=True
        cli._dirty=True
        before=cli.main.original_widget
        with patch.object(cli,'_service_save_config',side_effect=OSError('full')):
            cli.savePiJuiceConfig()
        self.assertIs(cli.main.original_widget,before)
        self.assertTrue(cli._dirty)
        self.assertIn('Your edits are kept',cli._notice[1])

    def test_alarm_validation_and_multiple_hours(self):
        cli.item_chosen('Wakeup Alarm')
        tab=cli._active_tab
        tab.current_config['minute']['value']='61'
        with self.assertRaises(ValueError): tab._set_alarm()
        self.rtc.SetAlarm.assert_not_called()
        tab.current_config['minute']['value']='30'
        tab.current_config['hour']['value']='8AM;12PM;6PM'
        tab._set_alarm()
        self.assertEqual(self.rtc.SetAlarm.call_args.args[0]['hour'],'8;12;18')
        self.rtc.SetWakeupEnabled.assert_not_called()
        self.rtc.ClearAlarmFlag.assert_not_called()

    def test_wakeup_disable_failure_restores_enabled_state(self):
        cli.item_chosen('Wakeup Alarm')
        tab=cli._active_tab
        tab.current_config['enabled']=True
        checkbox=urwid.CheckBox('Wakeup',state=True)
        self.rtc.SetWakeupEnabled.return_value={'error':'NO_CONNECTION'}
        tab._toggle_wakeup(checkbox,False)
        self.assertTrue(checkbox.state)
        self.assertTrue(tab.current_config['enabled'])

    def test_failed_led_write_never_reports_success(self):
        cli.item_chosen('LEDs')
        self.config.SetLedConfiguration.return_value={'error':'WRITE_FAILED'}
        self.click('Apply settings')
        self.assertEqual(cli._notice[0],'error')
        self.assertIn('WRITE_FAILED',cli._notice[1])

    def test_service_reload_failure_does_not_lose_saved_state(self):
        cli.item_chosen('System Events')
        with patch.object(cli,'notify_service',return_value=-1): cli.savePiJuiceConfig()
        self.assertFalse(cli._dirty)
        self.assertIn('F8',cli._notice[1])
        with patch.object(cli,'notify_service',return_value=0): cli.retry_service_reload()
        self.assertEqual(cli._notice[0],'ok')

    def test_refresh_does_not_overwrite_draft(self):
        cli.item_chosen('User Scripts')
        cli.pijuiceConfigData['user_functions']['USER_FUNC1']='draft'
        cli._dirty=True
        self.click('Refresh')
        self.assertEqual(cli.pijuiceConfigData['user_functions']['USER_FUNC1'],'draft')

    def test_quit_can_be_cancelled_with_drafts_intact(self):
        cli.item_chosen('User Scripts')
        cli._dirty=True
        cli.exit_program()
        self.assertTrue(cli._in_dialog)
        cli._dialog_cancel()
        self.assertFalse(cli._in_dialog)
        self.assertTrue(cli._dirty)


if __name__ == '__main__': unittest.main()
