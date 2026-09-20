"""Service-layer checks that need no hardware: firmware image validation and flash reporting."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pijuice_service as svc
from pijuice_service import PiJuiceError, PiJuiceService, check_firmware_file


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.image = Path(self.tmp.name, 'PiJuice-V1.6_2021_09_10.elf.binary')
        self.image.write_bytes(b'\xff' * 80000)
        self.service = PiJuiceService(config_path=str(Path(self.tmp.name, 'c.json')), connect=False)
        self.service.pj = SimpleNamespace(config=SimpleNamespace(interface=SimpleNamespace(GetAddress=lambda: 0x14)))

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def test_function_labels_are_readable_and_named(self):
        from pijuice_service import function_label, function_description, readable
        self.assertEqual(readable('HARD_FUNC_POWER_ON'), 'Power on')
        self.assertEqual(readable('LONG_PRESS1'), 'Long press 1')
        self.assertEqual(readable('PRESS'), 'Press')
        self.assertEqual(function_label('USER_FUNC12'), 'User script 12')
        self.assertEqual(function_label('USER_FUNC12', {'USER_FUNC12': 'Backup'}), 'Backup')
        self.assertIn('60 s', function_description('SYS_FUNC_HALT_POW_OFF'))
        self.assertIn('/x.sh', function_description('USER_FUNC2', {'user_functions': {'USER_FUNC2': '/x.sh'}}))

    def test_led_white_point_maps_writes_and_reads(self):
        from unittest.mock import Mock
        config = Mock()
        config.SetLedConfiguration.return_value = {'error': 'NO_ERROR'}
        config.GetLedConfiguration.return_value = {'error': 'NO_ERROR', 'data': {'function': 'USER_LED', 'parameter': {'r': 60, 'g': 100, 'b': 60}}}
        self.service.pj = SimpleNamespace(config=config)
        self.service.set_led_config('D2', {'function': 'USER_LED', 'parameter': {'r': 200, 'g': 200, 'b': 200}})
        self.assertEqual(config.SetLedConfiguration.call_args.args[1]['parameter'], {'r': 200, 'g': 200, 'b': 200})
        with patch.object(svc, 'notify_service', return_value=0):
            self.service.set_led_white('D2', [60, 100, 60])
            with self.assertRaises(ValueError): self.service.set_led_white('D2', [0, 100, 60])
            with self.assertRaises(ValueError): self.service.set_led_white('D2', [60, 100])
        self.service.set_led_config('D2', {'function': 'USER_LED', 'parameter': {'r': 255, 'g': 255, 'b': 255}})
        self.assertEqual(config.SetLedConfiguration.call_args.args[1]['parameter'], {'r': 60, 'g': 100, 'b': 60})
        self.assertEqual(self.service.get_led_config('D2')['parameter'], {'r': 255, 'g': 255, 'b': 255})
        self.assertEqual(self.service.get_led_white('D1'), [255, 255, 255])
        self.assertEqual(svc.led_white({'led_limits': {'D1': 50}}, 'D1'), [128, 128, 128])  # old limit migrates

    def test_image_must_look_like_a_pijuice_firmware(self):
        check_firmware_file(str(self.image))
        with self.assertRaisesRegex(PiJuiceError, 'file name'):
            check_firmware_file(str(Path(self.tmp.name, 'firmware.bin')))
        self.image.write_bytes(b'\xff' * 1000)
        with self.assertRaisesRegex(PiJuiceError, '1000 bytes'):
            check_firmware_file(str(self.image))
        with self.assertRaises(PiJuiceError):
            check_firmware_file(str(Path(self.tmp.name, 'PiJuice-V9.9_2030_01_01.elf.binary')))

    def test_flash_pauses_daemon_and_reports_flasher_output(self):
        signals = []
        run = SimpleNamespace(returncode=256 - 9, stdout='Page 12 programmed successfully\nverify failed 11\n')
        with patch.object(svc, 'signal_service', side_effect=lambda sig, _pid: signals.append(sig)), \
             patch.object(svc.subprocess, 'run', return_value=run) as popen:
            with self.assertRaisesRegex(PiJuiceError, 'PAGE_VERIFY_ERROR.*verify failed 11'):
                self.service.flash_firmware(str(self.image))
            self.assertEqual(signals, ['SIGUSR1', 'SIGUSR2'])
            self.assertEqual(popen.call_args.args[0], ['pijuiceboot', '14', str(self.image), '1'])
            run.returncode = 0
            self.assertEqual(self.service.flash_firmware(str(self.image)), 0)
        with patch.object(svc.subprocess, 'run') as popen:
            with self.assertRaises(PiJuiceError):
                self.service.flash_firmware(str(Path(self.tmp.name, 'x.bin')))
            popen.assert_not_called()  # a bad image never reaches the flasher


if __name__ == '__main__':
    unittest.main()
