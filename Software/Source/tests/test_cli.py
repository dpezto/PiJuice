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
import pijuice_service
from pijuice_service import PiJuiceService
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
        cli.frame = cli.linebox = None
        cli.main = cli._ContentArea(cli.menu(cli.choices), left=1, right=1)
        cli.frame = urwid.Frame(cli.main)
        cli.loop = Mock()
        cli.loop.widget = cli.frame
        cli._render_header()
        self.config = Mock()
        self.config.GetLedConfiguration.return_value = ok({'function': 'CHARGE_STATUS', 'parameter': dict(r=10, g=20, b=30)})
        self.config.SetLedConfiguration.return_value = ok()
        self.config.GetFirmwareVersion.return_value = ok({'version': '1.6'})
        self.config.GetButtonConfiguration.return_value = ok({event: {'function': 'NO_FUNC', 'parameter': 100} for event in cli.PiJuiceConfig.buttonEvents})
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
        cli.service = PiJuiceService(config_path=cli.PiJuiceConfigDataPath, connect=False)
        cli.service.pj = SimpleNamespace(config=self.config, rtcAlarm=self.rtc, power=power, status=status)
        cli.service.firmware_version = {'version': '1.6'}

    def tearDown(self):
        cli.service.close()
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
        with patch.object(pijuice_service,'notify_service',return_value=0):
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
        edit = next(w for w in cli._walk_widgets(cli.main.original_widget) if isinstance(w,urwid.Edit) and w.caption == 'R: ')
        edit.set_edit_text('999')
        cli.apply_draft()
        self.config.SetLedConfiguration.assert_not_called()
        self.assertEqual(edit.edit_text,'999')
        edit.set_edit_text('100')
        self.assertFalse(cli._errors)
        cli.go_back()
        cli.apply_draft()
        self.assertEqual(self.config.SetLedConfiguration.call_count,2)

    def test_led_white_point_hex_and_swatch(self):
        cli.item_chosen('LEDs')
        cli._active_tab.configure_led(None, 1)
        edits = {w.caption.strip(': '): w for w in cli._walk_widgets(cli.main.original_widget) if isinstance(w, urwid.Edit)}
        self.assertEqual(edits['Hex'].edit_text, '#0a141e')
        edits['Hex'].set_edit_text('#ff8000')                       # hex drives the channels
        self.assertEqual([edits[c].edit_text for c in 'RGB'], ['255', '128', '0'])
        edits['G'].set_edit_text('64')                              # and the channels drive the hex
        self.assertEqual(edits['Hex'].edit_text, '#ff4000')
        cli._screen_colors = 2 ** 24
        cli._active_tab._paint_swatch([255, 64, 0])
        self.assertEqual(cli.loop.screen.register_palette_entry.call_args.args[-1], '#ff4000')
        edits['White point R,G,B'].set_edit_text('60,999,60')
        cli.apply_draft()
        self.config.SetLedConfiguration.assert_not_called()
        edits['White point R,G,B'].set_edit_text('60,100,60')
        with patch.object(pijuice_service, 'notify_service', return_value=0):
            cli.go_back()
            cli.apply_draft()
        saved = json.loads(Path(cli.PiJuiceConfigDataPath).read_text())
        self.assertEqual(saved['led_white'], {'D1': [255, 255, 255], 'D2': [60, 100, 60]})
        self.assertEqual(self.config.SetLedConfiguration.call_args.args[1]['parameter'], {'r': 60, 'g': 25, 'b': 0})
        cli.main_menu()

    def test_nested_back_and_menu_preserve_led_draft(self):
        cli.item_chosen('LEDs')
        tab = cli._active_tab
        tab.configure_led(None,0)
        edit = next(w for w in cli._walk_widgets(cli.main.original_widget) if isinstance(w,urwid.Edit) and w.caption == 'R: ')
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
        edit = next(w for w in cli._walk_widgets(cli.main.original_widget) if isinstance(w,cli.ScriptEdit))
        edit.set_edit_text('/tmp/not-yet-a-script')
        cli.go_back()
        cli.item_chosen('System Events')
        with patch.object(pijuice_service,'notify_service',return_value=0):
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
        with patch.object(pijuice_service,'save_config',side_effect=OSError('full')):
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
        saved = json.loads(Path(cli.PiJuiceConfigDataPath).read_text())
        self.assertEqual(saved['wakeup_alarm']['alarm']['hour'], '8;12;18')  # kept for the daemon's boot re-arm

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
        with patch.object(pijuice_service,'notify_service',return_value=-1): cli.savePiJuiceConfig()
        self.assertFalse(cli._dirty)
        self.assertIn('F8',cli._notice[1])
        with patch.object(pijuice_service,'notify_service',return_value=0): cli.retry_service_reload()
        self.assertEqual(cli._notice[0],'ok')

    def test_refresh_does_not_overwrite_draft(self):
        cli.item_chosen('User Scripts')
        cli.pijuiceConfigData['user_functions']['USER_FUNC1']='draft'
        cli._dirty=True
        self.click('Refresh')
        self.assertEqual(cli.pijuiceConfigData['user_functions']['USER_FUNC1'],'draft')

    def test_profile_apply_stops_at_first_failed_write(self):
        self.config.GetBatteryProfileStatus.return_value = ok({'validity': 'VALID', 'origin': 'PREDEFINED', 'profile': 'BP7X', 'source': 'HOST'})
        self.config.GetBatteryProfile.return_value = ok(dict(capacity=1820, chargeCurrent=925, terminationCurrent=50, regulationVoltage=4180, cutoffVoltage=3000, tempCold=0, tempCool=10, tempWarm=45, tempHot=60, ntcB=3380, ntcResistance=10000))
        self.config.GetBatteryExtProfile.return_value = ok(dict(chemistry='LIPO', ocv10=3600, ocv50=3800, ocv90=4100, r10=.1, r50=.1, r90=.1))
        self.config.GetBatteryTempSenseConfig.return_value = ok('AUTO_DETECT')
        self.config.GetRsocEstimationConfig.return_value = ok('AUTO_DETECT')
        self.config.batteryProfiles = ['BP7X']
        self.config.SetBatteryTempSenseConfig.return_value = {'error': 'WRITE_FAILED'}
        cli.item_chosen('Battery profile')
        self.click('Apply settings')
        self.assertEqual(cli._notice[0], 'error')
        self.config.SetBatteryProfile.assert_not_called()

    def test_i2c_address_change_is_persisted(self):
        self.config.GetRunPinConfig.return_value = ok('NOT_INSTALLED')
        self.config.GetAddress.side_effect = lambda slave: ok('14' if slave == 1 else '68')
        self.config.GetIdEepromAddress.return_value = ok('52')
        self.config.GetIdEepromWriteProtect.return_value = ok(False)
        self.config.GetPowerInputsConfig.return_value = ok(dict(precedence='5V_GPIO', gpio_in_enabled=True, usb_micro_current_limit='2.5A', usb_micro_dpm='4.20V', no_battery_turn_on=False))
        self.config.GetPowerRegulatorMode.return_value = ok('POWER_SOURCE_DETECTION')
        self.config.GetChargingConfig.return_value = ok({'charging_enabled': True})
        self.config.SetAddress.return_value = ok()
        cli.item_chosen('General')
        cli._active_tab.current_config['i2c_addr'] = '15'
        with patch.object(pijuice_service, 'notify_service', return_value=0):
            self.click('Apply settings')
        self.config.SetAddress.assert_called_once_with(1, '15')
        saved = json.loads(Path(cli.PiJuiceConfigDataPath).read_text())
        self.assertEqual(saved['board']['general']['i2c_addr'], '15')
        self.assertEqual(saved['system_task'], self.original['system_task'])
        cli._active_tab.current_config['i2c_addr'] = 'zz'
        self.click('Apply settings')
        self.config.SetAddress.assert_called_once()

    def test_user_script_names_are_saved_and_shown(self):
        cli.item_chosen('User Scripts')
        edits = [w for w in cli._walk_widgets(cli.main.original_widget) if isinstance(w, urwid.Edit)]
        edits[0].set_edit_text('Backup')           # name of slot 1
        edits[1].set_edit_text(str(Path(__file__)))  # an existing absolute path
        with patch.object(pijuice_service, 'notify_service', return_value=0):
            cli.savePiJuiceConfig()
        saved = json.loads(Path(cli.PiJuiceConfigDataPath).read_text())
        self.assertEqual(saved['user_function_names'], {'USER_FUNC1': 'Backup'})
        self.assertEqual(saved['user_functions']['USER_FUNC1'], str(Path(__file__)))
        cli.main_menu()
        self.config.GetButtonConfiguration.return_value = ok({e: {'function': 'USER_FUNC1' if e == 'PRESS' else 'UNKNOWN', 'parameter': 100} for e in cli.PiJuiceConfig.buttonEvents})
        cli.item_chosen('Buttons')
        cli._active_tab.configure_sw(None, 'SW1')
        labels = [w.label for w in cli._walk_widgets(cli.main.original_widget) if isinstance(w, urwid.Button)]
        self.assertTrue(any('Backup' in l for l in labels), labels)
        self.assertFalse(any('_' in l for l in labels), labels)
        cli._active_tab._set_function(None, {'sw_id': 'SW1', 'action': 'RELEASE'})  # UNKNOWN must not crash (#998)
        self.assertTrue(cli._active_tab.bgroup[0].state)

    def test_keys_back_and_quit_are_consistent(self):
        cli.VIM_ENABLED = False
        cli.item_chosen('LEDs')
        cli._active_tab.configure_led(None, 0)
        self.assertTrue(any(isinstance(w, urwid.Edit) for w in cli._walk_widgets(cli.main.original_widget)))
        cli.input_filter(['q'], [])                       # q = back inside a section (LED D1 -> LED list)
        self.assertIsNotNone(cli._active_tab)
        self.assertFalse(any(isinstance(w, urwid.Edit) for w in cli._walk_widgets(cli.main.original_widget)))
        cli.input_filter(['backspace'], [])               # backspace = back to the menu
        self.assertIsNone(cli._active_tab)
        with self.assertRaises(urwid.ExitMainLoop):
            cli.input_filter(['q'], [])                   # q at the main menu = quit
        cli.input_filter(['?'], [])                       # keys screen, Esc returns
        self.assertIn('Keys', str(cli.main.original_widget.body[0].get_text()[0]))
        cli.input_filter(['esc'], [])
        self.assertIsNone(cli._active_tab)
        for i, row in enumerate(cli.main.original_widget.body):   # Right on a menu entry opens it
            if any(isinstance(b, urwid.Button) and b.label == 'LEDs' for b in cli._walk_widgets(row)):
                cli.main.original_widget.set_focus(i)
        self.assertEqual(cli.input_filter(['right'], []), [])
        self.assertIsInstance(cli._active_tab, cli.LEDTab)
        top = cli.ResponsiveScreen(urwid.Overlay(cli.linebox or urwid.LineBox(cli.frame), urwid.SolidFill(' '), 'center', 78, 'middle', 24))
        cli.loop.widget = top                              # the real stack must not hide the focus
        self.assertIsInstance(cli._focus_leaf(cli.frame), urwid.Button)
        header = cli.frame.header.contents[0][0].base_widget.get_text()[0]
        self.assertEqual(header, 'PiJuice HAT Configuration › LEDs')
        cli._active_tab.configure_led(None, 0)
        header = cli.frame.header.contents[0][0].base_widget.get_text()[0]
        self.assertEqual(header, 'PiJuice HAT Configuration › LEDs › LED D1')
        cli.input_filter(['left'], [])
        listbox = cli.main.original_widget.original_widget   # LED list is padded
        for i, row in enumerate(listbox.body):
            if any(isinstance(b, urwid.Button) and b.label == 'Apply settings' for b in cli._walk_widgets(row)):
                listbox.set_focus(i)
        cli.input_filter(['right'], [])                   # Right on an action button does nothing
        self.assertEqual(self.config.SetLedConfiguration.call_count, 0)
        cli.input_filter(['left'], [])                    # Left closes: back to the menu
        self.assertIsNone(cli._active_tab)
        cli.item_chosen('System Events')                  # rows with fields: Left/Right/Tab walk them
        cli.pijuiceConfigData['system_events']['low_charge']['enabled'] = True
        cli._active_tab.main()
        cli.main.original_widget.set_focus(3)
        self.assertEqual(cli.input_filter(['tab'], []), ['right'])
        self.assertEqual(cli.input_filter(['right'], []), ['right'])   # checkbox -> function button
        cli.main.original_widget.body[3].focus_position = 1
        self.assertEqual(cli.input_filter(['tab'], []), ['down'])
        self.assertEqual(cli.input_filter(['shift tab'], []), ['left'])
        self.assertEqual(cli.input_filter(['left'], []), ['left'])     # back across the row first
        cli.main.original_widget.body[3].focus_position = 0
        cli.input_filter(['left'], [])                    # at the row's start: back to the menu
        self.assertIsNone(cli._active_tab)
        cli.item_chosen('Buttons')                        # back lands where you left, not at the top
        listbox = cli.main.original_widget
        for i, row in enumerate(listbox.body):
            if any(isinstance(b, urwid.Button) and b.label == 'SW3' for b in cli._walk_widgets(row)):
                listbox.set_focus(i)
        cli.input_filter(['right'], [])                   # open SW3
        self.assertIn('SW3', str(cli._screen_title()))
        cli.go_back()
        listbox = cli.main.original_widget
        focused = [b.label for b in cli._walk_widgets(listbox.body[listbox.focus_position]) if isinstance(b, urwid.Button)]
        self.assertEqual(focused, ['SW3'])
        cli.main_menu()
        cli.item_chosen('User Scripts')
        edit = next(w for w in cli._walk_widgets(cli.main.original_widget) if isinstance(w, urwid.Edit))
        cli.main.original_widget.set_focus(4)             # first script row
        self.assertEqual(cli.input_filter(['q'], []), ['q'])  # typing q into a field stays typing
        cli.VIM_ENABLED = True
        cli._vim_mode = 'normal'
        self.assertEqual(cli.input_filter(['l'], []), ['right'])  # vim l: name field -> path field in the row
        cli.main.original_widget.body[4].focus_position = 1
        cli.input_filter(['h'], [])                       # vim h at the row's start: back
        self.assertIsNone(cli._active_tab)
        cli.VIM_ENABLED = False

    def test_vim_motions_on_rows_and_in_fields(self):
        cli.VIM_ENABLED = True
        cli._vim_mode = 'normal'
        cli.item_chosen('System Events')                  # row of [checkbox, function ›]: 0/$/w/b hop fields
        cli.pijuiceConfigData['system_events']['low_charge']['enabled'] = True
        cli._active_tab.main()
        listbox = cli.main.original_widget
        listbox.set_focus(3)
        row = listbox.body[3]
        cli.input_filter(['$'], [])
        self.assertEqual(row.focus_position, 1)
        cli.input_filter(['0'], [])
        self.assertEqual(row.focus_position, 0)
        self.assertEqual(cli.input_filter(['w'], []), ['right'])
        row.focus_position = 1
        self.assertEqual(cli.input_filter(['b'], []), ['left'])
        cli.main_menu()
        cli.item_chosen('User Scripts')                   # inside a field the same keys move the cursor
        listbox = cli.main.original_widget
        listbox.set_focus(4)
        row = listbox.body[4]
        edit = next(w for w in cli._walk_widgets(row) if isinstance(w, cli.ScriptEdit))
        row.focus_position = 2
        edit.set_edit_text('/usr/local/bin/x.sh'); edit.set_edit_pos(0)
        self.assertEqual(cli.input_filter(['w'], []), [])
        self.assertEqual(edit.edit_pos, 1)
        cli.input_filter(['e', 'x'], [])
        self.assertEqual(edit.edit_text, '/us/local/bin/x.sh')
        self.assertEqual(cli.input_filter(['backspace'], []), [])   # normal mode never edits by accident
        cli.input_filter(['A'], [])
        self.assertEqual((cli._vim_mode, edit.edit_pos), ('insert', len(edit.edit_text)))
        cli.input_filter(['esc'], [])
        self.assertEqual(cli._vim_mode, 'normal')
        cli.input_filter(['$', 'b', 'b', 'b'], [])
        self.assertEqual(edit.edit_text[edit.edit_pos:], 'x.sh')
        cli.VIM_ENABLED = False
        cli.main_menu()

    def test_notices_expire_and_chrome_is_quiet(self):
        cli.loop.set_alarm_in.reset_mock()
        cli._flash('Terminal preference saved.', 'ok')
        (ttl, clear), _kw = cli.loop.set_alarm_in.call_args
        self.assertEqual(ttl, 3)
        clear(cli.loop, None)
        self.assertEqual(cli._notice[1], '')
        cli._flash('Could not save', 'error')
        self.assertEqual(cli.loop.set_alarm_in.call_args.args[0], 10)
        cli.item_chosen('LEDs')
        self.assertEqual(cli._notice[1], 'Could not save')   # errors survive a screen change
        cli._flash('Settings saved.', 'ok')
        cli._active_tab.configure_led(None, 0)
        self.assertEqual(cli._notice[1], '')                 # confirmations do not
        self.assertFalse(any(isinstance(w, urwid.Button) for w in cli._walk_widgets(cli.frame.header)))
        footer = ''.join(t.get_text()[0] for t in cli._walk_widgets(cli.frame.footer) if isinstance(t, urwid.Text))
        self.assertNotIn('Tab', footer)
        self.assertIn('? keys', footer)
        cli.main_menu()

    def test_opening_a_chooser_and_going_back_is_not_a_draft(self):
        self.config.GetIoConfiguration.side_effect = lambda pin: ok(
            {'mode': 'DIGITAL_IN', 'pull': 'NOPULL', 'wakeup': ''} if pin == 2
            else {'mode': 'PWM_OUT_PUSHPULL', 'pull': 'NOPULL', 'period': '', 'duty_cycle': ''})
        for choice, path in (('IO', ['IO1', 'Mode: PWM_OUT_PUSHPULL']), ('IO', ['IO2', 'Wakeup: NO_WAKEUP']),
                             ('LEDs', ['D1', 'Function: Charge status']), ('Buttons', ['SW1'])):
            cli.item_chosen(choice)
            for label in path:
                button = next(b for b in cli._walk_widgets(cli.main.original_widget)
                              if isinstance(b, cli.MenuButton) and b.label.startswith(label.split(':')[0]))
                urwid.emit_signal(button, 'click', button)
                self.assertFalse(cli._dirty, (choice, label, 'entered'))
            for _ in path:
                cli.go_back()
                self.assertFalse(cli._dirty, (choice, 'back'))
            cli.main_menu()
        self.assertEqual(cli._drafts, {})

    def test_quit_can_be_cancelled_with_drafts_intact(self):
        cli.item_chosen('User Scripts')
        cli._dirty=True
        cli.exit_program()
        self.assertTrue(cli._in_dialog)
        cli._dialog_cancel()
        self.assertFalse(cli._in_dialog)
        self.assertTrue(cli._dirty)


if __name__ == '__main__': unittest.main()
