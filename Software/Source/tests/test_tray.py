"""Tray recovery runs in a separate process because GTK3 and GTK4 cannot mix."""
import subprocess
import sys
import unittest
from pathlib import Path


class TrayTests(unittest.TestCase):
    def test_recovers_after_repeated_disconnects(self):
        subprocess.run([sys.executable, __file__, '--worker'], check=True)


def worker():
    from concurrent.futures import Future
    from types import SimpleNamespace
    source = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(source), str(source / 'src')]
    from pijuice_tray import PiJuiceTray, GLib
    class Service:
        available = False
        online = False
        def connect(self): self.available = self.online
        def get_charge_level(self):
            if not self.online:
                raise OSError('disconnected')
            return 72
        def get_status(self): return {'battery': 'NORMAL'}
        def submit(self, fn):
            f = Future()
            try:
                f.set_result(fn())
            except Exception as exc:
                f.set_exception(exc)
            return f
    tray = PiJuiceTray.__new__(PiJuiceTray)
    tray.service = Service()
    tray._pending = False
    tray.refresh_err = 0
    labels = []
    tray.indicator = SimpleNamespace(set_icon_full=lambda *_args: None)
    tray.level_item = SimpleNamespace(set_label=labels.append)
    context = GLib.MainContext.default()
    for _ in range(6):
        assert tray.refresh()
        while context.pending(): context.iteration(False)
    assert tray.refresh_err == 6
    tray.service.online = True
    tray.refresh()
    while context.pending(): context.iteration(False)
    assert tray.refresh_err == 0
    assert labels[-1] == 'Charge: 72%'


if __name__ == '__main__':
    if '--worker' in sys.argv:
        worker()
    else:
        unittest.main()
