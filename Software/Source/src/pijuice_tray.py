#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""PiJuice status tray icon (Wayland-native via StatusNotifierItem).

GTK4 has no system-tray API, so this stays on GTK3 + libayatana-appindicator,
which speaks the StatusNotifierItem D-Bus protocol that the Wayfire panel
(``wf-panel-pi``) and other Wayland panels understand. It is a separate process
from the GTK4 settings app and only shares the toolkit-agnostic
:class:`pijuice_service.PiJuiceService` for I2C + config resolution.
"""

import os
import subprocess
import sys
from signal import SIGUSR1, SIGUSR2

# No a11y D-Bus on the Pi; skip the GTK3 atk-bridge to avoid a startup warning.
os.environ.setdefault("NO_AT_BRIDGE", "1")

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gtk, GLib  # noqa: E402
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402

# The service ships as a top-level module; make sure it's importable when this
# script is run by path (e.g. the desktop autostart entry).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from pijuice_service import PiJuiceService, PiJuiceError  # noqa: E402
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from pijuice_service import PiJuiceService, PiJuiceError
from pijuice import get_versions  # noqa: E402

APP_ID = "pijuice-tray"
ICON_DIR = "/usr/share/pijuice/data/images"
REFRESH_INTERVAL = 5000  # ms
TRAY_PID_FILE = "/run/pijuice/pijuice_tray.pid"


def _find_settings_app():
    """Locate the GTK4 settings script (dev tree or installed)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "pijuice_gtk.py"),
                 "/usr/bin/pijuice_gtk.py",
                 "/usr/share/pijuice/pijuice_gtk.py"):
        if os.path.exists(cand):
            return cand
    return None


class PiJuiceTray(object):
    def __init__(self):
        self.service = PiJuiceService()
        self.refresh_err = 0

        self.indicator = AppIndicator.Indicator.new(
            APP_ID, "pijuice", AppIndicator.IndicatorCategory.HARDWARE)
        # Resolve our icon basenames from ICON_DIR. Panels load the icon by
        # IconName + IconThemePath (SNI); passing an absolute path instead made
        # the panel look up a bare basename in its own theme and find nothing.
        self.indicator.set_icon_theme_path(ICON_DIR)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("PiJuice")
        self.indicator.set_menu(self._build_menu())

        self.refresh()
        GLib.timeout_add(REFRESH_INTERVAL, self.refresh)

    def _build_menu(self):
        menu = Gtk.Menu()
        self.level_item = Gtk.MenuItem(label="…")
        self.level_item.set_sensitive(False)
        menu.append(self.level_item)
        menu.append(Gtk.SeparatorMenuItem())

        self.settings_item = Gtk.MenuItem(label="Settings")
        self.settings_item.connect("activate", self._on_settings)
        menu.append(self.settings_item)

        about = Gtk.MenuItem(label="About…")
        about.connect("activate", self._on_about)
        menu.append(about)

        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _w: Gtk.main_quit())
        menu.append(quit_item)

        menu.show_all()
        return menu

    @staticmethod
    def _icon_name(level, status):
        """Pick an icon basename from charge level + battery/power state."""
        if status is None:
            return "connection-error"
        battery = status.get("battery", "NORMAL")
        p_in = status.get("powerInput", "NOT_PRESENT")
        p_io = status.get("powerInput5vIo", "NOT_PRESENT")
        step = (level // 10) * 10
        if battery == "NOT_PRESENT":
            return "no-bat-in-0" if p_in != "NOT_PRESENT" else "no-bat-rpi-0"
        if battery == "CHARGING_FROM_IN" or p_in != "NOT_PRESENT":
            return "bat-in-%d" % step
        if battery == "CHARGING_FROM_5V_IO" or p_io != "NOT_PRESENT":
            return "bat-rpi-%d" % step
        return "bat-%d" % step

    def refresh(self):
        try:
            level = self.service.get_charge_level()
            status = self.service.get_status()
            self.refresh_err = 0
        except PiJuiceError:
            self.refresh_err += 1
            self.indicator.set_icon_full("connection-error",
                                         "PiJuice: no connection")
            self.level_item.set_label("No connection")
            if self.refresh_err > 4:
                Gtk.main_quit()
            return True
        self.indicator.set_icon_full(self._icon_name(level, status),
                                     "%d%%" % level)
        self.level_item.set_label("Charge: %d%%" % level)
        return True  # keep the timer

    def _on_settings(self, _widget):
        app = _find_settings_app()
        if app is None:
            return
        subprocess.Popen(["/usr/bin/python3", app],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _on_about(self, _widget):
        sw, fw, osv = get_versions()
        msg = "Software: %s\nFirmware: %s\nOS: %s" % (sw, fw or "no connection", osv)
        dialog = Gtk.MessageDialog(modal=True, message_type=Gtk.MessageType.INFO,
                                   buttons=Gtk.ButtonsType.OK, text=msg)
        dialog.set_title("About PiJuice")
        dialog.run()
        dialog.destroy()


def main():
    try:
        with open(TRAY_PID_FILE, "w") as fh:
            fh.write(str(os.getpid()))
        os.chmod(TRAY_PID_FILE, 0o666)  # the settings GUI may signal us
    except OSError:
        pass

    tray = PiJuiceTray()

    # The settings GUI can SIGUSR1/SIGUSR2 to grey/ungrey "Settings" while open.
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, int(SIGUSR1),
                         lambda: (tray.settings_item.set_sensitive(False), True)[1])
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, int(SIGUSR2),
                         lambda: (tray.settings_item.set_sensitive(True), True)[1])

    Gtk.main()


if __name__ == "__main__":
    main()
