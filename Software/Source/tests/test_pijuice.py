"""I2C transfer plumbing of the pijuice library, against a fake bus."""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pijuice


class FakeBus:
    def __init__(self, *_args):
        self.reads = 0

    def read_i2c_block_data(self, _addr, _cmd, length):
        self.reads += 1
        data = [0x12] * (length - 1)
        return data + [pijuice.PiJuiceInterface._GetChecksum(None, data)]


class TransferTests(unittest.TestCase):
    def setUp(self):
        with patch.object(pijuice, 'SMBus', FakeBus):
            self.interface = pijuice.PiJuiceInterface(1, 0x14)

    def test_transfer_runs_inline_without_a_thread(self):
        with patch.object(pijuice.threading, 'Thread', side_effect=AssertionError('thread per transfer')):
            self.assertEqual(self.interface.ReadData(0x40, 1), {'data': [0x12], 'error': 'NO_ERROR'})
        self.assertEqual(self.interface.i2cbus.reads, 1)

    def test_busy_instance_and_bus_error_still_report_communication_error(self):
        self.interface._lock.acquire()
        self.assertEqual(self.interface.ReadData(0x40, 1)['error'], 'COMMUNICATION_ERROR')
        self.interface._lock.release()
        self.interface.i2cbus.read_i2c_block_data = lambda *_a: (_ for _ in ()).throw(OSError(121, 'Remote I/O'))
        self.assertEqual(self.interface.ReadData(0x40, 1)['error'], 'COMMUNICATION_ERROR')
        self.assertFalse(self.interface._lock.locked())


class FirmwareVersionTests(unittest.TestCase):
    def config(self, replies):
        interface = SimpleNamespace(ReadData=lambda cmd, length: replies[cmd])
        return pijuice.PiJuiceConfig(interface)

    def test_semver_firmware_reports_patch(self):
        ok = lambda data: {'error': 'NO_ERROR', 'data': data}
        cfg = self.config({0xFD: ok([0x17, 0]), 0xFC: ok([1, 1, 7, 0, 0])})
        self.assertEqual(cfg.GetFirmwareVersion()['data'], {'version': '1.7.0', 'variant': '0'})

    def test_old_firmware_falls_back_to_major_minor(self):
        ok = lambda data: {'error': 'NO_ERROR', 'data': data}
        self.assertEqual(self.config({0xFD: ok([0x16, 0]), 0xFC: {'error': 'DATA_CORRUPTED'}})
                         .GetFirmwareVersion()['data']['version'], '1.6')
        # a reply that passes the checksum but is not ours (schema, or 0xFD mismatch)
        self.assertEqual(self.config({0xFD: ok([0x16, 0]), 0xFC: ok([0, 0, 0, 0, 0])})
                         .GetFirmwareVersion()['data']['version'], '1.6')
        self.assertEqual(self.config({0xFD: ok([0x16, 0]), 0xFC: ok([1, 1, 7, 0, 0])})
                         .GetFirmwareVersion()['data']['version'], '1.6')


if __name__ == '__main__':
    unittest.main()
