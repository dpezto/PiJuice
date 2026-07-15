#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""GTK4 / libadwaita front-end for the PiJuice HAT (Wayland-native).

Design:
  * libadwaita gives a HIG-consistent look and **follows the system light/dark
    scheme automatically** — using ``Adw.Application`` is the whole of the dark
    mode support, no custom CSS. Each tab is an ``Adw.PreferencesPage`` of
    ``Adw.PreferencesGroup`` rows (``ActionRow``/``ComboRow``/``EntryRow`` +
    ``Gtk.Switch``/``SpinButton`` suffixes); a ``Gtk.Stack`` + ``StackSidebar``
    switches between them.
  * All HAT access goes through :class:`pijuice_service.PiJuiceService`. No widget
    ever touches I2C or the JSON config directly — that is the decoupling. Reads
    run on the service's worker thread and results are marshalled back to the GTK
    main loop with ``GLib.idle_add`` (see :meth:`_View.run_async`).

Note: ``Adw.SwitchRow`` / ``Adw.SpinRow`` need libadwaita 1.4; Raspberry Pi OS
Bookworm ships 1.2, so we use ``Adw.ActionRow`` + a suffix widget instead.

Run on the device:  python3 pijuice_gtk.py   (--selftest builds the window and exits)
"""

import datetime
import os
import re
import sys

# The Pi has no a11y D-Bus; skip it to avoid a noisy startup warning.
os.environ.setdefault("GTK_A11Y", "none")

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from pijuice_service import (  # noqa: E402
    LED_USER_SELECTABLE,
    PiJuiceError,
    PiJuiceService,
)

APP_ID = "org.pisupply.PiJuice"

_H = Gtk.Orientation.HORIZONTAL
_V = Gtk.Orientation.VERTICAL
_CENTER = Gtk.Align.CENTER


def _portal_color_scheme():
    """Desktop light/dark preference: 1 = dark, 2 = light, 0 = no preference.

    This is what GNOME/KDE (and any compliant compositor) expose; libadwaita
    follows it automatically. Raspberry Pi OS does not set it (returns 0).
    """
    try:
        proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Settings",
            None,
        )
        result = proxy.call_sync(
            "Read",
            GLib.Variant("(ss)", ("org.freedesktop.appearance", "color-scheme")),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        # Read returns (v); the value is often doubly variant-wrapped (v of v).
        value = result.get_child_value(0)
        while value.get_type_string() == "v":
            value = value.get_variant()
        if value.get_type_string() == "u":
            return value.get_uint32()
        return 0
    except GLib.Error:
        return 0


def _theme_name_is_dark():
    """Infer dark mode from the GTK theme name, for desktops with no portal
    preference (Raspberry Pi OS: ``PiXnoir`` = dark, ``PiXflat`` = light).

    Must be read *before* ``Adw.init`` masks ``gtk-theme-name`` to
    ``Adwaita-empty``. Returns True/False, or None when undeterminable.
    """
    settings = Gtk.Settings.get_default()
    if settings is None:
        return None
    if settings.get_property("gtk-application-prefer-dark-theme"):
        return True
    name = settings.get_property("gtk-theme-name") or ""
    low = name.lower()
    if "dark" in low or "noir" in low:
        return True
    return False if name else None


# Raspberry Pi OS icon themes ship no ``document-edit-symbolic``, so Adw.EntryRow's
# always-on edit affordance renders as a broken-image glyph. Blank it.
_CSS = b".edit-icon { opacity: 0; min-width: 0; min-height: 0; margin: 0; padding: 0; }"
_css_installed = False


def _install_css(display):
    global _css_installed
    if _css_installed or display is None:
        return
    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(_CSS)  # GTK < 4.12
    except TypeError:
        provider.load_from_string(_CSS.decode())  # GTK >= 4.12
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _css_installed = True


# ── base view ────────────────────────────────────────────────────────────────
class _View(Adw.PreferencesPage):
    """Base page: the service, the thread-marshalling helper, and the shared
    Adwaita row builders that used to be copy-pasted into every tab."""

    title = "View"
    slug = "view"

    def __init__(self, service):
        super().__init__()
        self.service = service
        self._status = Gtk.Label(xalign=0, wrap=True)
        self._status.add_css_class("dim-label")

    # --- threading -----------------------------------------------------------
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

    def require_device(self):
        """Bail out with a message when no HAT is present. Returns availability."""
        if self.service.available:
            return True
        self.flash("No PiJuice detected.")
        self.add_status()
        return False

    def _saved(self, rc):
        self.flash(
            "Saved." if rc == 0 else "Saved; service notify failed (rc=%s)." % rc
        )

    @staticmethod
    def _int(entry, default):
        try:
            return int(entry.get_text())
        except ValueError:
            return default

    # --- Adwaita row builders ------------------------------------------------
    def add_group(self, title=None, description=None):
        group = Adw.PreferencesGroup()
        if title:
            group.set_title(title)
        if description:
            group.set_description(description)
        self.add(group)
        return group

    def add_status(self):
        group = Adw.PreferencesGroup()
        group.add(self._status)
        self.add(group)

    def set_actions(self, group, apply_cb=None, refresh_cb=None, apply_label="Apply"):
        box = Gtk.Box(orientation=_H, spacing=6, valign=_CENTER)
        if refresh_cb is not None:
            btn = Gtk.Button(label="Refresh")
            btn.add_css_class("flat")
            btn.connect("clicked", lambda _b: refresh_cb())
            box.append(btn)
        if apply_cb is not None:
            btn = Gtk.Button(label=apply_label)
            btn.add_css_class("suggested-action")
            btn.connect("clicked", apply_cb)
            box.append(btn)
        group.set_header_suffix(box)

    def combo_row(self, group, title, strings, subtitle=None):
        row = Adw.ComboRow(title=title, model=Gtk.StringList.new(list(strings)))
        if subtitle:
            row.set_subtitle(subtitle)
        group.add(row)
        return row

    def switch_row(self, group, title, subtitle=None, active=False):
        row = Adw.ActionRow(title=title)
        if subtitle:
            row.set_subtitle(subtitle)
        switch = Gtk.Switch(active=active, valign=_CENTER)
        row.add_suffix(switch)
        row.set_activatable_widget(switch)
        group.add(row)
        return row, switch

    def spin_row(self, group, title, lo, hi, step=1, value=0):
        adj = Gtk.Adjustment(
            lower=lo, upper=hi, step_increment=step, page_increment=step * 10 or 1
        )
        spin = Gtk.SpinButton(adjustment=adj, numeric=True, valign=_CENTER)
        spin.set_value(value)
        row = Adw.ActionRow(title=title)
        row.add_suffix(spin)
        row.set_activatable_widget(spin)
        group.add(row)
        return row, spin

    def value_row(self, group, title):
        """Read-only ActionRow whose suffix Label is returned for live updates."""
        row = Adw.ActionRow(title=title)
        label = Gtk.Label(label="…", xalign=1, selectable=True, wrap=True)
        label.add_css_class("dim-label")
        row.add_suffix(label)
        group.add(row)
        return label

    @staticmethod
    def combo_get(row, options, default=None):
        i = row.get_selected()
        return options[i] if 0 <= i < len(options) else default

    @staticmethod
    def combo_set(row, value, options):
        if value in options:
            row.set_selected(options.index(value))


# ── status view ──────────────────────────────────────────────────────────────
class StatusView(_View):
    title = "Status"
    slug = "status"

    def __init__(self, service):
        super().__init__(service)
        self._labels = {}
        group = self.add_group("Status")
        fields = [
            ("battery", "Battery"),
            ("gpio", "GPIO power input"),
            ("usb", "USB micro power"),
            ("fault", "Fault"),
            ("sys_sw", "System switch"),
        ]
        for key, label in fields:
            self._labels[key] = self.value_row(group, label)

        switch_group = self.add_group("System power switch")
        self._switch_values = [0, 500, 2100]
        self._switch = self.combo_row(
            switch_group, "Switch state", ["Off", "500 mA", "2100 mA"]
        )
        set_btn = Gtk.Button(label="Set")
        set_btn.add_css_class("suggested-action")
        set_btn.connect("clicked", self._on_set_switch)
        switch_group.set_header_suffix(set_btn)
        self.add_status()

        if not self.service.available:
            self.flash("No PiJuice detected.")
        else:
            self.refresh()
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
            out["gpio"] = "%.3fV, %.3fA, %s" % (
                iov,
                ioc,
                status.get("powerInput5vIo", "N/A"),
            )
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
            lambda _r: self.flash(
                "System switch set to %s." % ("Off" if not value else "%dmA" % value)
            ),
        )


# ── LED view (includes the B1a "red disables green" fix) ─────────────────────
class LedView(_View):
    title = "LEDs"
    slug = "leds"

    def __init__(self, service):
        super().__init__(service)
        self._rows = {}

        head = self.add_group(
            "LEDs",
            description=(
                "Set a LED to USER_LED to control its colour directly. In "
                "CHARGE_STATUS the firmware drives R/G itself, which is why a "
                "manual colour appears to “disable” a channel. “Test colour” "
                "forces USER_LED first."
            ),
        )
        if not self.require_device():
            return
        self.set_actions(head, self._on_apply, self.refresh)

        for led in self.service.leds:
            self._build_led_group(led)
        self.add_status()
        self.refresh()

    def _build_led_group(self, led):
        group = self.add_group(led)
        func = self.combo_row(group, "Function", LED_USER_SELECTABLE)
        r = self.spin_row(group, "Red", 0, 255)[1]
        g = self.spin_row(group, "Green", 0, 255)[1]
        b = self.spin_row(group, "Blue", 0, 255)[1]
        test = Gtk.Button(label="Test colour")
        test.add_css_class("flat")
        test.connect("clicked", self._on_test, led)
        group.set_header_suffix(test)
        self._rows[led] = {"function": func, "r": r, "g": g, "b": b}

    def refresh(self):
        for led in self._rows:
            self.run_async(
                lambda led=led: (led, self.service.get_led_config(led)), self._apply_one
            )

    def _apply_one(self, pair):
        led, cfg = pair
        row = self._rows.get(led)
        if not row or not cfg:
            return
        self.combo_set(row["function"], cfg.get("function", "NOT_USED"), LED_USER_SELECTABLE)
        param = cfg.get("parameter", {})
        for ch in ("r", "g", "b"):
            try:
                row[ch].set_value(int(param.get(ch, 0)))
            except (TypeError, ValueError):
                row[ch].set_value(0)

    def _row_config(self, led):
        row = self._rows[led]
        function = self.combo_get(row["function"], LED_USER_SELECTABLE, "NOT_USED")
        return {
            "function": function,
            "parameter": {ch: row[ch].get_value_as_int() for ch in ("r", "g", "b")},
        }

    def _on_apply(self, _btn):
        for led in self._rows:
            cfg = self._row_config(led)
            self.run_async(
                lambda led=led, cfg=cfg: self.service.set_led_config(led, cfg),
                lambda _r: self.flash("LED settings applied."),
            )

    def _on_test(self, _btn, led):
        row = self._rows[led]
        rgb = [row[ch].get_value_as_int() for ch in ("r", "g", "b")]
        # set_led_color enforces USER_LED first — the actual bug fix.
        self.run_async(
            lambda: self.service.set_led_color(led, rgb),
            lambda _r: self.flash("%s -> rgb%s (USER_LED)." % (led, tuple(rgb))),
        )


# ── buttons view ─────────────────────────────────────────────────────────────
class ButtonsView(_View):
    title = "Buttons"
    slug = "buttons"

    def __init__(self, service):
        super().__init__(service)
        self._cells = {}

        head = self.add_group("Buttons")
        if not self.require_device():
            return

        from pijuice import (
            pijuice_hard_functions,
            pijuice_sys_functions,
            pijuice_user_functions,
        )

        self._functions = (
            ["NO_FUNC"]
            + list(pijuice_hard_functions)
            + list(pijuice_sys_functions)
            + list(pijuice_user_functions)
        )
        self.set_actions(head, self._on_apply, self.refresh)

        for button in self.service.buttons:
            group = self.add_group(button)
            for event in self.service.button_events:
                row = self.combo_row(group, event, self._functions)
                param = Gtk.Entry(text="0", width_chars=4, valign=_CENTER)
                row.add_suffix(param)
                self._cells[(button, event)] = (row, param)
        self.add_status()
        self.refresh()

    def refresh(self):
        for button in self.service.buttons:
            self.run_async(
                lambda b=button: (b, self.service.get_button_config(b)),
                self._apply_button,
            )

    def _apply_button(self, pair):
        button, cfg = pair
        if not cfg:
            return
        for event, conf in cfg.items():
            cell = self._cells.get((button, event))
            if not cell:
                continue
            func, param = cell
            self.combo_set(func, conf.get("function", "NO_FUNC"), self._functions)
            param.set_text(str(conf.get("parameter", 0)))

    def _on_apply(self, _btn):
        for button in self.service.buttons:
            config = {}
            for event in self.service.button_events:
                func, param = self._cells[(button, event)]
                fn = self.combo_get(func, self._functions, "NO_FUNC")
                try:
                    pval = int(param.get_text())
                except ValueError:
                    pval = 0
                config[event] = {"function": fn, "parameter": pval}
            self.run_async(
                lambda b=button, c=config: self.service.set_button_config(b, c),
                lambda _r: self.flash("Button settings applied."),
            )


# ── user scripts view (config JSON) ──────────────────────────────────────────
class UserScriptsView(_View):
    title = "User Scripts"
    slug = "userscripts"
    COUNT = 15

    def __init__(self, service):
        super().__init__(service)
        cfg = self.service.config.setdefault("user_functions", {})
        self._entries = {}

        head = self.add_group(
            "User Scripts",
            description=(
                "Each USER_FUNCx runs as the pijuice user when a button or system "
                "event is mapped to it. Use an absolute path (blank = unused); the "
                "service is reloaded on Apply."
            ),
        )
        self.set_actions(head, self._on_apply)

        group = self.add_group()
        self._chooser = None  # keep a FileChooserNative alive while it is open
        for i in range(self.COUNT):
            key = "USER_FUNC%d" % (i + 1)
            row = Adw.EntryRow(title=key)
            row.set_text(str(cfg.get(key, "")))
            browse = Gtk.Button(icon_name="document-open-symbolic", valign=_CENTER)
            browse.add_css_class("flat")
            browse.set_tooltip_text("Browse for a script")
            browse.connect("clicked", self._on_browse, row)
            row.add_suffix(browse)
            group.add(row)
            self._entries[key] = row
        self.add_status()

    def _on_browse(self, _btn, row):
        # FileChooserNative uses the desktop portal, so it works on Wayland and
        # GTK 4.8 (Gtk.FileDialog needs 4.10). Same idea as the CLI's file picker.
        chooser = Gtk.FileChooserNative(
            title="Select a script",
            action=Gtk.FileChooserAction.OPEN,
            transient_for=self.get_root(),
            accept_label="_Select",
            cancel_label="_Cancel",
        )
        current = row.get_text().strip()
        start = os.path.dirname(current) if current else "/usr/local/bin"
        if os.path.isdir(start):
            try:
                chooser.set_current_folder(Gio.File.new_for_path(start))
            except GLib.Error:
                pass
        chooser.connect("response", self._on_browse_done, row)
        self._chooser = chooser
        chooser.show()

    def _on_browse_done(self, chooser, response, row):
        if response == Gtk.ResponseType.ACCEPT:
            gfile = chooser.get_file()
            if gfile is not None and gfile.get_path():
                row.set_text(gfile.get_path())
        chooser.destroy()
        self._chooser = None

    def _on_apply(self, _btn):
        cfg = self.service.config.setdefault("user_functions", {})
        for key, entry in self._entries.items():
            cfg[key] = entry.get_text().strip()
        self.run_async(self.service.save_and_notify, self._saved)


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

        self._functions = (
            ["NO_FUNC"] + list(pijuice_sys_functions) + list(pijuice_user_functions)
        )
        events_cfg = self.service.config.setdefault("system_events", {})
        self._rows = {}

        head = self.add_group("System Events")
        self.set_actions(head, self._on_apply)

        group = self.add_group()
        for key, text in self.EVENTS:
            ev = events_cfg.setdefault(key, {})
            ev.setdefault("enabled", False)
            ev.setdefault("function", "NO_FUNC")
            row = self.combo_row(group, text, self._functions)
            self.combo_set(row, ev["function"], self._functions)
            switch = Gtk.Switch(active=bool(ev["enabled"]), valign=_CENTER)
            row.add_prefix(switch)
            self._rows[key] = (switch, row)
        self.add_status()

    def _on_apply(self, _btn):
        cfg = self.service.config.setdefault("system_events", {})
        for key, (switch, row) in self._rows.items():
            fn = self.combo_get(row, self._functions, "NO_FUNC")
            cfg[key] = {"enabled": switch.get_active(), "function": fn}
        self.run_async(self.service.save_and_notify, self._saved)


# ── system task view (config JSON) ───────────────────────────────────────────
class SystemTaskView(_View):
    title = "System Task"
    slug = "systask"
    # section, label, value field, value label, type, min, max
    PARAMS = [
        ("watchdog", "Watchdog", "period", "Expire period [minutes]", int, 1, 65535),
        (
            "wakeup_on_charge",
            "Wakeup on charge",
            "trigger_level",
            "Trigger level [%]",
            int,
            0,
            100,
        ),
        ("min_charge", "Minimum charge", "threshold", "Threshold [%]", int, 0, 100),
        (
            "min_bat_voltage",
            "Minimum battery voltage",
            "threshold",
            "Threshold [V]",
            float,
            0,
            10,
        ),
        (
            "ext_halt_power_off",
            "Software halt power off",
            "period",
            "Delay period [seconds]",
            int,
            20,
            65535,
        ),
    ]
    # ponytail: the FW>=0x15 non-volatile "restore" watchdog/wakeup toggles are
    # omitted (they need an I2C SetWatchdog write); the daemon re-applies the JSON
    # params each boot regardless. Add them with a service method if persistence
    # across power-loss is wanted.

    def __init__(self, service):
        super().__init__(service)
        st = self.service.config.setdefault("system_task", {})

        head = self.add_group("System Task")
        self.set_actions(head, self._on_apply)
        _, self._enabled = self.switch_row(
            head, "System task enabled", active=bool(st.get("enabled", False))
        )

        self._rows = {}
        group = self.add_group()
        for sec, label, field, vlabel, typ, lo, hi in self.PARAMS:
            secd = st.setdefault(sec, {})
            row = Adw.ActionRow(title=label, subtitle=vlabel)
            switch = Gtk.Switch(active=bool(secd.get("enabled", False)), valign=_CENTER)
            row.add_prefix(switch)
            entry = Gtk.Entry(
                text=str(secd.get(field, "")), width_chars=8, valign=_CENTER
            )
            row.add_suffix(entry)
            group.add(row)
            self._rows[sec] = (switch, entry, field, typ, lo, hi)
        self.add_status()

    def _on_apply(self, _btn):
        st = self.service.config.setdefault("system_task", {})
        st["enabled"] = self._enabled.get_active()
        bad = []
        for sec, (switch, entry, field, typ, lo, hi) in self._rows.items():
            secd = st.setdefault(sec, {})
            secd["enabled"] = switch.get_active()
            text = entry.get_text().strip()
            if text == "":
                secd.pop(field, None)  # value unset
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
        self.add_group("Battery")  # placeholder header for the no-device message
        if not service.available:
            self.flash("No PiJuice detected.")
            self.add_status()
            return

        self._profiles = service.get_battery_profiles()
        head = self.add_group()
        self.set_actions(head, self._on_apply, self.refresh)
        self._profile = self.combo_row(head, "Profile", self._profiles)
        self._pstatus = self.value_row(head, "Status")
        self._temp = self.combo_row(
            head, "Temperature sense", service.battery_temp_sense_options
        )
        self._rsoc = None
        if service.fw_int >= 0x13:
            self._rsoc = self.combo_row(
                head, "RSoC estimation", service.rsoc_estimation_options
            )
        _, self._charging = self.switch_row(head, "Charging enabled")
        self.add_status()
        self.refresh()

    def refresh(self):
        self.run_async(self._read, self._apply)

    def _read(self):
        out = {}
        for key, fn in (
            ("status", self.service.get_battery_profile_status),
            ("temp", self.service.get_battery_temp_sense),
            ("charging", self.service.get_charging_config),
        ):
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
            self._pstatus.set_text(
                "%s · %s · %s"
                % (
                    st.get("profile", "?"),
                    st.get("validity", "?"),
                    st.get("source", "?"),
                )
            )
            self.combo_set(self._profile, st.get("profile"), self._profiles)
        opts = self.service.battery_temp_sense_options
        self.combo_set(self._temp, data.get("temp"), opts)
        if self._rsoc is not None:
            self.combo_set(
                self._rsoc, data.get("rsoc"), self.service.rsoc_estimation_options
            )
        ch = data.get("charging")
        if isinstance(ch, dict):
            self._charging.set_active(bool(ch.get("charging_enabled")))

    def _on_apply(self, _btn):
        profile = self.combo_get(self._profile, self._profiles)
        temp = self.combo_get(self._temp, self.service.battery_temp_sense_options)
        rsoc = (
            self.combo_get(self._rsoc, self.service.rsoc_estimation_options)
            if self._rsoc is not None
            else None
        )
        charging = self._charging.get_active()

        def work():
            if profile:
                self.service.set_battery_profile(profile)
            self.service.set_battery_temp_sense(temp)
            if rsoc is not None:
                self.service.set_rsoc_estimation(rsoc)
            self.service.set_charging_config(charging)
            return True

        self.run_async(
            work, lambda _r: (self.flash("Battery settings applied."), self.refresh())
        )


# ── IO view ──────────────────────────────────────────────────────────────────
class IoView(_View):
    title = "IO"
    slug = "io"

    def __init__(self, service):
        super().__init__(service)
        head = self.add_group("IO")
        if not service.available:
            self.flash("No PiJuice detected.")
            self.add_status()
            return
        self.set_actions(head, self._on_apply, self.refresh)

        self._pins = {}
        for pin in (1, 2):
            self._build_pin(pin)
        self.add_status()
        self.refresh()

    def _build_pin(self, pin):
        group = self.add_group("IO%d" % pin)
        modes = self.service.io_supported_modes(pin)
        mode_dd = self.combo_row(group, "Mode", modes)
        pull_dd = self.combo_row(group, "Pull", self.service.io_pull_options)
        self._pins[pin] = {
            "group": group,
            "modes": modes,
            "mode": mode_dd,
            "pull": pull_dd,
            "param_rows": [],
            "params": [],
            "data": {},
        }
        mode_dd.connect(
            "notify::selected", lambda *_a, pin=pin: self._rebuild_params(pin)
        )

    def _rebuild_params(self, pin):
        p = self._pins[pin]
        group = p["group"]
        for row in p["param_rows"]:
            group.remove(row)
        p["param_rows"] = []
        p["params"] = []
        mode = p["modes"][p["mode"].get_selected()]
        for pcfg in self.service.io_config_params.get(mode, []):
            if pcfg["type"] == "enum":
                row = Adw.ComboRow(
                    title=pcfg["name"], model=Gtk.StringList.new(pcfg["options"])
                )
            else:
                unit = (" " + pcfg["unit"]) if pcfg.get("unit") else ""
                row = Adw.EntryRow(
                    title="%s [%s-%s%s]" % (pcfg["name"], pcfg["min"], pcfg["max"], unit)
                )
            group.add(row)
            p["param_rows"].append(row)
            p["params"].append((pcfg, row))
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
            self.run_async(
                lambda pin=pin: (pin, self.service.get_io_config(pin)), self._apply_pin
            )

    def _apply_pin(self, pair):
        pin, cfg = pair
        if not cfg:
            return
        p = self._pins[pin]
        p["data"] = cfg
        self.combo_set(p["mode"], cfg.get("mode"), p["modes"])
        self.combo_set(p["pull"], cfg.get("pull"), self.service.io_pull_options)
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
                        cfg[pcfg["name"]] = (
                            float(text) if pcfg["type"] == "float" else int(text)
                        )
                    except ValueError:
                        cfg[pcfg["name"]] = pcfg["min"]
            self.run_async(
                lambda pin=pin, cfg=cfg: self.service.set_io_config(pin, cfg),
                lambda _r: self.flash("IO settings applied."),
            )


# ── wakeup alarm view ────────────────────────────────────────────────────────
class WakeupView(_View):
    title = "Wakeup Alarm"
    slug = "wakeup"
    _DAY_TYPES = ["Day of month", "Weekday"]
    _MIN_TYPES = ["Minute", "Minutes period"]

    def __init__(self, service):
        super().__init__(service)
        self.add_group("Wakeup Alarm")  # header (+ no-device message)
        if not service.available:
            self.flash("No PiJuice detected.")
            self.add_status()
            return

        clock = self.add_group("RTC clock")
        self._time = self.value_row(clock, "RTC time (UTC)")
        self._time.remove_css_class("dim-label")
        self._time.add_css_class("monospace")
        set_time = Gtk.Button(label="Set from Pi")
        set_time.add_css_class("flat")
        set_time.connect("clicked", self._on_set_time)
        clock.set_header_suffix(set_time)

        alarm = self.add_group("Alarm")
        self._daytype = self.combo_row(alarm, "Day type", self._DAY_TYPES)
        self._day = Adw.EntryRow(title="Day value (1-31 / 1-7)")
        alarm.add(self._day)
        _, self._every_day = self.switch_row(alarm, "Every day")
        self._hour = Adw.EntryRow(title="Hour")
        alarm.add(self._hour)
        _, self._every_hour = self.switch_row(alarm, "Every hour")
        self._mintype = self.combo_row(alarm, "Minute type", self._MIN_TYPES)
        self._minute = Adw.EntryRow(title="Minute value")
        alarm.add(self._minute)
        self._second = Adw.EntryRow(title="Second")
        self._second.set_text("0")
        alarm.add(self._second)

        control = self.add_group("Control")
        _, self._enabled = self.switch_row(control, "Wakeup enabled")
        self._enabled.connect("state-set", self._on_toggle_enabled)
        set_alarm = Gtk.Button(label="Set Alarm")
        set_alarm.add_css_class("suggested-action")
        set_alarm.connect("clicked", self._on_set_alarm)
        control.set_header_suffix(set_alarm)
        self.add_status()

        self.run_async(self._read_alarm, self._apply_alarm)
        GLib.timeout_add_seconds(1, self._tick)

    def _tick(self):
        self.run_async(
            self.service.get_rtc_time, self._show_time, on_error=lambda _e: None
        )
        return True

    def _show_time(self, t):
        try:
            self._time.set_text(
                "%04d-%02d-%02d %02d:%02d:%02d"
                % (t["year"], t["month"], t["day"], t["hour"], t["minute"], t["second"])
            )
        except (KeyError, TypeError):
            pass

    def _read_alarm(self):
        return {
            "control": self.service.get_alarm_control(),
            "alarm": self.service.get_alarm(),
        }

    def _apply_alarm(self, data):
        ctrl = data.get("control") or {}
        self._enabled.set_active(bool(ctrl.get("alarm_wakeup_enabled")))
        a = data.get("alarm") or {}
        if "weekday" in a:
            self._daytype.set_selected(1)
            self._set_day(a["weekday"])
        elif "day" in a:
            self._daytype.set_selected(0)
            self._set_day(a["day"])
        if a.get("hour") == "EVERY_HOUR":
            self._every_hour.set_active(True)
        elif "hour" in a:
            self._hour.set_text(str(a["hour"]))
        if "minute_period" in a:
            self._mintype.set_selected(1)
            self._minute.set_text(str(a["minute_period"]))
        elif "minute" in a:
            self._mintype.set_selected(0)
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
        fields = {
            "second": now.second,
            "minute": now.minute,
            "hour": now.hour,
            "weekday": (now.weekday() + 1) % 7 + 1,
            "day": now.day,
            "month": now.month,
            "year": now.year,
            "subsecond": 0,
        }
        self.run_async(
            lambda: self.service.set_rtc_time(fields),
            lambda _r: self.flash("RTC time set."),
        )

    def _on_set_alarm(self, _btn):
        alarm = {}
        try:
            alarm["second"] = int(self._second.get_text() or 0)
        except ValueError:
            alarm["second"] = 0
        if self._mintype.get_selected() == 1:
            alarm["minute_period"] = self._int(self._minute, 1)
        else:
            alarm["minute"] = self._int(self._minute, 0)
        alarm["hour"] = (
            "EVERY_HOUR"
            if self._every_hour.get_active()
            else self._hour.get_text().strip()
        )
        key = "weekday" if self._daytype.get_selected() == 1 else "day"
        alarm[key] = (
            "EVERY_DAY"
            if self._every_day.get_active()
            else self._day.get_text().strip()
        )
        self.run_async(
            lambda: self.service.set_alarm(alarm), lambda _r: self.flash("Alarm set.")
        )

    def _on_toggle_enabled(self, _switch, state):
        self.run_async(
            lambda: self.service.set_wakeup_enabled(state),
            lambda _r: self.flash("Wakeup %s." % ("enabled" if state else "disabled")),
        )
        return False  # let the switch update its visual state


# ── firmware view ────────────────────────────────────────────────────────────
class FirmwareView(_View):
    title = "Firmware"
    slug = "firmware"
    FW_DIR = "/usr/share/pijuice/data/firmware/"
    _RE = re.compile(r"PiJuice-V(\d+)\.(\d+)_(\d+_\d+_\d+)\.elf\.binary")

    def __init__(self, service):
        super().__init__(service)
        self._bin_file = None

        group = self.add_group("Firmware")
        self._ver = self.value_row(group, "Installed version")
        self._path = self.value_row(group, "Update file")
        self._update_btn = Gtk.Button(label="Update firmware")
        self._update_btn.set_sensitive(False)
        self._update_btn.add_css_class("suggested-action")
        self._update_btn.connect("clicked", self._on_update)
        group.set_header_suffix(self._update_btn)

        status_group = self.add_group()
        self._fw_status = Gtk.Label(xalign=0, wrap=True)
        status_group.add(self._fw_status)

        # Inline confirm group, hidden until an update is requested.
        self._confirm = self.add_group(
            "Confirm firmware update",
            description=(
                "Interrupting a firmware update can leave the PiJuice "
                "non-functional. Do not remove power."
            ),
        )
        self._confirm.set_visible(False)
        confirm_btn = Gtk.Button(label="Flash now")
        confirm_btn.add_css_class("destructive-action")
        confirm_btn.connect("clicked", self._do_flash)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.add_css_class("flat")
        cancel_btn.connect("clicked", lambda _b: self._confirm.set_visible(False))
        crow = Gtk.Box(orientation=_H, spacing=6, valign=_CENTER)
        crow.append(cancel_btn)
        crow.append(confirm_btn)
        self._confirm.set_header_suffix(crow)
        self.add_status()

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
            powered = (
                status.get("powerInput") == "PRESENT"
                or status.get("powerInput5vIo") == "PRESENT"
            )
            if powered:
                return True
            return self.service.get_charge_level() >= 20

        def done(ok):
            if ok:
                self._confirm.set_visible(True)
            else:
                self.flash(
                    "Charge level too low to update (connect power or charge >20%)."
                )

        self.run_async(check, done)

    def _do_flash(self, _btn):
        if not self._bin_file:
            return
        self._confirm.set_visible(False)
        self._update_btn.set_sensitive(False)
        self.flash("Flashing… do not remove power.")
        self.run_async(
            lambda: self.service.flash_firmware(self._bin_file), self._flashed
        )

    def _flashed(self, rc):
        if rc == 0:
            self.flash("Firmware updated. Re-reading version…")
            self.service.connect()  # picks up the restarted firmware
            self.refresh()
        else:
            self.flash("Firmware update failed (pijuiceboot rc=%s)." % rc)
            self._update_btn.set_sensitive(True)


VIEW_CLASSES = [
    StatusView,
    ButtonsView,
    LedView,
    SystemEventsView,
    UserScriptsView,
    BatteryView,
    IoView,
    WakeupView,
    SystemTaskView,
    FirmwareView,
]


# ── main window / application ────────────────────────────────────────────────
class PiJuiceWindow(Adw.ApplicationWindow):
    def __init__(self, service, **kwargs):
        super().__init__(title="PiJuice Settings", **kwargs)
        self.service = service
        self.set_default_size(640, 480)
        _install_css(self.get_display())

        header = Adw.HeaderBar()

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True)
        sidebar = Gtk.StackSidebar(stack=self.stack)
        sidebar.set_size_request(170, -1)
        for view_cls in VIEW_CLASSES:
            view = view_cls(service)
            self.stack.add_titled(view, view.slug, view.title)

        content = Gtk.Box(orientation=_H)
        content.set_vexpand(True)
        content.append(sidebar)
        content.append(Gtk.Separator(orientation=_V))
        content.append(self.stack)

        outer = Gtk.Box(orientation=_V)
        outer.append(header)
        outer.append(content)
        self.set_content(outer)

        if not service.available:
            header.set_title_widget(Gtk.Label(label="PiJuice Settings — no device"))


class PiJuiceApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.service = None
        # Captured before Adw.init masks the GTK theme name (set in main()).
        self.theme_dark = None

    def _apply_system_theme(self):
        # A desktop with a real color-scheme preference (GNOME/KDE) is followed
        # live by libadwaita on its own -- leave the default scheme alone. Only
        # when there is no preference (Raspberry Pi OS) do we map the GTK theme
        # name to a scheme ourselves.
        if _portal_color_scheme() != 0 or self.theme_dark is None:
            return
        style = Adw.StyleManager.get_default()
        style.set_color_scheme(
            Adw.ColorScheme.FORCE_DARK
            if self.theme_dark
            else Adw.ColorScheme.FORCE_LIGHT
        )

    def do_activate(self):
        if self.service is None:
            self.service = PiJuiceService()
        self._apply_system_theme()
        win = self.get_active_window()
        if win is None:
            win = PiJuiceWindow(self.service, application=self)
        win.present()

    def do_shutdown(self):
        if self.service is not None:
            self.service.close()
        Adw.Application.do_shutdown(self)


def _selftest():
    """Build the window off the bus and assert every page is present."""
    Adw.init()
    service = PiJuiceService()
    win = PiJuiceWindow(service)
    n = win.stack.get_pages().get_n_items()
    assert n == len(VIEW_CLASSES), "expected %d pages, built %d" % (
        len(VIEW_CLASSES),
        n,
    )
    service.close()
    print("selftest OK: %d views built" % n)
    return 0


def main():
    if "--selftest" in sys.argv:
        return _selftest()
    # Read the theme name now, before Adw.init masks it to "Adwaita-empty".
    theme_dark = _theme_name_is_dark()
    app = PiJuiceApplication()
    app.theme_dark = theme_dark
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
