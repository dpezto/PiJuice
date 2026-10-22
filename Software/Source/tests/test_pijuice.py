"""I2C transfer plumbing of the pijuice library, against a fake bus."""
import sys
import threading
import unittest
from pathlib import Path
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


if __name__ == '__main__':
    unittest.main()
