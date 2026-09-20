#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""GTK4 / libadwaita front-end for the PiJuice HAT (Wayland-native).

Design:
  * libadwaita gives a HIG-consistent look and **follows the system light/dark
    scheme automatically**, with a theme-name fallback for Raspberry Pi OS. Each tab is an ``Adw.PreferencesPage`` of
    ``Adw.PreferencesGroup`` rows (``ActionRow``/``ComboRow``/``EntryRow`` +
    ``Gtk.Switch``/``SpinButton`` suffixes); a ``Gtk.Stack`` + ``StackSidebar``
    switches between them.
  * All HAT access goes through :class:`pijuice_service.PiJuiceService`. No widget
    ever touches I2C or the config file directly — that is the decoupling. Reads
    run on the service's worker thread and results are marshalled back to the GTK
    main loop with ``GLib.idle_add`` (see :meth:`_View.run_async`).

Note: ``Adw.SwitchRow`` / ``Adw.SpinRow`` need libadwaita 1.4; Raspberry Pi OS
Bookworm ships 1.2, so we use ``Adw.ActionRow`` + a suffix widget instead.

Run on the device:  python3 pijuice_gtk.py   (--selftest builds the window and exits)
"""

import copy
import datetime
import os
import re
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from pijuice_battery import profile_label

from pijuice_service import (  # noqa: E402
    LED_USER_SELECTABLE,
    PiJuiceError,
    PiJuiceService,
    alarm_fields,
    function_description,
    function_label,
    pack_version,
    readable,
    user_function_names,
    schedule_values,
    rtc_fields_now,
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
            1500,
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

    def __init__(self, service):
        super().__init__()
        self.service = service
        self._status = Gtk.Label(xalign=0, wrap=True)
        self._status.add_css_class("dim-label")
        self._pending = 0
        self._writing = False
        self._applying = False
        self._loading = False
        self._disposed = False
        self._timers = []
        self._baseline = {}
        self._tracked = set()
        self._actions = None
        self._dialog = None
        self._status_added = False
        self._built_available = service.available
        GLib.idle_add(self._capture)


    @staticmethod
    def _controls(widget):
        # Stop at composite controls: their internal entries are implementation details.
        for typ, prop in ((Adw.ComboRow, "selected"), (Gtk.SpinButton, "value"),
                          (Adw.EntryRow, "text"), (Gtk.Entry, "text"),
                          (Gtk.Switch, "active")):
            if isinstance(widget, typ):
                if not getattr(widget, "_immediate", False):
                    yield widget, prop
                if not isinstance(widget, Adw.ComboRow):
                    return
                break
        if isinstance(widget, Gtk.Popover):
            return
        child = widget.get_first_child()
        while child:
            yield from _View._controls(child)
            child = child.get_next_sibling()

    @property
    def dirty(self):
        return any(w.get_property(prop) != value for (w, prop), value in self._baseline.items())

    def _capture(self):
        root = self.get_root()
        self._tracked = {w for w in self._tracked if w.get_root() == root}
        if self._disposed:
            return False
        self._baseline = {(w, prop): w.get_property(prop)
                          for w, prop in self._controls(self)}
        for w, prop in self._baseline:
            if w not in self._tracked:
                w.connect("notify::" + prop, self._changed)
                self._tracked.add(w)
        self._changed()
        return False

    def _changed(self, *_args):
        if self._loading:
            return
        if self._actions:
            self._actions.set_visible(self.dirty)
        if self.dirty:
            self.flash("Unsaved changes")

    def _discard(self, *_args):
        self._loading = True
        # Restoring a mode can rebuild dependent controls; refresh those from device.
        for (w, prop), value in list(self._baseline.items()):
            w.set_property(prop, value)
            w.remove_css_class("error")
        self._loading = False
        self._capture()
        if hasattr(self, "refresh"):
            self.refresh()
        self.flash("Changes discarded.")

    def poll(self, seconds, callback):
        def tick():
            if self._disposed:
                return False
            if not self._pending and not self._writing and self.get_mapped():
                callback()
            return True
        self._timers.append(GLib.timeout_add_seconds(seconds, tick))

    def dispose_view(self):
        self._disposed = True
        for timer in self._timers:
            GLib.source_remove(timer)
        self._timers.clear()

    def run_async(self, fn, on_done=None, on_error=None, write=False, preserve=False, live=False):
        """Serialize device operations and settle every outcome on the GTK loop."""
        write = write or self._applying
        baseline = self._baseline.copy()
        if self._disposed or (write and self._writing):
            return
        if write:
            self._writing = True
            self.set_sensitive(False)
            self.flash("Applying…")
        self._pending += 1
        def settle(result=None, error=None):
            self._pending -= 1
            if self._disposed:
                return False
            if write:
                self._writing = False
                self.set_sensitive(True)
            if error is not None:
                if on_error:
                    on_error(error)
                else:
                    self.flash("Could not complete the operation: %s. Your edits are kept; retry when ready. Earlier steps may already have applied." % error)
            elif on_done:
                # Background reads must never replace a draft.
                if write or live or (not self.dirty and not self._writing):
                    self._loading = True
                    try:
                        on_done(result)
                    finally:
                        self._loading = False
                    if preserve:
                        self._baseline = baseline
                        self._changed()
                    elif not live:
                        self._capture()
            return False
        def completed(fut):
            try:
                result = fut.result()
            except Exception as exc:
                GLib.idle_add(settle, None, exc)
            else:
                GLib.idle_add(settle, result)
        try:
            self.service.submit(fn).add_done_callback(completed)
        except Exception as exc:
            settle(error=exc)

    def flash(self, text):
        self._status.set_text(text)
        root = self.get_root()
        if root is not None and hasattr(root, "_toasts") and self.get_mapped() and text != "Unsaved changes":
            root._toasts.add_toast(Adw.Toast(title=text, timeout=5))

    def number(self, entry, label, lo, hi, typ=int):
        try:
            value = typ(entry.get_text().strip())
            if not lo <= value <= hi:
                raise ValueError()
        except ValueError:
            entry.add_css_class("error")
            entry.set_tooltip_text("%s must be between %s and %s" % (label, lo, hi))
            entry.grab_focus()
            raise ValueError("%s must be between %s and %s" % (label, lo, hi))
        entry.remove_css_class("error")
        return value

    def confirm(self, heading, body, callback):
        if self._dialog is not None:
            return  # one question at a time; a second click must not queue a second action
        dialog = Adw.MessageDialog(transient_for=self.get_root(), modal=True,
                                   heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("confirm", "Continue")
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.set_default_response("cancel")
        def response(_dialog, answer):
            self._dialog = None
            if answer == "confirm":
                callback()
        dialog.connect("response", response)
        self._dialog = dialog
        dialog.present()

    def immediate(self, switch, setter, message, after=None):
        """A switch that writes on toggle (no Apply). *after(result)* runs on success."""
        switch._immediate = True
        def changed(_switch, state):
            if self._loading:
                return False
            old = switch.get_state()
            def done(result):
                switch.set_active(state)
                switch.set_state(state)
                if after:
                    after(result)
                self.flash(message(state) if callable(message) else message)
            def failed(exc):
                self._loading = True
                switch.set_active(old)
                switch.set_state(old)
                self._loading = False
                self.flash("Change failed: %s. Try again." % exc)
            self.run_async(lambda: setter(state), done, failed, write=True, preserve=True)
            return True
        switch.connect("state-set", changed)

    def require_device(self):
        """Bail out with a message when no HAT is present. Returns availability."""
        if self.service.available:
            return True
        self.flash("No PiJuice detected.")
        self.add_status()
        return False

    def _saved(self, rc):
        self.flash("Saved." if rc == 0 else
                   "Saved, but the background service did not reload. Use Retry service reload.")
        self._retry_notify.set_visible(rc != 0)

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
        if self._status_added:
            return
        self._status_added = True
        group = Adw.PreferencesGroup()
        group.add(self._status)
        self._retry_notify = Gtk.Button(label="Retry service reload", visible=False)
        self._retry_notify.connect("clicked", lambda _b: self.run_async(
            self.service.retry_notify, self._saved, write=True))
        group.add(self._retry_notify)
        self.add(group)

    def set_actions(self, group, apply_cb=None, refresh_cb=None, apply_label="Apply"):
        box = Gtk.Box(orientation=_H, spacing=6, valign=_CENTER)
        if refresh_cb is not None:
            btn = Gtk.Button(label="Refresh")
            btn.add_css_class("flat")
            btn.connect("clicked", lambda _b: refresh_cb() if not self.dirty else
                        self.flash("Apply or discard your changes before refreshing."))
            box.append(btn)
        if apply_cb is not None:
            actions = Gtk.Box(orientation=_H, spacing=6, visible=False)
            discard = Gtk.Button(label="Discard")
            discard.connect("clicked", self._discard)
            actions.append(discard)
            btn = Gtk.Button(label=apply_label)
            btn.add_css_class("suggested-action")
            def apply(_btn):
                if self._pending or self._writing:
                    return
                self._applying = True
                try:
                    apply_cb(_btn)
                except Exception as exc:  # any failure keeps the draft and tells the user
                    self.flash(str(exc))
                finally:
                    self._applying = False
            btn.connect("clicked", apply)
            actions.append(btn)
            self._actions = actions
            box.append(actions)
        group.set_header_suffix(box)

    readable = staticmethod(readable)

    def combo_row(self, group, title, strings, subtitle=None, labels=None):
        row = Adw.ComboRow(title=title, model=Gtk.StringList.new(labels or [self.readable(x) for x in strings]))
        if subtitle:
            row.set_subtitle(subtitle)
        group.add(row)
        return row

    def function_row(self, group, title, functions, subtitle=None):
        """Combo over button/event functions with readable labels; the subtitle
        explains the selected function."""
        names = user_function_names(self.service.config)
        row = self.combo_row(group, title, functions, subtitle,
                             labels=[function_label(f, names) for f in functions])
        def describe(*_args):
            fn = self.combo_get(row, functions, 'NO_FUNC')
            row.set_subtitle((subtitle + ' · ' if subtitle else '') + function_description(fn, self.service.config))
        row.connect('notify::selected', describe)
        describe()
        return row

    def switch_row(self, group, title, subtitle=None, active=False):
        row = Adw.ActionRow(title=title)
        if subtitle:
            row.set_subtitle(subtitle)
        switch = Gtk.Switch(active=active, valign=_CENTER)
        switch.update_property([Gtk.AccessibleProperty.LABEL], [title])
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
        spin.update_property([Gtk.AccessibleProperty.LABEL], [title])
        row = Adw.ActionRow(title=title)
        row.add_suffix(spin)
        row.set_activatable_widget(spin)
        group.add(row)
        return row, spin

    def value_row(self, group, title):
        """Read-only ActionRow whose suffix Label is returned for live updates."""
        row = Adw.ActionRow(title=title)
        label = Gtk.Label(label="…", xalign=1, selectable=True, wrap=True,
                          width_chars=20, max_width_chars=32, hexpand=True)
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
        summary = self.add_group("Battery")
        self._charge = Gtk.Label(label="Connecting…", xalign=0)
        self._charge.add_css_class("title-1")
        summary.add(self._charge)
        self._level = Gtk.LevelBar(min_value=0, max_value=100)
        self._level.update_property([Gtk.AccessibleProperty.LABEL], ["Battery charge"])
        summary.add(self._level)
        group = self.add_group("Power and health")
        self._switch_initialized = False
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
        self._switch._immediate = True  # applied by its own Set button, never a draft
        set_btn = Gtk.Button(label="Set")
        set_btn.add_css_class("suggested-action")
        set_btn.connect("clicked", self._on_set_switch)
        switch_group.set_header_suffix(set_btn)
        self.add_status()

        if not self.service.available:
            self.flash("No PiJuice detected.")
        else:
            self.refresh()
            self.poll(2, self._tick)

    def _tick(self):
        self.refresh()

    def refresh(self):
        self.run_async(self._read, self._apply, live=True)

    def _read(self):
        """Best-effort read of all status fields (runs on the worker)."""
        out = {}
        try:
            status = self.service.get_status()
        except PiJuiceError:
            status = {}

        try:
            out["level"] = self.service.get_charge_level()
            batt = "%i%%" % out["level"]
            try:
                mv = float(self.service.get_battery_voltage())
                batt += ", %.3fV" % (mv / 1000)
                temp = self.service.get_battery_temperature()
                if temp != -999:
                    batt += ", %d°C" % temp
            except PiJuiceError:
                pass
            if status.get("battery"):
                batt += ", %s" % self.readable(status["battery"])
            out["battery"] = batt
        except PiJuiceError as exc:
            out["battery"] = str(exc)

        out["usb"] = self.readable(str(status.get("powerInput", "Unavailable")))

        try:
            iov = float(self.service.get_io_voltage()) / 1000
            ioc = float(self.service.get_io_current()) / 1000
            out["gpio"] = "%.3fV, %.3fA, %s" % (
                iov,
                ioc,
                self.readable(status.get("powerInput5vIo", "Unavailable")),
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
            out["switch_value"] = sw
            out["sys_sw"] = ("%dmA" % sw) if sw else "Off"
        except PiJuiceError:
            out["sys_sw"] = "N/A"

        return out

    def _apply(self, data):
        level = data.get("level")
        self._charge.set_text("%s%% charged" % level if level is not None else "Battery unavailable")
        self._level.set_visible(level is not None)
        if level is not None:
            self._level.set_value(max(0, min(100, level)))
        for key, value in data.items():
            if key in self._labels:
                self._labels[key].set_text(value)
        if not self._switch_initialized and "switch_value" in data:
            value = data["switch_value"]
            if value in self._switch_values:
                self._switch.set_selected(self._switch_values.index(value))
            self._switch_initialized = True
            self._capture()

    def _on_set_switch(self, _btn):
        idx = self._switch.get_selected()
        value = self._switch_values[idx] if 0 <= idx < len(self._switch_values) else 0
        def apply():
            self.run_async(
                lambda: self.service.set_system_power_switch(value),
                lambda _r: (self.flash("System power switch updated."), self.refresh()),
                write=True,
            )
        if value == 0:
            self.confirm("Turn off system power?", "This can immediately interrupt power to connected equipment.", apply)
        else:
            apply()


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
                "Choose charge status or a custom colour. Preview temporarily displays "
                "the colour, then restores the saved LED settings."
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
        test = Gtk.Button(label="Preview colour")
        test.add_css_class("flat")
        test.connect("clicked", self._on_test, led)
        group.set_header_suffix(test)
        self._rows[led] = {"function": func, "r": r, "g": g, "b": b}
        def custom_colour(_spin):
            if not self._loading:
                self.combo_set(func, "USER_LED", LED_USER_SELECTABLE)
        for spin in (r, g, b):
            spin.connect("value-changed", custom_colour)

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
        configs = [(led, self._row_config(led)) for led in self._rows]
        def work():
            for led, cfg in configs:
                self.service.set_led_config(led, cfg)
        self.run_async(work, lambda _r: self.flash("LED settings applied."), write=True)

    def _on_test(self, _btn, led):
        row = self._rows[led]
        rgb = [row[ch].get_value_as_int() for ch in ("r", "g", "b")]
        # Preview is transient and restores the actual saved configuration.
        def work():
            saved = self.service.get_led_config(led)
            try:
                self.service.set_led_config(led, {
                    "function": "USER_LED", "parameter": dict(zip(("r", "g", "b"), rgb))
                })
                time.sleep(1)
            finally:
                self.service.set_led_config(led, saved)
        self.run_async(work, lambda _r: self.flash("Preview finished. Saved LED settings restored."),
                       write=True, preserve=True)


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
                row = self.function_row(group, readable(event), self._functions)
                param = Gtk.Entry(text="0", width_chars=6, valign=_CENTER)
                param.set_tooltip_text("Timing in milliseconds, steps of 100 (0–25500): single-press window, "
                                       "double-press gap or long-press hold time. Unused for press/release.")
                param.update_property([Gtk.AccessibleProperty.LABEL], [readable(event) + " timing in milliseconds"])
                param.set_sensitive(event not in ("PRESS", "RELEASE"))
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
        configs = []
        for button in self.service.buttons:
            config = {}
            for event in self.service.button_events:
                func, param = self._cells[(button, event)]
                pval = self.number(param, "%s %s delay (ms)" % (button, event), 0, 25500)
                if pval % 100:
                    raise ValueError("Button delays must use steps of 100 ms.")
                config[event] = {"function": self.combo_get(func, self._functions, "NO_FUNC"), "parameter": pval}
            configs.append((button, config))
        def work():
            for button, config in configs:
                self.service.set_button_config(button, config)
        self.run_async(work, lambda _r: self.flash("Button settings applied."), write=True)


# ── user scripts view (config JSON) ──────────────────────────────────────────
class UserScriptsView(_View):
    title = "User Scripts"
    slug = "userscripts"
    COUNT = 15

    def __init__(self, service):
        super().__init__(service)
        cfg = self.service.config.setdefault("user_functions", {})
        names = user_function_names(self.service.config)
        self._entries = {}
        self._names = {}

        head = self.add_group(
            "User Scripts",
            description=(
                "Each slot runs as the pijuice user when a button or system event is "
                "mapped to it. Use an absolute path (blank = unused). The name is how "
                "the slot appears in Buttons and System Events; the service is "
                "reloaded on Apply."
            ),
        )
        self.set_actions(head, self._on_apply)

        group = self.add_group()
        self._chooser = None  # keep a FileChooserNative alive while it is open
        for i in range(self.COUNT):
            key = "USER_FUNC%d" % (i + 1)
            row = Adw.EntryRow(title="Script %d (%s)" % (i + 1, key))
            row.set_text(str(cfg.get(key, "")))
            name = Gtk.Entry(placeholder_text="Name", text=names.get(key, ""), width_chars=14, valign=_CENTER)
            name.set_tooltip_text("Optional display name for this slot")
            name.update_property([Gtk.AccessibleProperty.LABEL], ["Name for script %d" % (i + 1)])
            row.add_prefix(name)
            self._names[key] = name
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
        cfg = copy.deepcopy(self.service.config.get("user_functions", {}))
        for key, entry in self._entries.items():
            value = entry.get_text().strip()
            if value and (not os.path.isabs(value) or not os.path.isfile(value)):
                entry.add_css_class("error")
                entry.grab_focus()
                raise ValueError("Choose an existing script using its full path.")
            entry.remove_css_class("error")
            cfg[key] = value
        names = {key: entry.get_text().strip() for key, entry in self._names.items() if entry.get_text().strip()}
        self.run_async(lambda: self.service.save_sections(user_functions=cfg, user_function_names=names), self._saved)


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
            row = self.function_row(group, text, self._functions)
            self.combo_set(row, ev["function"], self._functions)
            switch = Gtk.Switch(active=bool(ev["enabled"]), valign=_CENTER)
            switch.update_property([Gtk.AccessibleProperty.LABEL], [text + " enabled"])
            row.add_prefix(switch)
            self._rows[key] = (switch, row)
        self.add_status()

    def _on_apply(self, _btn):
        cfg = copy.deepcopy(self.service.config.get("system_events", {}))
        for key, (switch, row) in self._rows.items():
            fn = self.combo_get(row, self._functions, "NO_FUNC")
            cfg[key] = {"enabled": switch.get_active(), "function": fn}
        self.run_async(lambda: self.service.save_section("system_events", cfg), self._saved)


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
            switch.update_property([Gtk.AccessibleProperty.LABEL], [label + " enabled"])
            row.add_prefix(switch)
            entry = Gtk.Entry(
                text=str(secd.get(field, "")), width_chars=8, valign=_CENTER
            )
            row.add_suffix(entry)
            group.add(row)
            self._rows[sec] = (switch, entry, field, typ, lo, hi)
            switch.bind_property("active", entry, "sensitive", 2)
            entry.update_property([Gtk.AccessibleProperty.LABEL], [label + ": " + vlabel])
        self.add_status()

    def _on_apply(self, _btn):
        st = copy.deepcopy(self.service.config.get("system_task", {}))
        st["enabled"] = self._enabled.get_active()
        for sec, (switch, entry, field, typ, lo, hi) in self._rows.items():
            secd = st.setdefault(sec, {})
            secd["enabled"] = switch.get_active()
            if not entry.get_text().strip() and not switch.get_active():
                secd.pop(field, None)
                continue
            secd[field] = self.number(entry, sec.replace("_", " "), lo, hi, typ)
        self.run_async(lambda: self.service.save_section("system_task", st), self._saved)


# ── battery view ─────────────────────────────────────────────────────────────
class BatteryView(_View):
    title = "Battery"
    slug = "battery"

    def __init__(self, service):
        super().__init__(service)
        self.add_group("Battery")  # placeholder header for the no-device message
        if not self.require_device():
            return

        self._policy = service.get_charge_policy()
        care = self.add_group("Battery care", "The charge limit runs while the Pi and PiJuice service are running. It pauses at 80% and resumes at 75%; it does not discharge the battery to 80%.")
        _, self._limit = self.switch_row(care, "80% charge limit", "Changes immediately; leave off for maximum backup runtime")
        self._limit.set_active(self._policy["enabled"])
        self.immediate(self._limit, self._set_limit,
                       lambda state: "80% charge limit " + ("enabled. The service checks every 5 seconds." if state else "disabled."),
                       after=self._limit_saved)
        health = self.add_group("Battery condition", "Configured capacity describes the selected profile, not measured remaining capacity.")
        self._condition = self.value_row(health, "Condition")
        self._temperature = self.value_row(health, "Temperature")
        self._capacity = self.value_row(health, "Configured capacity")
        self._charge_specs = self.value_row(health, "Profile charging limits")
        self._health_note = Gtk.Label(label="Battery history: loading…", xalign=0, wrap=True)
        health.add(self._health_note)
        reset = Gtk.Button(label="New battery / reset tracking…")
        reset.connect("clicked", lambda _b: self.confirm("Start new battery history?",
            "Cycle tracking restarts at zero and capacity health starts learning again. Use this after replacing the battery. Previous summaries are archived.",
            lambda: self.run_async(self.service.reset_battery_history,
                lambda rc: self.flash("Tracking reset requested." if rc == 0 else "Saved; reload the background service to start new history."), write=True)))
        health.add(reset)
        self._profiles = service.get_battery_profiles() + ["DEFAULT", "CUSTOM"]
        head = self.add_group()
        self.set_actions(head, self._on_apply, self.refresh)
        self._profile = self.combo_row(head, "Profile", self._profiles,
            "Match the exact battery model. Existing custom profiles can be edited in the CLI.")
        self._profile.set_model(Gtk.StringList.new([profile_label(p) for p in self._profiles]))
        self._pstatus = self.value_row(head, "Status")
        self._temp = self.combo_row(
            head, "Temperature sense", service.battery_temp_sense_options
        )
        self._rsoc = None
        if service.fw_int >= 0x13:
            self._rsoc = self.combo_row(
                head, "RSoC estimation", service.rsoc_estimation_options
            )
        _, self._charging = self.switch_row(head, "Charging enabled", subtitle="Changes immediately")
        self.immediate(self._charging, service.set_charging_config, "Charging setting updated.")
        self._charging.set_sensitive(not self._policy["enabled"])
        self._charging.set_tooltip_text("Managed automatically while the charge limit is enabled")
        self.add_status()
        self.refresh()
        self.poll(5, self._refresh_health)

    def _set_limit(self, state):
        return self.service.set_charge_policy({"enabled": state, "limit": 80, "resume": 75})

    def _limit_saved(self, rc):
        self._policy = self.service.get_charge_policy()
        self._charging.set_sensitive(not self._policy["enabled"])
        self._saved(rc)

    def _refresh_health(self):
        if self.service.available:  # a lost HAT must not toast every 5 s
            self.run_async(self.service.get_battery_report, self._show_health, live=True,
                           on_error=lambda exc: self._condition.set_text("Unavailable: %s" % exc))

    def _show_health(self, report):
        if isinstance(report.get("policy"), dict):
            self._policy = report["policy"]
            self._limit.set_active(self._policy["enabled"])
            self._charging.set_sensitive(not self._policy["enabled"])
        if isinstance(report.get("charging"), dict):
            self._charging.set_active(report["charging"].get("charging_enabled", False))
        self._health_note.set_text(report.get("health", "Battery history: waiting for readings."))
        self._condition.set_text(report.get("condition", ""))
        temperature = report.get("temperature")
        self._temperature.set_text("Unavailable" if temperature is None else "%s °C" % temperature)
        capacity = report.get("design_capacity")
        self._capacity.set_text("Unknown" if capacity is None else "%s mAh" % capacity)
        profile = report.get("profile")
        if isinstance(profile, dict):
            self._charge_specs.set_text("%s mA · %s mV" % (profile.get("chargeCurrent", "?"), profile.get("regulationVoltage", "?")))

    def refresh(self):
        self.run_async(self._read, self._apply)

    def _read(self):
        out = {}
        for key, fn in (
            ("status", self.service.get_battery_profile_status),
            ("temp", self.service.get_battery_temp_sense),
            ("charging", self.service.get_charging_config),
            ("report", self.service.get_battery_report),
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
        if isinstance(data.get("report"), dict):
            self._show_health(data["report"])
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
            selected = "CUSTOM" if st.get("origin") == "CUSTOM" else "DEFAULT" if st.get("source") in ("DIP_SWITCH", "RESISTOR") else st.get("profile")
            self.combo_set(self._profile, selected, self._profiles)
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

        def work():
            if profile:
                self.service.set_battery_profile(profile)
            self.service.set_battery_temp_sense(temp)
            if rsoc is not None:
                self.service.set_rsoc_estimation(rsoc)
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
        if not self.require_device():
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
        for widget, prop in self._controls(group):
            if widget not in self._tracked:
                self._baseline[(widget, prop)] = widget.get_property(prop)
                widget.connect("notify::" + prop, self._changed)
                self._tracked.add(widget)

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
        configs = []
        for pin in (1, 2):
            p = self._pins[pin]
            mode = p["modes"][p["mode"].get_selected()]
            cfg = {"mode": mode, "pull": pulls[p["pull"].get_selected()]}
            for pcfg, widget in p["params"]:
                if pcfg["type"] == "enum":
                    cfg[pcfg["name"]] = pcfg["options"][widget.get_selected()]
                else:
                    cfg[pcfg["name"]] = self.number(widget, pcfg["name"], pcfg["min"], pcfg["max"],
                                                   float if pcfg["type"] == "float" else int)
            configs.append((pin, cfg))
        def work():
            for pin, cfg in configs:
                self.service.set_io_config(pin, cfg)
        self.run_async(work, lambda _r: self.flash("IO settings applied."), write=True)


# ── wakeup alarm view ────────────────────────────────────────────────────────
class WakeupView(_View):
    title = "Wakeup Alarm"
    slug = "wakeup"
    _DAY_TYPES = ["Day of month", "Weekday"]
    _MIN_TYPES = ["Minute", "Minutes period"]

    def __init__(self, service):
        super().__init__(service)
        head = self.add_group("Wakeup Alarm", "Schedules use UTC and do not shift with daylight saving time.")
        if not self.require_device():
            return

        self.set_actions(head, self._on_set_alarm, self.refresh, "Apply schedule")
        clock = self.add_group("RTC clock")
        self._time = self.value_row(clock, "RTC time (UTC)")
        self._local_time = self.value_row(clock, "Local time")
        self._time.remove_css_class("dim-label")
        self._time.add_css_class("monospace")
        set_time = Gtk.Button(label="Set from Pi")
        set_time.add_css_class("flat")
        set_time.connect("clicked", self._on_set_time)
        clock.set_header_suffix(set_time)

        alarm = self.add_group("Alarm")
        self._daytype = self.combo_row(alarm, "Day type", self._DAY_TYPES)
        self._day = Adw.EntryRow(title="Day of month (1–31)")
        self._day.set_tooltip_text("For weekdays, use 1=Sunday to 7=Saturday; several days may be separated by semicolons.")
        alarm.add(self._day)
        _, self._every_day = self.switch_row(alarm, "Every day")
        self._hour = Adw.EntryRow(title="Hour (0–23)")
        self._hour.set_tooltip_text("One hour or several separated by semicolons, e.g. 8;12;18. AM/PM also accepted.")
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
        self.immediate(self._enabled, service.set_wakeup_enabled, "Wakeup setting updated.")
        self._enabled.set_tooltip_text("Changes immediately; apply schedule edits separately")
        self._summary = Gtk.Label(xalign=0, wrap=True)
        alarm.add(self._summary)
        for widget, signal in ((self._daytype, "notify::selected"), (self._mintype, "notify::selected"),
                               (self._every_day, "notify::active"), (self._every_hour, "notify::active"),
                               (self._day, "changed"), (self._hour, "changed"),
                               (self._minute, "changed"), (self._second, "changed")):
            widget.connect(signal, self._update_summary)
        self._update_summary()
        self.add_status()

        self.refresh()
        self._tick()
        self.poll(1, self._tick)

    def refresh(self):
        self.run_async(self._read_alarm, self._apply_alarm)

    def _update_summary(self, *_args):
        weekday = self._daytype.get_selected() == 1
        period = self._mintype.get_selected() == 1
        self._day.set_title("Weekday (1=Sunday … 7=Saturday)" if weekday else "Day of month (1–31)")
        self._minute.set_title("Repeat interval (1–60 minutes)" if period else "Minute (0–59)")
        self._day.set_sensitive(not self._every_day.get_active())
        self._hour.set_sensitive(not self._every_hour.get_active())
        day = "Every day" if self._every_day.get_active() else (
            ("Weekday " if weekday else "Day ") + (self._day.get_text() or "…"))
        hour = "every hour" if self._every_hour.get_active() else "hour " + (self._hour.get_text() or "…")
        minute = ("every %s minutes" if period else "minute %s") % (self._minute.get_text() or "…")
        self._summary.set_text("%s, %s, %s, second %s (UTC). Apply to save this schedule." %
                               (day, hour, minute, self._second.get_text() or "…"))

    def _tick(self):
        self.run_async(
            self.service.get_rtc_time, self._show_time,
            on_error=lambda _e: (self._time.set_text("Unavailable"), self._local_time.set_text("Unavailable")), live=True
        )

    def _show_time(self, t):
        try:
            self._time.set_text(
                "%04d-%02d-%02d %02d:%02d:%02d"
                % (t["year"], t["month"], t["day"], t["hour"], t["minute"], t["second"])
            )
            utc = datetime.datetime(t["year"], t["month"], t["day"], t["hour"], t["minute"],
                                    t["second"], tzinfo=datetime.timezone.utc)
            self._local_time.set_text(utc.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"))
        except (KeyError, TypeError, ValueError):
            self._time.set_text("Unavailable")
            self._local_time.set_text("Unavailable")

    def _read_alarm(self):
        return {
            "control": self.service.get_alarm_control(),
            "alarm": self.service.get_alarm(),
        }

    def _apply_alarm(self, data):
        ctrl = data.get("control") or {}
        self._enabled.set_active(bool(ctrl.get("alarm_wakeup_enabled")))
        f = alarm_fields(data.get("alarm"))  # every field reset, so no stale draft text survives
        self._daytype.set_selected(f["day_type"])
        self._every_day.set_active(f["every_day"])
        self._day.set_text(f["day"])
        self._every_hour.set_active(f["every_hour"])
        self._hour.set_text(f["hour"])
        self._mintype.set_selected(f["minute_type"])
        self._minute.set_text(f["minute"])
        self._second.set_text(f["second"] or "0")

    def _on_set_time(self, _btn):
        self.run_async(
            lambda: self.service.set_rtc_time(rtc_fields_now()),
            lambda _r: (self.flash("RTC time set."), self._tick()),
            write=True, preserve=True,
        )

    def _schedule_values(self, entry, label, lo, hi, hours=False):
        try:
            value = schedule_values(entry.get_text(), lo, hi, hours)
        except ValueError:
            entry.add_css_class("error")
            entry.grab_focus()
            raise ValueError("%s must be between %s and %s; separate multiple values with semicolons." % (label, lo, hi))
        entry.remove_css_class("error")
        return value

    def _on_set_alarm(self, _btn):
        alarm = {"second": self.number(self._second, "Second", 0, 59)}
        period = self._mintype.get_selected() == 1
        alarm["minute_period" if period else "minute"] = self.number(
            self._minute, "Minute interval" if period else "Minute", 1 if period else 0, 60 if period else 59)
        alarm["hour"] = "EVERY_HOUR" if self._every_hour.get_active() else self._schedule_values(self._hour, "Hour", 0, 23, hours=True)
        weekday = self._daytype.get_selected() == 1
        alarm["weekday" if weekday else "day"] = ("EVERY_DAY" if self._every_day.get_active()
            else self._schedule_values(self._day, "Weekday", 1, 7) if weekday
            else self.number(self._day, "Day", 1, 31))
        self.run_async(lambda: self.service.set_alarm(alarm),
                       lambda _r: self.flash("Schedule saved. Wakeup enablement is unchanged."), write=True)


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
        self._spinner = Gtk.Spinner(halign=Gtk.Align.START)
        status_group.add(self._spinner)
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
            ver = pack_version("%s.%s" % m.group(1, 2))
            if ver >= best:
                best = ver
                self._new_ver = "%d.%d" % (int(m.group(1)), int(m.group(2)))
                self._bin_file = os.path.join(self.FW_DIR, name)
        self._path.set_text(os.path.basename(self._bin_file) if self._bin_file else "No firmware file found")
        self._path.set_tooltip_text(self._bin_file)

    def refresh(self):
        if not self.service.available:
            self._ver.set_text("no device")
            return
        self.run_async(lambda: self.service.firmware_version, self._apply_ver)

    def _apply_ver(self, fw):
        self._update_btn.set_sensitive(False)
        cur = (fw or {}).get("version") if isinstance(fw, dict) else None
        self._ver.set_text(cur or "unknown")
        cur_int, new_int = pack_version(cur), pack_version(self._new_ver)
        if cur_int and new_int > cur_int:
            self._fw_status.set_text("New firmware V%s available." % self._new_ver)
            self._update_btn.set_sensitive(True)
        elif cur_int and new_int:
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
                    "Charge level too low to update (connect power or charge to at least 20%)."
                )

        self.run_async(check, done, live=True)

    def _do_flash(self, _btn):
        if not self._bin_file or self._writing:
            return
        self._confirm.set_visible(False)
        self._update_btn.set_sensitive(False)
        self._spinner.start()
        def work():
            # Check again: power may have changed since confirmation opened.
            status = self.service.get_status()
            if (status.get("powerInput") != "PRESENT" and status.get("powerInput5vIo") != "PRESENT"
                    and self.service.get_charge_level() < 20):
                raise PiJuiceError("Connect external power or charge to at least 20%.")
            self.service.flash_firmware(self._bin_file)  # raises with the reason on failure
            for _ in range(60):  # ponytail: bounded 30 s; the HAT reboots in a few
                time.sleep(0.5)
                if self.service.connect():
                    return True
            return False
        def failed(exc):
            self._spinner.stop()
            self._update_btn.set_sensitive(True)
            self.flash("%s. Check power and connection, then retry." % exc)
        def done(connected):
            self._spinner.stop()
            self.flash("Firmware updated." if connected else "Firmware written. Waiting for the device to reconnect…")
            self.refresh()
        self.run_async(work, done, failed, write=True)
        self.flash("Updating firmware… Keep power connected until this finishes.")


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
        self.set_default_size(820, 600)
        _install_css(self.get_display())

        header = Adw.HeaderBar()
        self._connection = Gtk.Label(label="PiJuice Settings")
        header.set_title_widget(self._connection)
        retry = Gtk.Button(label="Retry connection")
        retry.connect("clicked", lambda _b: self._check_connection())
        self._retry = retry
        header.pack_end(retry)
        self._connection_pending = False
        self._closed = False
        self._online = service.available
        self.connect("close-request", self._on_close)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True)
        sidebar = Gtk.StackSidebar(stack=self.stack)
        sidebar.set_size_request(170, -1)
        self._leaflet = Adw.Leaflet(can_navigate_back=True)
        self._leaflet.set_hexpand(True)
        self._leaflet.append(sidebar)
        self._leaflet.append(self.stack)
        self.stack.set_size_request(360, -1)
        back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Settings sections")
        header.pack_start(back)
        back.connect("clicked", lambda _b: self._leaflet.set_visible_child(sidebar))
        self._leaflet.bind_property("folded", back, "visible", 2)
        self.stack.connect("notify::visible-child", lambda *_a: self._leaflet.set_visible_child(self.stack))
        for view_cls in VIEW_CLASSES:
            view = view_cls(service)
            self.stack.add_titled(view, view.slug, view.title)

        content = Gtk.Box(orientation=_H)
        content.set_vexpand(True)
        content.append(self._leaflet)

        outer = Gtk.Box(orientation=_V)
        outer.append(header)
        outer.append(content)
        self._toasts = Adw.ToastOverlay()
        self._toasts.set_child(outer)
        self.set_content(self._toasts)

        self._show_connection(service.available)
        self._connection_timer = GLib.timeout_add_seconds(5, self._check_connection)
        GLib.idle_add(lambda: (self._check_connection(), False)[1])

    def _views(self):
        return [page.get_child() for page in self.stack.get_pages()]

    def _show_connection(self, online):
        self._connection.set_text("PiJuice Settings" if online else "PiJuice not connected — retrying…")
        self._retry.set_visible(not online)

    def _check_connection(self):
        if self._closed:
            return False
        if self._connection_pending or any(v._pending or v._writing for v in self._views()):
            return True
        self._connection_pending = True
        def check():
            if self.service.available:
                try:
                    self.service.get_status()
                    return True
                except Exception:
                    pass
            return self.service.connect()
        def completed(fut):
            try:
                online = fut.result()
            except Exception:
                online = False
            GLib.idle_add(self._connected, online)
        self.service.submit(check).add_done_callback(completed)
        return True

    def _connected(self, online):
        self._connection_pending = False
        if self._closed:
            return False
        self._show_connection(online)
        selected = self.stack.get_visible_child_name()
        views = self._views()
        rebuild = online and any(not v._built_available and v.slug not in
                                  ("userscripts", "sysevents", "systask") for v in views)
        replacements = []
        for view in views:
            hardware = view.slug not in ("userscripts", "sysevents", "systask")
            if online and not view._built_available and hardware:
                view.dispose_view()
                replacements.append(type(view)(self.service))
            else:
                replacements.append(view)
                if hardware:
                    view.set_sensitive(online)
                if online and not self._online and not view.dirty and hasattr(view, "refresh"):
                    view.refresh()
        if rebuild:
            for view in views:
                self.stack.remove(view)
            for view in replacements:
                self.stack.add_titled(view, view.slug, view.title)
        self.stack.set_visible_child_name(selected)
        self._online = online
        return False

    def _on_close(self, *_args):
        views = self._views()
        if any(v._writing for v in views):
            views[0].flash("Wait for the current operation to finish before closing.")
            return True
        if any(v.dirty for v in views):
            views[0].confirm("Discard unsaved changes?", "Your saved settings will be kept.", self._close_now)
            return True
        self._cleanup()
        return False

    def _cleanup(self):
        self._closed = True
        GLib.source_remove(self._connection_timer)
        for view in self._views():
            view.dispose_view()

    def _close_now(self):
        self._cleanup()
        self.destroy()


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
            self.service = PiJuiceService(connect=False)
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
    win._cleanup()
    win.destroy()
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
