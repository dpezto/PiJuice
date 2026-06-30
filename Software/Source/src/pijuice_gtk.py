#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""GTK4 front-end for the PiJuice HAT (Wayland-native, replaces the TkInter GUI).

Design:
  * Pure GTK4 (no libadwaita) so it stays light and portable on the Pi's small
    Waveshare touchscreen. A ``Gtk.Stack`` + ``Gtk.StackSidebar`` replaces the old
    ``ttk.Notebook`` tabs.
  * All HAT access goes through :class:`pijuice_service.PiJuiceService`. No widget
    ever touches I2C or the JSON config directly — that is the decoupling. Reads
    run on the service's worker thread and results are marshalled back to the GTK
    main loop with ``GLib.idle_add`` (see :meth:`_View.run_async`).
  * Views are added incrementally. Status / LEDs / Buttons are fully ported;
    the remaining tabs render a scaffold that already has the service wired in,
    so finishing them is filling in widgets, not plumbing.

Run on the device:  python3 pijuice_gtk.py
"""

import datetime
import os
import re
import sys

# The Pi has no a11y D-Bus; skip it to avoid a noisy startup warning.
os.environ.setdefault("GTK_A11Y", "none")

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from pijuice_service import (  # noqa: E402
    LED_USER_SELECTABLE,
    PiJuiceError,
    PiJuiceService,
)

APP_ID = "org.pisupply.PiJuice"

# Touch-friendly styling for the small Waveshare screen ("better looks" TODO).
_CSS = b"""
button { min-height: 38px; padding: 6px 12px; }
entry { min-height: 34px; }
.monospace { font-family: monospace; }
frame > label { font-weight: bold; }
.dim-label { opacity: 0.65; font-size: 90%; }
"""


def _install_css(display):
    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(_CSS)            # GTK < 4.12
    except TypeError:
        provider.load_from_string(_CSS.decode())  # GTK >= 4.12
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


# ── base view ────────────────────────────────────────────────────────────────
class _View(Gtk.Box):
    """Base for stack pages: holds the service + thread-marshalling helper."""

    title = "View"
    slug = "view"

    def __init__(self, service):
        super().__init__(orientation=Gtk.Orientation.VERTICAL,
                         spacing=10, margin_top=12, margin_bottom=12,
                         margin_start=12, margin_end=12)
        self.service = service
        self._status = Gtk.Label(xalign=0)
        self._status.add_css_class("dim-label")

    def run_async(self, fn, on_done=None, on_error=None):
        """Run ``fn`` on the I2C worker; deliver result/error on the GTK loop."""
        future = self.service.submit(fn)

        def _settle(fut):
            try:
                result = fut.result()
            except PiJuiceError as exc:
                if on_error is not None:
                    GLib.idle_add(on_error, exc)
                else:
                    GLib.idle_add(self._status.set_text, str(exc))
                return
            if on_done is not None:
                GLib.idle_add(on_done, result)

        future.add_done_callback(_settle)

    def flash(self, text):
        self._status.set_text(text)


# ── status view ──────────────────────────────────────────────────────────────
class StatusView(_View):
    title = "Status"
    slug = "status"

    def __init__(self, service):
        super().__init__(service)
        self._labels = {}
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        fields = [
            ("battery", "Battery"),
            ("gpio", "GPIO power input"),
            ("usb", "USB micro power"),
            ("fault", "Fault"),
            ("sys_sw", "System switch"),
        ]
        for row, (key, label) in enumerate(fields):
            name = Gtk.Label(label=label + ":", xalign=0)
            name.add_css_class("heading")
            value = Gtk.Label(label="…", xalign=0, selectable=True)
            grid.attach(name, 0, row, 1, 1)
            grid.attach(value, 1, row, 1, 1)
            self._labels[key] = value
        self.append(grid)

        switch_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        switch_row.append(Gtk.Label(label="System power switch:", xalign=0))
        self._switch = Gtk.DropDown.new_from_strings(["Off", "500 mA", "2100 mA"])
        self._switch_values = [0, 500, 2100]
        apply_sw = Gtk.Button(label="Set")
        apply_sw.connect("clicked", self._on_set_switch)
        switch_row.append(self._switch)
        switch_row.append(apply_sw)
        self.append(switch_row)

        self.append(self._status)

        if not self.service.available:
            self.flash("No PiJuice detected.")
        else:
            self.refresh()
            # Live status, like the old 1s urwid alarm.
            GLib.timeout_add_seconds(2, self._tick)

    def _tick(self):
        self.refresh()
        return True  # keep the timer running

    def refresh(self):
        self.run_async(self._read, self._apply)

    def _read(self):
        """Best-effort read of all status fields (runs on the worker)."""
        out = {}
        try:
            status = self.service.get_status()
        except PiJuiceError:
            status = {}

        try:
            batt = "%i%%" % self.service.get_charge_level()
            try:
                mv = float(self.service.get_battery_voltage())
                batt += ", %.3fV" % (mv / 1000)
                temp = self.service.get_battery_temperature()
                if temp != -999:
                    batt += ", %d°C" % temp
            except PiJuiceError:
                pass
            if status.get("battery"):
                batt += ", %s" % status["battery"]
            out["battery"] = batt
        except PiJuiceError as exc:
            out["battery"] = str(exc)

        out["usb"] = str(status.get("powerInput", "N/A"))

        try:
            iov = float(self.service.get_io_voltage()) / 1000
            ioc = float(self.service.get_io_current()) / 1000
            out["gpio"] = "%.3fV, %.3fA, %s" % (iov, ioc,
                                                status.get("powerInput5vIo", "N/A"))
        except PiJuiceError:
            out["gpio"] = "N/A"

        try:
            fault = self.service.get_fault_status() or {}
            problems = []
            if fault.get("battery_profile_invalid"):
                problems.append("battery profile invalid")
            ctf = fault.get("charging_temperature_fault")
            if ctf and ctf != "NORMAL":
                problems.append("charging temperature " + str(ctf))
            out["fault"] = ", ".join(problems) if problems else "None"
        except PiJuiceError as exc:
            out["fault"] = str(exc)

        try:
            sw = self.service.get_system_power_switch()
            out["sys_sw"] = ("%dmA" % sw) if sw else "Off"
        except PiJuiceError:
            out["sys_sw"] = "N/A"

        return out

    def _apply(self, data):
        for key, value in data.items():
            if key in self._labels:
                self._labels[key].set_text(value)

    def _on_set_switch(self, _btn):
        idx = self._switch.get_selected()
        value = self._switch_values[idx] if 0 <= idx < len(self._switch_values) else 0
        self.run_async(
            lambda: self.service.set_system_power_switch(value),
            lambda _r: self.flash("System switch set to %s." %
                                  ("Off" if not value else "%dmA" % value)),
        )


# ── LED view (includes the B1a "red disables green" fix) ─────────────────────
class LedView(_View):
    title = "LEDs"
    slug = "leds"

    def __init__(self, service):
        super().__init__(service)
        self._rows = {}

        intro = Gtk.Label(xalign=0, wrap=True)
        intro.set_markup(
            "<small>Set a LED to <b>USER_LED</b> to control its colour directly. "
            "In CHARGE_STATUS the firmware drives R/G itself, which is why a manual "
            "colour appears to “disable” a channel. “Test colour” "
            "forces USER_LED first.</small>"
        )
        self.append(intro)

        if not self.service.available:
            self.flash("No PiJuice detected.")
            self.append(self._status)
            return

        for led in self.service.leds:
            self.append(self._build_led_row(led))

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", self._on_apply)
        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.connect("clicked", lambda _b: self.refresh())
        actions.append(apply_btn)
        actions.append(refresh_btn)
        self.append(actions)
        self.append(self._status)

        self.refresh()

    def _rgb_spin(self):
        adj = Gtk.Adjustment(lower=0, upper=255, step_increment=1, page_increment=16)
        return Gtk.SpinButton(adjustment=adj, numeric=True)

    def _build_led_row(self, led):
        frame = Gtk.Frame(label=led)
        grid = Gtk.Grid(column_spacing=10, row_spacing=6,
                        margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)

        grid.attach(Gtk.Label(label="Function:", xalign=0), 0, 0, 1, 1)
        func = Gtk.DropDown.new_from_strings(LED_USER_SELECTABLE)
        grid.attach(func, 1, 0, 3, 1)

        r, g, b = self._rgb_spin(), self._rgb_spin(), self._rgb_spin()
        for col, (lbl, spin) in enumerate(zip("RGB", (r, g, b))):
            grid.attach(Gtk.Label(label=lbl + ":", xalign=0), 0, col + 1, 1, 1)
            grid.attach(spin, 1, col + 1, 1, 1)

        test = Gtk.Button(label="Test colour")
        test.connect("clicked", self._on_test, led)
        grid.attach(test, 2, 1, 2, 3)

        frame.set_child(grid)
        self._rows[led] = {"function": func, "r": r, "g": g, "b": b}
        return frame

    def refresh(self):
        for led in self._rows:
            self.run_async(lambda led=led: (led, self.service.get_led_config(led)),
                           self._apply_one)

    def _apply_one(self, pair):
        led, cfg = pair
        row = self._rows.get(led)
        if not row or not cfg:
            return
        fn = cfg.get("function", "NOT_USED")
        if fn in LED_USER_SELECTABLE:
            row["function"].set_selected(LED_USER_SELECTABLE.index(fn))
        param = cfg.get("parameter", {})
        for ch in ("r", "g", "b"):
            try:
                row[ch].set_value(int(param.get(ch, 0)))
            except (TypeError, ValueError):
                row[ch].set_value(0)

    def _row_config(self, led):
        row = self._rows[led]
        idx = row["function"].get_selected()
        function = LED_USER_SELECTABLE[idx] if 0 <= idx < len(LED_USER_SELECTABLE) \
            else "NOT_USED"
        return {
            "function": function,
            "parameter": {ch: row[ch].get_value_as_int() for ch in ("r", "g", "b")},
        }

    def _on_apply(self, _btn):
        for led in self._rows:
            cfg = self._row_config(led)
            self.run_async(lambda led=led, cfg=cfg: self.service.set_led_config(led, cfg),
                           lambda _r: self.flash("LED settings applied."))

    def _on_test(self, _btn, led):
        row = self._rows[led]
        rgb = [row[ch].get_value_as_int() for ch in ("r", "g", "b")]
        # set_led_color enforces USER_LED first — the actual bug fix.
        self.run_async(lambda: self.service.set_led_color(led, rgb),
                       lambda _r: self.flash("%s -> rgb%s (USER_LED)." % (led, tuple(rgb))))


# ── buttons view ─────────────────────────────────────────────────────────────
class ButtonsView(_View):
    title = "Buttons"
    slug = "buttons"

    def __init__(self, service):
        super().__init__(service)
        self._cells = {}

        if not self.service.available:
            self.flash("No PiJuice detected.")
            self.append(self._status)
            return

        from pijuice import (pijuice_hard_functions, pijuice_sys_functions,
                             pijuice_user_functions)
        self._functions = (["NO_FUNC"] + list(pijuice_hard_functions)
                           + list(pijuice_sys_functions) + list(pijuice_user_functions))

        scroller = Gtk.ScrolledWindow(vexpand=True)
        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        row = 0
        for button in self.service.buttons:
            header = Gtk.Label(xalign=0)
            header.set_markup("<b>%s</b>" % button)
            grid.attach(header, 0, row, 3, 1)
            row += 1
            for event in self.service.button_events:
                grid.attach(Gtk.Label(label=event + ":", xalign=0), 0, row, 1, 1)
                func = Gtk.DropDown.new_from_strings(self._functions)
                param = Gtk.Entry(text="0")
                grid.attach(func, 1, row, 1, 1)
                grid.attach(param, 2, row, 1, 1)
                self._cells[(button, event)] = (func, param)
                row += 1
        scroller.set_child(grid)
        self.append(scroller)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", self._on_apply)
        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.connect("clicked", lambda _b: self.refresh())
        actions.append(apply_btn)
        actions.append(refresh_btn)
        self.append(actions)
        self.append(self._status)

        self.refresh()

    def refresh(self):
        for button in self.service.buttons:
            self.run_async(lambda b=button: (b, self.service.get_button_config(b)),
                           self._apply_button)

    def _apply_button(self, pair):
        button, cfg = pair
        if not cfg:
            return
        for event, conf in cfg.items():
            cell = self._cells.get((button, event))
            if not cell:
                continue
            func, param = cell
            fn = conf.get("function", "NO_FUNC")
            if fn in self._functions:
                func.set_selected(self._functions.index(fn))
            param.set_text(str(conf.get("parameter", 0)))

    def _on_apply(self, _btn):
        for button in self.service.buttons:
            config = {}
            for event in self.service.button_events:
                func, param = self._cells[(button, event)]
                idx = func.get_selected()
                fn = self._functions[idx] if 0 <= idx < len(self._functions) else "NO_FUNC"
                try:
                    pval = int(param.get_text())
                except ValueError:
                    pval = 0
                config[event] = {"function": fn, "parameter": pval}
            self.run_async(
                lambda b=button, c=config: self.service.set_button_config(b, c),
                lambda _r: self.flash("Button settings applied."),
            )


# ── user scripts view (config JSON; "better user functions input" TODO) ──────
class UserScriptsView(_View):
    title = "User Scripts"
    slug = "userscripts"
    COUNT = 15

    def __init__(self, service):
        super().__init__(service)
        cfg = self.service.config.setdefault("user_functions", {})
        self._entries = {}

        intro = Gtk.Label(xalign=0, wrap=True)
        intro.set_markup(
            "<small>Each <b>USER_FUNCx</b> runs as the <tt>pijuice</tt> user when a "
            "button or system event is mapped to it. Use an absolute path; the "
            "service is reloaded on Apply.</small>")
        self.append(intro)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        for i in range(self.COUNT):
            key = "USER_FUNC%d" % (i + 1)
            label = Gtk.Label(label=key, xalign=1)
            entry = Gtk.Entry(hexpand=True, text=str(cfg.get(key, "")))
            entry.set_placeholder_text("/usr/local/bin/my-script.sh  (blank = unused)")
            entry.add_css_class("monospace")
            grid.attach(label, 0, i, 1, 1)
            grid.attach(entry, 1, i, 1, 1)
            self._entries[key] = entry
        scroller.set_child(grid)
        self.append(scroller)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", self._on_apply)
        actions.append(apply_btn)
        self.append(actions)
        self.append(self._status)

    def _on_apply(self, _btn):
        cfg = self.service.config.setdefault("user_functions", {})
        for key, entry in self._entries.items():
            cfg[key] = entry.get_text().strip()
        self.run_async(self.service.save_and_notify, self._saved)

    def _saved(self, rc):
        self.flash("Saved." if rc == 0 else "Saved; service notify failed (rc=%s)." % rc)


# ── system events view (config JSON) ─────────────────────────────────────────
class SystemEventsView(_View):
    title = "System Events"
    slug = "sysevents"
    EVENTS = [
        ("low_charge", "Low charge"),
        ("low_battery_voltage", "Low battery voltage"),
        ("no_power", "No power"),
        ("power", "Power present"),
        ("watchdog_reset", "Watchdog reset"),
        ("button_power_off", "Button power off"),
        ("forced_power_off", "Forced power off"),
        ("forced_sys_power_off", "Forced sys power off"),
        ("sys_start", "System start"),
        ("sys_stop", "System stop"),
    ]

    def __init__(self, service):
        super().__init__(service)
        from pijuice import pijuice_sys_functions, pijuice_user_functions
        self._functions = (["NO_FUNC"] + list(pijuice_sys_functions)
                           + list(pijuice_user_functions))
        events_cfg = self.service.config.setdefault("system_events", {})
        self._rows = {}

        scroller = Gtk.ScrolledWindow(vexpand=True)
        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        for row, (key, text) in enumerate(self.EVENTS):
            ev = events_cfg.setdefault(key, {})
            ev.setdefault("enabled", False)
            ev.setdefault("function", "NO_FUNC")
            chk = Gtk.CheckButton(label=text, active=bool(ev["enabled"]))
            func = Gtk.DropDown.new_from_strings(self._functions)
            if ev["function"] in self._functions:
                func.set_selected(self._functions.index(ev["function"]))
            grid.attach(chk, 0, row, 1, 1)
            grid.attach(func, 1, row, 1, 1)
            self._rows[key] = (chk, func)
        scroller.set_child(grid)
        self.append(scroller)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", self._on_apply)
        actions.append(apply_btn)
        self.append(actions)
        self.append(self._status)

    def _on_apply(self, _btn):
        cfg = self.service.config.setdefault("system_events", {})
        for key, (chk, func) in self._rows.items():
            idx = func.get_selected()
            fn = self._functions[idx] if 0 <= idx < len(self._functions) else "NO_FUNC"
            cfg[key] = {"enabled": chk.get_active(), "function": fn}
        self.run_async(self.service.save_and_notify, self._saved)

    def _saved(self, rc):
        self.flash("Saved." if rc == 0 else "Saved; service notify failed (rc=%s)." % rc)


# ── system task view (config JSON; mirrors the Tk PiJuiceSysTaskTab) ──────────
class SystemTaskView(_View):
    title = "System Task"
    slug = "systask"
    # section, label, value field, value label, type, min, max
    PARAMS = [
        ("watchdog", "Watchdog", "period", "Expire period [minutes]", int, 1, 65535),
        ("wakeup_on_charge", "Wakeup on charge", "trigger_level", "Trigger level [%]", int, 0, 100),
        ("min_charge", "Minimum charge", "threshold", "Threshold [%]", int, 0, 100),
        ("min_bat_voltage", "Minimum battery voltage", "threshold", "Threshold [V]", float, 0, 10),
        ("ext_halt_power_off", "Software halt power off", "period", "Delay period [seconds]", int, 20, 65535),
    ]
    # ponytail: the FW>=0x15 non-volatile "restore" watchdog/wakeup toggles are
    # omitted (they need an I2C SetWatchdog write); the daemon re-applies the JSON
    # params each boot regardless. Add them with a service method if persistence
    # across power-loss is wanted.

    def __init__(self, service):
        super().__init__(service)
        st = self.service.config.setdefault("system_task", {})

        self._enabled = Gtk.CheckButton(label="System task enabled",
                                        active=bool(st.get("enabled", False)))
        self.append(self._enabled)

        self._rows = {}
        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        for row, (sec, label, field, vlabel, typ, lo, hi) in enumerate(self.PARAMS):
            secd = st.setdefault(sec, {})
            chk = Gtk.CheckButton(label=label, active=bool(secd.get("enabled", False)))
            entry = Gtk.Entry(text=str(secd.get(field, "")))
            entry.set_width_chars(8)
            grid.attach(chk, 0, row, 1, 1)
            grid.attach(Gtk.Label(label=vlabel + ":", xalign=0), 1, row, 1, 1)
            grid.attach(entry, 2, row, 1, 1)
            self._rows[sec] = (chk, entry, field, typ, lo, hi)
        self.append(grid)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", self._on_apply)
        actions.append(apply_btn)
        self.append(actions)
        self.append(self._status)

    def _on_apply(self, _btn):
        st = self.service.config.setdefault("system_task", {})
        st["enabled"] = self._enabled.get_active()
        bad = []
        for sec, (chk, entry, field, typ, lo, hi) in self._rows.items():
            secd = st.setdefault(sec, {})
            secd["enabled"] = chk.get_active()
            text = entry.get_text().strip()
            if text == "":
                secd.pop(field, None)          # value unset
                continue
            try:
                val = typ(text)
            except ValueError:
                bad.append(sec)
                continue
            if not (lo <= val <= hi):
                bad.append("%s (%s..%s)" % (sec, lo, hi))
                continue
            secd[field] = val
        if bad:
            self.flash("Invalid: " + ", ".join(bad))
            return
        self.run_async(self.service.save_and_notify, self._saved)

    def _saved(self, rc):
        self.flash("Saved." if rc == 0 else "Saved; service notify failed (rc=%s)." % rc)


# ── battery view ─────────────────────────────────────────────────────────────
class BatteryView(_View):
    title = "Battery"
    slug = "battery"
    # ponytail: predefined profiles + temp-sense + RSOC + charging only. The
    # full custom-profile editor (~20 fields) is the rare advanced path; add it
    # with a "Custom" toggle + the GetBatteryProfile/SetCustomBatteryProfile pair
    # when someone actually needs to hand-tune a cell.

    def __init__(self, service):
        super().__init__(service)
        if not service.available:
            self.flash("No PiJuice detected.")
            self.append(self._status)
            return

        self._profiles = service.get_battery_profiles()
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        row = 0
        grid.attach(Gtk.Label(label="Profile:", xalign=0), 0, row, 1, 1)
        self._profile = Gtk.DropDown.new_from_strings(self._profiles)
        grid.attach(self._profile, 1, row, 1, 1)
        row += 1

        grid.attach(Gtk.Label(label="Status:", xalign=0), 0, row, 1, 1)
        self._pstatus = Gtk.Label(label="…", xalign=0, selectable=True)
        grid.attach(self._pstatus, 1, row, 1, 1)
        row += 1

        grid.attach(Gtk.Label(label="Temperature sense:", xalign=0), 0, row, 1, 1)
        self._temp = Gtk.DropDown.new_from_strings(service.battery_temp_sense_options)
        grid.attach(self._temp, 1, row, 1, 1)
        row += 1

        self._rsoc = None
        if service.fw_int >= 0x13:
            grid.attach(Gtk.Label(label="RSoC estimation:", xalign=0), 0, row, 1, 1)
            self._rsoc = Gtk.DropDown.new_from_strings(service.rsoc_estimation_options)
            grid.attach(self._rsoc, 1, row, 1, 1)
            row += 1

        self._charging = Gtk.CheckButton(label="Charging enabled")
        grid.attach(self._charging, 0, row, 2, 1)
        self.append(grid)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", self._on_apply)
        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.connect("clicked", lambda _b: self.refresh())
        actions.append(apply_btn)
        actions.append(refresh_btn)
        self.append(actions)
        self.append(self._status)

        self.refresh()

    def refresh(self):
        self.run_async(self._read, self._apply)

    def _read(self):
        out = {}
        for key, fn in (("status", self.service.get_battery_profile_status),
                        ("temp", self.service.get_battery_temp_sense),
                        ("charging", self.service.get_charging_config)):
            try:
                out[key] = fn()
            except PiJuiceError as exc:
                out[key] = ("error", str(exc))
        if self._rsoc is not None:
            try:
                out["rsoc"] = self.service.get_rsoc_estimation()
            except PiJuiceError:
                out["rsoc"] = None
        return out

    def _apply(self, data):
        st = data.get("status")
        if isinstance(st, dict):
            self._pstatus.set_text("%s · %s · %s" % (st.get("profile", "?"),
                                                     st.get("validity", "?"),
                                                     st.get("source", "?")))
            prof = st.get("profile")
            if prof in self._profiles:
                self._profile.set_selected(self._profiles.index(prof))
        opts = self.service.battery_temp_sense_options
        if data.get("temp") in opts:
            self._temp.set_selected(opts.index(data["temp"]))
        if self._rsoc is not None and data.get("rsoc") in self.service.rsoc_estimation_options:
            self._rsoc.set_selected(self.service.rsoc_estimation_options.index(data["rsoc"]))
        ch = data.get("charging")
        if isinstance(ch, dict):
            self._charging.set_active(bool(ch.get("charging_enabled")))

    def _on_apply(self, _btn):
        profiles = self._profiles
        idx = self._profile.get_selected()
        profile = profiles[idx] if 0 <= idx < len(profiles) else None
        temp = self.service.battery_temp_sense_options[self._temp.get_selected()]
        rsoc = (self.service.rsoc_estimation_options[self._rsoc.get_selected()]
                if self._rsoc is not None else None)
        charging = self._charging.get_active()

        def work():
            if profile:
                self.service.set_battery_profile(profile)
            self.service.set_battery_temp_sense(temp)
            if rsoc is not None:
                self.service.set_rsoc_estimation(rsoc)
            self.service.set_charging_config(charging)
            return True

        self.run_async(work, lambda _r: (self.flash("Battery settings applied."),
                                         self.refresh()))


# ── IO view ──────────────────────────────────────────────────────────────────
class IoView(_View):
    title = "IO"
    slug = "io"

    def __init__(self, service):
        super().__init__(service)
        if not service.available:
            self.flash("No PiJuice detected.")
            self.append(self._status)
            return

        self._pins = {}
        for pin in (1, 2):
            self.append(self._build_pin(pin))

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", self._on_apply)
        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.connect("clicked", lambda _b: self.refresh())
        actions.append(apply_btn)
        actions.append(refresh_btn)
        self.append(actions)
        self.append(self._status)

        self.refresh()

    def _build_pin(self, pin):
        frame = Gtk.Frame(label="IO%d" % pin)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        modes = self.service.io_supported_modes(pin)

        head = Gtk.Grid(column_spacing=10, row_spacing=6)
        head.attach(Gtk.Label(label="Mode:", xalign=0), 0, 0, 1, 1)
        mode_dd = Gtk.DropDown.new_from_strings(modes)
        head.attach(mode_dd, 1, 0, 1, 1)
        head.attach(Gtk.Label(label="Pull:", xalign=0), 2, 0, 1, 1)
        pull_dd = Gtk.DropDown.new_from_strings(self.service.io_pull_options)
        head.attach(pull_dd, 3, 0, 1, 1)
        box.append(head)

        param_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(param_box)
        frame.set_child(box)

        self._pins[pin] = {"modes": modes, "mode": mode_dd, "pull": pull_dd,
                           "param_box": param_box, "params": [], "data": {}}
        mode_dd.connect("notify::selected",
                        lambda _dd, _p, pin=pin: self._rebuild_params(pin))
        return frame

    def _rebuild_params(self, pin):
        p = self._pins[pin]
        child = p["param_box"].get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            p["param_box"].remove(child)
            child = nxt
        p["params"] = []
        mode = p["modes"][p["mode"].get_selected()]
        for pcfg in self.service.io_config_params.get(mode, []):
            rowbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            if pcfg["type"] == "enum":
                widget = Gtk.DropDown.new_from_strings(pcfg["options"])
                label = pcfg["name"]
            else:
                widget = Gtk.Entry()
                unit = (" " + pcfg["unit"]) if pcfg.get("unit") else ""
                label = "%s [%s-%s%s]" % (pcfg["name"], pcfg["min"], pcfg["max"], unit)
            rowbox.append(Gtk.Label(label=label + ":", xalign=0))
            rowbox.append(widget)
            p["param_box"].append(rowbox)
            p["params"].append((pcfg, widget))
        # Pre-fill from the device config only when the mode matches what's on it.
        if p["data"].get("mode") == mode:
            for pcfg, widget in p["params"]:
                val = p["data"].get(pcfg["name"])
                if val is None:
                    continue
                if pcfg["type"] == "enum":
                    if val in pcfg["options"]:
                        widget.set_selected(pcfg["options"].index(val))
                else:
                    widget.set_text(str(val))

    def refresh(self):
        for pin in (1, 2):
            self.run_async(lambda pin=pin: (pin, self.service.get_io_config(pin)),
                           self._apply_pin)

    def _apply_pin(self, pair):
        pin, cfg = pair
        if not cfg:
            return
        p = self._pins[pin]
        p["data"] = cfg
        if cfg.get("mode") in p["modes"]:
            p["mode"].set_selected(p["modes"].index(cfg["mode"]))
        pulls = self.service.io_pull_options
        if cfg.get("pull") in pulls:
            p["pull"].set_selected(pulls.index(cfg["pull"]))
        self._rebuild_params(pin)

    def _on_apply(self, _btn):
        pulls = self.service.io_pull_options
        for pin in (1, 2):
            p = self._pins[pin]
            mode = p["modes"][p["mode"].get_selected()]
            cfg = {"mode": mode, "pull": pulls[p["pull"].get_selected()]}
            for pcfg, widget in p["params"]:
                if pcfg["type"] == "enum":
                    cfg[pcfg["name"]] = pcfg["options"][widget.get_selected()]
                else:
                    text = widget.get_text().strip()
                    try:
                        cfg[pcfg["name"]] = (float(text) if pcfg["type"] == "float"
                                             else int(text))
                    except ValueError:
                        cfg[pcfg["name"]] = pcfg["min"]
            self.run_async(lambda pin=pin, cfg=cfg: self.service.set_io_config(pin, cfg),
                           lambda _r: self.flash("IO settings applied."))


# ── wakeup alarm view ────────────────────────────────────────────────────────
class WakeupView(_View):
    title = "Wakeup Alarm"
    slug = "wakeup"

    def __init__(self, service):
        super().__init__(service)
        if not service.available:
            self.flash("No PiJuice detected.")
            self.append(self._status)
            return

        self._time = Gtk.Label(label="UTC: …", xalign=0)
        self._time.add_css_class("monospace")
        set_time = Gtk.Button(label="Set RTC time (from this Pi, UTC)")
        set_time.connect("clicked", self._on_set_time)
        trow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        trow.append(self._time)
        trow.append(set_time)
        self.append(trow)

        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        # Day vs weekday (radio) + value + every-day
        self._is_weekday = Gtk.CheckButton(label="Day")
        wk = Gtk.CheckButton(label="Weekday")
        wk.set_group(self._is_weekday)
        self._is_weekday_btn = wk
        grid.attach(self._is_weekday, 0, 0, 1, 1)
        grid.attach(wk, 1, 0, 1, 1)
        self._day = Gtk.Entry()
        self._day.set_placeholder_text("1-31 / 1-7")
        self._every_day = Gtk.CheckButton(label="Every day")
        grid.attach(self._day, 0, 1, 2, 1)
        grid.attach(self._every_day, 2, 1, 1, 1)

        grid.attach(Gtk.Label(label="Hour:", xalign=0), 0, 2, 1, 1)
        self._hour = Gtk.Entry()
        self._every_hour = Gtk.CheckButton(label="Every hour")
        grid.attach(self._hour, 1, 2, 1, 1)
        grid.attach(self._every_hour, 2, 2, 1, 1)

        self._is_period = Gtk.CheckButton(label="Minute")
        per = Gtk.CheckButton(label="Minutes period")
        per.set_group(self._is_period)
        self._is_period_btn = per
        grid.attach(self._is_period, 0, 3, 1, 1)
        grid.attach(per, 1, 3, 1, 1)
        self._minute = Gtk.Entry()
        grid.attach(self._minute, 0, 4, 2, 1)

        grid.attach(Gtk.Label(label="Second:", xalign=0), 0, 5, 1, 1)
        self._second = Gtk.Entry(text="0")
        grid.attach(self._second, 1, 5, 1, 1)
        self.append(grid)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        set_alarm = Gtk.Button(label="Set Alarm")
        set_alarm.connect("clicked", self._on_set_alarm)
        self._enabled = Gtk.CheckButton(label="Wakeup enabled")
        self._enabled.connect("toggled", self._on_toggle_enabled)
        actions.append(set_alarm)
        actions.append(self._enabled)
        self.append(actions)
        self.append(self._status)

        self.run_async(self._read_alarm, self._apply_alarm)
        GLib.timeout_add_seconds(1, self._tick)

    def _tick(self):
        self.run_async(self.service.get_rtc_time, self._show_time,
                       on_error=lambda _e: None)
        return True

    def _show_time(self, t):
        try:
            self._time.set_text(
                "UTC: %04d-%02d-%02d %02d:%02d:%02d"
                % (t["year"], t["month"], t["day"], t["hour"], t["minute"], t["second"]))
        except (KeyError, TypeError):
            pass

    def _read_alarm(self):
        return {"control": self.service.get_alarm_control(),
                "alarm": self.service.get_alarm()}

    def _apply_alarm(self, data):
        ctrl = data.get("control") or {}
        self._enabled.set_active(bool(ctrl.get("alarm_wakeup_enabled")))
        a = data.get("alarm") or {}
        if "weekday" in a:
            self._is_weekday_btn.set_active(True)
            self._set_day(a["weekday"])
        elif "day" in a:
            self._is_weekday.set_active(True)
            self._set_day(a["day"])
        if a.get("hour") == "EVERY_HOUR":
            self._every_hour.set_active(True)
        elif "hour" in a:
            self._hour.set_text(str(a["hour"]))
        if "minute_period" in a:
            self._is_period_btn.set_active(True)
            self._minute.set_text(str(a["minute_period"]))
        elif "minute" in a:
            self._is_period.set_active(True)
            self._minute.set_text(str(a["minute"]))
        if "second" in a:
            self._second.set_text(str(a["second"]))

    def _set_day(self, value):
        if value == "EVERY_DAY":
            self._every_day.set_active(True)
        else:
            self._day.set_text(str(value))

    def _on_set_time(self, _btn):
        now = datetime.datetime.utcnow()
        fields = {"second": now.second, "minute": now.minute, "hour": now.hour,
                  "weekday": (now.weekday() + 1) % 7 + 1, "day": now.day,
                  "month": now.month, "year": now.year, "subsecond": 0}
        self.run_async(lambda: self.service.set_rtc_time(fields),
                       lambda _r: self.flash("RTC time set."))

    def _on_set_alarm(self, _btn):
        alarm = {}
        try:
            alarm["second"] = int(self._second.get_text() or 0)
        except ValueError:
            alarm["second"] = 0
        if self._is_period_btn.get_active():
            alarm["minute_period"] = self._int(self._minute, 1)
        else:
            alarm["minute"] = self._int(self._minute, 0)
        alarm["hour"] = "EVERY_HOUR" if self._every_hour.get_active() \
            else self._hour.get_text().strip()
        key = "weekday" if self._is_weekday_btn.get_active() else "day"
        alarm[key] = "EVERY_DAY" if self._every_day.get_active() \
            else self._day.get_text().strip()
        self.run_async(lambda: self.service.set_alarm(alarm),
                       lambda _r: self.flash("Alarm set."))

    def _on_toggle_enabled(self, btn):
        want = btn.get_active()
        self.run_async(lambda: self.service.set_wakeup_enabled(want),
                       lambda _r: self.flash("Wakeup %s." % ("enabled" if want else "disabled")))

    @staticmethod
    def _int(entry, default):
        try:
            return int(entry.get_text())
        except ValueError:
            return default


# ── firmware view ────────────────────────────────────────────────────────────
class FirmwareView(_View):
    title = "Firmware"
    slug = "firmware"
    FW_DIR = "/usr/share/pijuice/data/firmware/"
    _RE = re.compile(r"PiJuice-V(\d+)\.(\d+)_(\d+_\d+_\d+)\.elf\.binary")

    def __init__(self, service):
        super().__init__(service)
        self._bin_file = None

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        grid.attach(Gtk.Label(label="Firmware version:", xalign=0), 0, 0, 1, 1)
        self._ver = Gtk.Label(label="…", xalign=0, selectable=True)
        grid.attach(self._ver, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Update file:", xalign=0), 0, 1, 1, 1)
        self._path = Gtk.Label(label="…", xalign=0, selectable=True, wrap=True)
        grid.attach(self._path, 1, 1, 1, 1)
        self.append(grid)

        self._fw_status = Gtk.Label(xalign=0)
        self.append(self._fw_status)

        self._update_btn = Gtk.Button(label="Update firmware")
        self._update_btn.set_sensitive(False)
        self._update_btn.connect("clicked", self._on_update)
        self.append(self._update_btn)

        # Inline confirm (avoids version-specific dialog APIs).
        self._confirm = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False)
        warn = Gtk.Label(xalign=0, wrap=True)
        warn.set_markup("<b>Warning:</b> interrupting a firmware update can leave the "
                        "PiJuice non-functional. Do not remove power.")
        confirm_btn = Gtk.Button(label="Flash now")
        confirm_btn.add_css_class("destructive-action")
        confirm_btn.connect("clicked", self._do_flash)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _b: self._confirm.set_visible(False))
        crow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        crow.append(confirm_btn)
        crow.append(cancel_btn)
        self._confirm.append(warn)
        self._confirm.append(crow)
        self.append(self._confirm)
        self.append(self._status)

        self._scan_file()
        self.refresh()

    def _scan_file(self):
        best = 0
        self._bin_file = None
        self._new_ver = None
        try:
            files = sorted(os.listdir(self.FW_DIR))
        except OSError:
            files = []
        for name in files:
            m = self._RE.match(name)
            if not m:
                continue
            ver = (int(m.group(1)) << 4) + int(m.group(2))
            if ver >= best:
                best = ver
                self._new_ver = "%d.%d" % (int(m.group(1)), int(m.group(2)))
                self._bin_file = os.path.join(self.FW_DIR, name)
        self._path.set_text(self._bin_file or "No firmware file found")

    def refresh(self):
        if not self.service.available:
            self._ver.set_text("no device")
            return
        self.run_async(lambda: self.service.firmware_version, self._apply_ver)

    def _apply_ver(self, fw):
        cur = (fw or {}).get("version") if isinstance(fw, dict) else None
        self._ver.set_text(cur or "unknown")
        cur_int = None
        if cur:
            try:
                major, minor = cur.split(".")
                cur_int = (int(major) << 4) + int(minor)
            except ValueError:
                cur_int = None
        new_int = None
        if self._new_ver:
            major, minor = self._new_ver.split(".")
            new_int = (int(major) << 4) + int(minor)
        if cur_int is not None and new_int is not None and new_int > cur_int:
            self._fw_status.set_text("New firmware V%s available." % self._new_ver)
            self._update_btn.set_sensitive(True)
        elif cur_int is not None and new_int is not None:
            self._fw_status.set_text("Firmware is up to date.")
        else:
            self._fw_status.set_text("No applicable update found.")

    def _on_update(self, _btn):
        # Guard: refuse on low charge with no external power (mirrors the Tk flow).
        def check():
            status = self.service.get_status()
            powered = (status.get("powerInput") == "PRESENT"
                       or status.get("powerInput5vIo") == "PRESENT")
            if powered:
                return True
            return self.service.get_charge_level() >= 20

        def done(ok):
            if ok:
                self._confirm.set_visible(True)
            else:
                self.flash("Charge level too low to update (connect power or charge >20%).")

        self.run_async(check, done)

    def _do_flash(self, _btn):
        if not self._bin_file:
            return
        self._confirm.set_visible(False)
        self._update_btn.set_sensitive(False)
        self.flash("Flashing… do not remove power.")
        self.run_async(lambda: self.service.flash_firmware(self._bin_file), self._flashed)

    def _flashed(self, rc):
        if rc == 0:
            self.flash("Firmware updated. Re-reading version…")
            self.service.connect()        # picks up the restarted firmware
            self.refresh()
        else:
            self.flash("Firmware update failed (pijuiceboot rc=%s)." % rc)
            self._update_btn.set_sensitive(True)


# ── main window / application ────────────────────────────────────────────────
class PiJuiceWindow(Gtk.ApplicationWindow):
    def __init__(self, service, **kwargs):
        super().__init__(title="PiJuice Settings", **kwargs)
        self.service = service
        self.set_default_size(600, 420)

        display = Gdk.Display.get_default()
        if display is not None:
            _install_css(display)

        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        sidebar = Gtk.StackSidebar(stack=stack)

        views = [
            StatusView(service),
            ButtonsView(service),
            LedView(service),
            SystemEventsView(service),
            UserScriptsView(service),
            BatteryView(service),
            IoView(service),
            WakeupView(service),
            SystemTaskView(service),
            FirmwareView(service),
        ]
        for view in views:
            scroll = Gtk.ScrolledWindow(child=view, hexpand=True, vexpand=True)
            stack.add_titled(scroll, view.slug, view.title)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.append(sidebar)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        box.append(stack)
        self.set_child(box)

        if not service.available:
            header.set_title_widget(Gtk.Label(label="PiJuice Settings — no device"))


class PiJuiceApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.service = None

    def do_activate(self):
        if self.service is None:
            self.service = PiJuiceService()
        win = self.get_active_window()
        if win is None:
            win = PiJuiceWindow(self.service, application=self)
        win.present()

    def do_shutdown(self):
        if self.service is not None:
            self.service.close()
        Gtk.Application.do_shutdown(self)


def main():
    app = PiJuiceApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
