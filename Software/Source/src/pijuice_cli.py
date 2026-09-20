# This python script to be executed as user pijuice by the setuid program pijuice_cli
# Python 3 only
#
# -*- coding: utf-8 -*-
# pylint: disable=import-error
import copy
import math
import datetime
import os
import re
import subprocess
import time
import fcntl
import json
import sys

from pijuice_battery import battery_report, charge_policy, profile_label

import urwid
from pijuice import (
    PiJuice,
    PiJuiceConfig,
    pijuice_hard_functions,
    pijuice_sys_functions,
    pijuice_user_functions,
)

# Shared, UI-agnostic helpers (paths, config I/O, service notify) live in
# pijuice_service so the CLI, GUI and tray agree on one source of truth.
from pijuice_service import (
    ADDRESS_DEFAULT,
    BUS_DEFAULT,
    CONFIG_PATH_DEFAULT,
    PID_FILE_DEFAULT,
    load_config as _service_load_config,
    notify_service as _service_notify_service,
    save_config as _service_save_config,
)

class ActionButton(urwid.Button):
    """Keep form errors recoverable and protect drafts from explicit refreshes."""
    def __init__(self, label, on_press=None, user_data=None):
        self.action = on_press
        self.action_data = user_data
        def dispatch(button, *args):
            if str(label).lower().startswith("refresh") and _dirty:
                _flash("Unsaved changes kept. Apply (F5) or discard (F6) before refreshing.", "warning")
                return
            if str(label).lower().startswith(("apply", "set alarm")) and _errors:
                _flash(next(iter(_errors.values())), "error")
                return
            previous = (main.original_widget, _current_back, _location) if main else None
            try:
                return on_press(button, *args)
            except urwid.ExitMainLoop:
                raise
            except Exception as exc:
                if previous:
                    _restore_view(*previous)
                _flash("Could not complete the operation: %s. Your edits are kept." % exc, "error")
        super().__init__(label, on_press=dispatch if on_press else None, user_data=user_data)


# Buttons render with [ label ] instead of urwid's default < label >.
urwid.Button.button_left = urwid.Text("[")
urwid.Button.button_right = urwid.Text("]")

BUS = BUS_DEFAULT
ADDRESS = ADDRESS_DEFAULT
PID_FILE = PID_FILE_DEFAULT
LOCK_FILE = "/run/pijuice/pijuice_gui.lock"  # CLI-only single-instance lock

pijuice = None

pijuiceConfigData = {}
PiJuiceConfigDataPath = CONFIG_PATH_DEFAULT

# NumEdit/FloatEdit are vendored from urwid because urwid still ships no such
# widget (2.1.2 has IntEdit only) -- do not delete this block.
#### Following taken from urwid 2.0.2 to get a FloatEdit widget ###
#
# Urwid basic widget classes
#    Copyright (C) 2004-2012  Ian Ward
#
#    This library is free software; you can redistribute it and/or
#    modify it under the terms of the GNU Lesser General Public
#    License as published by the Free Software Foundation; either
#    version 2.1 of the License, or (at your option) any later version.
#
#    This library is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#    Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public
#    License along with this library; if not, write to the Free Software
#    Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
#
# Urwid web site: http://excess.org/urwid/


from urwid import Edit
from decimal import Decimal


def _InitPiJuiceInterface():
    try:
        addr = ADDRESS
        bus = BUS
        global pijuiceConfigData
        if pijuiceConfigData == None:
            pijuiceConfigData = loadPiJuiceConfig()
        if "board" in pijuiceConfigData and "general" in pijuiceConfigData["board"]:
            if "i2c_addr" in pijuiceConfigData["board"]["general"]:
                addr = int(pijuiceConfigData["board"]["general"]["i2c_addr"], 16)
            if "i2c_bus" in pijuiceConfigData["board"]["general"]:
                bus = pijuiceConfigData["board"]["general"]["i2c_bus"]
        global pijuice
        pijuice = PiJuice(bus, addr)
        global current_fw_version
        current_fw_version = get_current_fw_version()
    except:
        pijuice = None


class NumEdit(Edit):
    """NumEdit - edit numerical types

    based on the characters in 'allowed' different numerical types
    can be edited:
      + regular int: 0123456789
      + regular float: 0123456789.
      + regular oct: 01234567
      + regular hex: 0123456789abcdef
    """

    ALLOWED = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def __init__(self, allowed, caption, default, trimLeadingZeros=True):
        super(NumEdit, self).__init__(caption, default)
        self._allowed = allowed
        self.trimLeadingZeros = trimLeadingZeros

    def valid_char(self, ch):
        """
        Return true for allowed characters.
        """
        return len(ch) == 1 and ch.upper() in self._allowed

    def keypress(self, size, key):
        """
        Handle editing keystrokes.  Remove leading zeros.
        """
        (maxcol,) = size
        unhandled = Edit.keypress(self, (maxcol,), key)

        if not unhandled:
            if self.trimLeadingZeros:
                # trim leading zeros
                while self.edit_pos > 0 and self.edit_text[:1] == "0":
                    self.set_edit_pos(self.edit_pos - 1)
                    self.set_edit_text(self.edit_text[1:])

        return unhandled


class FloatEdit(NumEdit):
    """Edit widget for float values."""

    def __init__(
        self, caption="", default=None, preserveSignificance=False, decimalSeparator="."
    ):
        """
        caption -- caption markup
        default -- default edit value
        preserveSignificance -- return value has the same signif. as default
        decimalSeparator -- use '.' as separator by default, optionally a ','
        """
        self.significance = None
        self._decimalSeparator = decimalSeparator
        if decimalSeparator not in [".", ","]:
            raise ValueError("invalid decimalSeparator: {}".format(decimalSeparator))

        val = ""
        if default is not None and default != "":
            if not isinstance(default, (int, str, float, Decimal)):
                raise ValueError(
                    "default: Only 'str', 'int', 'float' or Decimal input allowed"
                )

            if isinstance(default, str) and len(default) and preserveSignificance:
                default = Decimal(default)

            if preserveSignificance:
                self.significance = abs(default.as_tuple()[2])

            val = str(default)

        super(FloatEdit, self).__init__(
            self.ALLOWED[0:10] + decimalSeparator, caption, val
        )


####################################################################################


def version_to_str(number):
    # Convert int version to str {major}.{minor}
    return "{}.{}".format(number >> 4, number & 15)


def get_current_fw_version():
    # Returns current version as int (first 4 bits - minor, second 4 bits - major)
    status = pijuice.config.GetFirmwareVersion()

    if status["error"] == "NO_ERROR":
        major, minor = status["data"]["version"].split(".")
    else:
        major = minor = 0
    current_version = (int(major) << 4) + int(minor)
    return current_version


def validate_value(text, type, min, max, old):
    """Validate a complete number; never silently clamp or substitute a value."""
    try:
        value = int(text) if type == "int" else float(text)
        if not math.isfinite(value) or (min is not None and value < min) or (max is not None and value > max):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("Enter a %s between %s and %s." % ("whole number" if type == "int" else "number", min, max))
    return str(value)


def _validate_edit(widget, text, kind, lo, hi, key):
    try:
        validate_value(text, kind, lo, hi, None)
    except ValueError:
        _errors[key] = "%s: enter %s–%s%s." % (key, lo, hi, " (whole numbers)" if kind == "int" else "")
        _flash(_errors[key], "error")
    else:
        _errors.pop(key, None)
        _flash("Unsaved changes", "warning")
    return text  # Keep incomplete input editable until Apply.


def confirmation_dialog(text, next, nextno="", single_option=True):
    global _in_dialog, _dialog_cancel
    if single_option and any(word in text.lower() for word in ("successfully", "settings saved", "settings have been refreshed", "settings have been applied", "updated settings for all pins")):
        _saved_draft()
        result = next(None)
        _flash(text.replace("\n", " "), "ok")
        return result

    def done(cb, *bound):
        # Wrap a dialog callback so it clears the dialog flag before running.
        def inner(*a):
            global _in_dialog
            _in_dialog = False
            return cb(*(bound if bound else a))

        return inner

    elements = [
        urwid.Text(text, align="center"),
        urwid.Divider(),
    ]
    if single_option:
        ok = done(next)
        elements.append(
            urwid.Padding(
                attrmap(ActionButton("OK", on_press=ok)), align="center", width=6
            )
        )
        _dialog_cancel = lambda: ok(None)
    else:
        yes_btn = ActionButton("Yes")
        no_btn = ActionButton("No")
        yes = done(next, None, True)
        no = done(nextno, None, False)
        urwid.connect_signal(yes_btn, "click", lambda b: yes())
        urwid.connect_signal(no_btn, "click", lambda b: no())
        row = urwid.Columns(
            [(7, attrmap(yes_btn)), (6, attrmap(no_btn))], dividechars=2, focus_column=1
        )
        elements.append(
            urwid.Padding(row, align="center", width=15)
        )  # ←/→ or h/l to pick
        _dialog_cancel = lambda: no()

    _in_dialog = True
    main.original_widget = urwid.Filler(urwid.Pile(elements))


class StatusTab(object):
    def __init__(self, *args):
        self.main()

    def get_status(self):
        if pijuice is None:
            _InitPiJuiceInterface()
        status = pijuice.status.GetStatus() if pijuice else {"error": "NO_CONNECTION"}
        if status.get("error") != "NO_ERROR":
            _InitPiJuiceInterface()
            return [("error", "Not connected — retrying automatically\n"),
                    "Check the HAT and I2C connection. Settings remain available.\n"]
        data = status.get("data", {})
        charge = pijuice.status.GetChargeLevel().get("data")
        rows = [("title", "BATTERY & POWER\n\n")]
        if charge is None:
            rows.append(("warning", "Charge unavailable\n"))
        else:
            level = max(0, min(100, int(charge)))
            colour = "error" if level <= 15 else "warning" if level <= 30 else "ok"
            filled = round(level / 5)
            rows += [(colour, "%3d%%  [%s%s]\n" % (level, "#" * filled, "-" * (20-filled)))]
        battery = data.get("battery", "UNKNOWN")
        rows += [("warning" if battery == "NOT_PRESENT" else "value", readable(battery) + "\n\n")]
        for label, method, scale, unit in (
                ("Voltage", pijuice.status.GetBatteryVoltage, 1000, "V"),
                ("Temperature", pijuice.status.GetBatteryTemperature, 1, "°C")):
            result = method().get("data")
            value = "Unavailable" if result is None or result == -999 else "%g %s" % (result / scale, unit)
            rows += [("muted", label + ": "), value + "\n"]
        rows.append("\n")
        for label, key in (("USB input", "powerInput"), ("GPIO input", "powerInput5vIo")):
            value = data.get(key, "UNKNOWN")
            rows += [("muted", label + ": "), ("ok" if value == "PRESENT" else "value", readable(value) + "\n")]
        fault = pijuice.status.GetFaultStatus()
        problems = []
        if fault.get("error") != "NO_ERROR":
            problems.append("Unable to read faults")
        else:
            for key, value in fault.get("data", {}).items():
                if key in ("battery_profile_invalid", "charging_temperature_fault") and value and value not in ("NORMAL", "NO_ERROR"):
                    problems.append(readable(key) + (": " + readable(value) if isinstance(value, str) else ""))
        rows += [("muted", "Health: "), ("error" if problems else "ok", "; ".join(problems) if problems else "No faults"), "\n"]
        switch = pijuice.power.GetSystemPowerSwitch().get("data")
        rows += [("muted", "System switch: "), "Unavailable" if switch is None else "%s mA" % switch if switch else "Off"]
        return rows

    def update_status(self, obj, text):
        if main.original_widget is not self._widget:
            return
        text.set_text(self.get_status())
        self.alarm_handle = loop.set_alarm_in(2, self.update_status, text)

    def main(self, *args):
        text = urwid.Text(self.get_status())
        rows = [text, urwid.Divider(), attrmap(ActionButton("Change power switch", on_press=self.change_power_switch)),
                attrmap(ActionButton("Back", on_press=self._goto_main_menu))]
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(rows))
        self._widget = main.original_widget
        self.alarm_handle = loop.set_alarm_in(2, self.update_status, text)

    def change_power_switch(self, *args):
        loop.remove_alarm(self.alarm_handle)
        elements = [urwid.Text("Choose value for System Power switch"), urwid.Divider()]
        values = [0, 500, 2100]
        for value in values:
            text = str(value) + " mA" if value else "Off"
            elements.append(
                urwid.Padding(
                    attrmap(
                        ActionButton(
                            text, on_press=self.set_power_switch, user_data=value
                        )
                    ),
                    width=11,
                )
            )
        elements.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(ActionButton("Back", on_press=self.main)), width=8
                ),
            ]
        )
        main.original_widget = urwid.Filler(urwid.Pile(elements), valign="top")

    def set_power_switch(self, button, value):
        def apply(*_args):
            result = pijuice.power.SetSystemPowerSwitch(int(value))
            if result.get("error") != "NO_ERROR":
                _flash("Power switch unchanged: " + result.get("error", "Unknown error"), "error")
                return
            self.main()
            _flash("System power switch updated.", "ok")
        if value == 0:
            confirmation_dialog("Turn off system power? This can interrupt power to connected equipment.",
                                next=apply, nextno=self.main, single_option=False)
        else:
            apply()

    def _goto_main_menu(self, *args):
        loop.remove_alarm(self.alarm_handle)
        main_menu()


class FirmwareTab(object):
    FIRMWARE_UPDATE_ERRORS = [
        "NO_ERROR",
        "I2C_BUS_ACCESS_ERROR",
        "INPUT_FILE_OPEN_ERROR",
        "STARTING_BOOTLOADER_ERROR",
        "FIRST_PAGE_ERASE_ERROR",
        "EEPROM_ERASE_ERROR",
        "INPUT_FILE_READ_ERROR",
        "PAGE_WRITE_ERROR",
        "PAGE_READ_ERROR",
        "PAGE_VERIFY_ERROR",
        "CODE_EXECUTE_ERROR",
    ]

    def __init__(self, *args):
        self.firmware_path = None
        self.show_firmware()

    def check_for_fw_updates(self):
        # Check /usr/share/pijuice/data/firmware/ for new version of firmware.
        # Returns (version, path)
        import glob

        bin_dir = "/usr/share/pijuice/data/firmware/"
        latest_version = 0
        firmware_path = ""

        regex = re.compile(r"PiJuice-V(\d+)\.(\d+)_(\d+_\d+_\d+).elf.binary")
        for path in sorted(glob.glob(os.path.join(bin_dir, "PiJuice-V*.elf.binary"))):
            match = regex.match(os.path.basename(path))
            if match:
                major = int(match.group(1))
                minor = int(match.group(2))
                version = (major << 4) + minor
                if version >= latest_version:
                    latest_version = version
                    firmware_path = path

        return latest_version, firmware_path

    def get_fw_status(self):
        global current_fw_version
        current_version = current_fw_version
        latest_version, firmware_path = self.check_for_fw_updates()

        if current_version and latest_version:
            if latest_version > current_version:
                firmware_status = (
                    "New firmware (V"
                    + version_to_str(latest_version)
                    + ") is available"
                )
            else:
                firmware_status = "up to date"
        elif not current_version:
            firmware_status = "unknown"
        else:
            firmware_status = "Missing/wrong firmware file"
        return current_version, latest_version, firmware_status, firmware_path

    def update_firmware_start(self, *args):
        device_status = pijuice.status.GetStatus()

        if device_status["error"] == "NO_ERROR":
            if (
                device_status["data"]["powerInput"] != "PRESENT"
                and device_status["data"]["powerInput5vIo"] != "PRESENT"
                and pijuice.status.GetChargeLevel().get("data", 0) < 20
            ):
                # Charge level is too low
                return confirmation_dialog(
                    "Charge level is too low", next=main_menu, single_option=True
                )
        confirmation_dialog(
            "Are you sure you want to update the firmware?",
            next=self.update_firmware,
            nextno=main_menu,
            single_option=False,
        )

    def update_firmware(self, *args):
        global current_fw_version
        current_addr = pijuice.config.interface.GetAddress()
        error_status = None
        if current_addr:
            # Set up the 'Wait for update' screen
            spinner = ["-", "\\", "|", "/"]
            i = 0
            waittext = urwid.Text(
                "Updating firmware, Wait " + spinner[i], align="center"
            )
            main.original_widget = urwid.Filler(
                urwid.LineBox(
                    urwid.Pile(
                        [
                            waittext,
                            urwid.Divider(),
                            urwid.Text(
                                "Interrupting this process can lead to a non-functional device.",
                                align="center",
                            ),
                        ]
                    )
                )
            )
            loop.draw_screen()
            # Start the firmware update in a subprocess
            addr = format(current_addr, "x")
            with open("/dev/null", "w") as f:  # Suppress pijuiceboot output
                p = subprocess.Popen(
                    ["pijuiceboot", addr, self.firmware_path],
                    stdout=f,
                    stderr=subprocess.STDOUT,
                )
            # Show the 'Wait for update' screen  with a rotating spinner
            finished = False
            while not finished:
                try:
                    finished = True
                    p.communicate(timeout=0.3)
                except subprocess.TimeoutExpired:
                    finished = False
                if not finished:
                    i = (i + 1) % 4
                    waittext.set_text("Updating firmware, Wait " + spinner[i])
                    loop.draw_screen()
            # Check the result
            result = 256 - p.returncode
            if result != 256:
                error_status = (
                    self.FIRMWARE_UPDATE_ERRORS[result] if result < 11 else "UNKNOWN"
                )
                messages = {
                    "I2C_BUS_ACCESS_ERROR": "Check if I2C bus is enabled.",
                    "INPUT_FILE_OPEN_ERROR": "Firmware binary file might be missing or damaged.",
                    "STARTING_BOOTLOADER_ERROR": "Try to start bootloader manually. Press and hold button SW3 while powering up RPI and PiJuice.",
                    "UNKNOWN_ADDRESS": "Unknown PiJuice I2C address",
                }
        else:
            error_status = "UNKNOWN_ADDRESS"

        if error_status:
            message = (
                "Firmware update failed.\nReason: "
                + error_status
                + ". "
                + messages.get(error_status, "")
            )
        else:
            # Wait till firmware has restarted (current_version != 0)
            main.original_widget = urwid.Filler(
                urwid.LineBox(
                    urwid.Pile(
                        [
                            urwid.Divider(),
                            urwid.Text("Waiting for firmware restart.", align="center"),
                            urwid.Divider(),
                        ]
                    )
                )
            )
            loop.draw_screen()
            current_version = 0
            while current_version == 0:
                current_version = get_current_fw_version()
                time.sleep(0.2)
            current_fw_version = current_version
            message = (
                "Firmware update successful"
                + ": V"
                + version_to_str(current_fw_version)
            )

        confirmation_dialog(message, single_option=True, next=self.show_firmware)

    def show_firmware(self, *args):
        current_version, latest_version, firmware_status, firmware_path = (
            self.get_fw_status()
        )
        current_version_txt = urwid.Text(
            "Current version: " + version_to_str(current_version)
        )
        firmware_path_txt = urwid.Text("Path: " + str(firmware_path))
        status_txt = urwid.Text("Status: " + firmware_status)
        elements = [
            urwid.Text("Firmware"),
            urwid.Divider(),
            current_version_txt,
            status_txt,
            firmware_path_txt,
            urwid.Divider(),
        ]
        if latest_version > current_version:
            self.firmware_path = firmware_path
            elements.extend(
                [
                    urwid.Padding(
                        attrmap(
                            ActionButton("Update", on_press=self.update_firmware_start)
                        ),
                        width=10,
                    ),
                    urwid.Divider(),
                ]
            )
        elements.append(
            urwid.Padding(attrmap(ActionButton("Back", on_press=main_menu)), width=10)
        )
        main.original_widget = urwid.Filler(urwid.Pile(elements), valign="top")


class GeneralTab(object):
    RUN_PIN_VALUES = PiJuiceConfig.runPinConfigs
    EEPROM_ADDRESSES = PiJuiceConfig.idEepromAddresses
    INPUTS_PRECEDENCE = PiJuiceConfig.powerInputs
    USB_CURRENT_LIMITS = PiJuiceConfig.usbMicroCurrentLimits
    USB_MICRO_IN_DPMS = PiJuiceConfig.usbMicroDPMs
    POWER_REGULATOR_MODES = PiJuiceConfig.powerRegulatorModes

    def __init__(self, *args):
        try:
            self.current_config = self._get_device_config()
            self.main()
        except:
            confirmation_dialog(
                "Unable to connect to device", single_option=True, next=main_menu
            )

    def _get_device_config(self):
        config = {}
        config["run_pin"] = self.RUN_PIN_VALUES.index(
            pijuice.config.GetRunPinConfig().get("data")
        )
        config["i2c_addr"] = pijuice.config.GetAddress(1).get("data")
        config["i2c_addr_rtc"] = pijuice.config.GetAddress(2).get("data")
        config["eeprom_addr"] = self.EEPROM_ADDRESSES.index(
            pijuice.config.GetIdEepromAddress().get("data")
        )
        config[
            "eeprom_write_unprotected"
        ] = not pijuice.config.GetIdEepromWriteProtect().get("data", False)
        result = pijuice.config.GetPowerInputsConfig()
        if result["error"] == "NO_ERROR":
            pow_config = result["data"]
            config["precedence"] = self.INPUTS_PRECEDENCE.index(
                pow_config["precedence"]
            )
            config["gpio_in_enabled"] = pow_config["gpio_in_enabled"]
            config["usb_micro_current_limit"] = self.USB_CURRENT_LIMITS.index(
                pow_config["usb_micro_current_limit"]
            )
            config["usb_micro_dpm"] = self.USB_MICRO_IN_DPMS.index(
                pow_config["usb_micro_dpm"]
            )
            config["no_battery_turn_on"] = pow_config["no_battery_turn_on"]

        config["power_reg_mode"] = self.POWER_REGULATOR_MODES.index(
            pijuice.config.GetPowerRegulatorMode().get("data")
        )
        config["charging_enabled"] = (
            pijuice.config.GetChargingConfig().get("data", {}).get("charging_enabled")
        )
        return config

    def main(self, *args):
        global pijuiceConfigData
        elements = [urwid.Text("General settings"), urwid.Divider()]

        options_with_lists = [
            {"title": "Run pin", "list": self.RUN_PIN_VALUES, "key": "run_pin"},
            {
                "title": "EEPROM address",
                "list": self.EEPROM_ADDRESSES,
                "key": "eeprom_addr",
            },
            {
                "title": "Inputs precedence",
                "list": self.INPUTS_PRECEDENCE,
                "key": "precedence",
            },
            {
                "title": "USB micro current limit",
                "list": self.USB_CURRENT_LIMITS,
                "key": "usb_micro_current_limit",
            },
            {
                "title": "USB micro IN DPM",
                "list": self.USB_MICRO_IN_DPMS,
                "key": "usb_micro_dpm",
            },
            {
                "title": "Power regulator mode",
                "list": self.POWER_REGULATOR_MODES,
                "key": "power_reg_mode",
            },
        ]

        options_with_checkboxes = [
            {"title": "GPIO input enable", "key": "gpio_in_enabled"},
            {"title": "EEPROM write unprotect", "key": "eeprom_write_unprotected"},
            {"title": "Charging enabled", "key": "charging_enabled"},
            {"title": "No battery turn on", "key": "no_battery_turn_on"},
        ]

        # I2C address
        i2c_addr_edit = urwid.Edit(
            "I2C address: ", edit_text=str(self.current_config["i2c_addr"])
        )
        urwid.connect_signal(
            i2c_addr_edit,
            "change",
            lambda x, text: self.current_config.update({"i2c_addr": text}),
        )
        elements.append(attrmap(i2c_addr_edit))

        # I2C address RTC
        i2c_addr_rtc_edit = urwid.Edit(
            "I2C address RTC: ", edit_text=str(self.current_config["i2c_addr_rtc"])
        )
        urwid.connect_signal(
            i2c_addr_rtc_edit,
            "change",
            lambda x, text: self.current_config.update({"i2c_addr_rtc": text}),
        )
        elements.append(attrmap(i2c_addr_rtc_edit))

        for option in options_with_checkboxes:
            elements.append(
                attrmap(
                    urwid.CheckBox(
                        option["title"],
                        state=self.current_config[option["key"]],
                        on_state_change=lambda x, state, key: (
                            self.current_config.update({key: state})
                        ),
                        user_data=option["key"],
                    )
                )
            )

        for option in options_with_lists:
            elements.append(
                attrmap(
                    ActionButton(
                        "{title}: {value}".format(
                            title=option["title"],
                            value=option["list"][self.current_config[option["key"]]],
                        ),
                        on_press=self._list_options,
                        user_data=option,
                    )
                )
            )

        elements.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(
                        ActionButton("Apply settings", on_press=self._apply_settings)
                    ),
                    width=20,
                ),
                urwid.Padding(
                    attrmap(
                        ActionButton(
                            "Reset to default",
                            on_press=lambda x: confirmation_dialog(
                                "This action will reset all settings on your device to their default values.\n"
                                "Do you want to proceed?",
                                single_option=False,
                                next=self._reset_settings,
                                nextno=main_menu,
                            ),
                        )
                    ),
                    width=20,
                ),
                urwid.Padding(
                    attrmap(ActionButton("Back", on_press=main_menu)), width=20
                ),
            ]
        )
        main.original_widget = urwid.Padding(
            CyclingListBox(urwid.SimpleFocusListWalker(elements)), width=48
        )

    def _list_options(self, button, data):
        body = [urwid.Text(data["title"]), urwid.Divider()]
        self.bgroup = []
        for choice in data["list"]:
            button = urwid.RadioButton(self.bgroup, choice)
            body.append(button)
        self.bgroup[self.current_config[data["key"]]].toggle_state()
        body.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    ActionButton("Back", on_press=self._set_option, user_data=data),
                    width=8,
                ),
            ]
        )
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(body))

    def _set_option(self, button, data):
        states = [c.state for c in self.bgroup]
        self.current_config[data["key"]] = states.index(True)
        self.bgroup = []
        self.main()

    def _apply_settings(self, *args):
        if loadPiJuiceConfig().get("battery_management", {}).get("enabled"):
            actual = pijuice.config.GetChargingConfig().get("data", {}).get("charging_enabled")
            if self.current_config.get("charging_enabled") != actual:
                raise ValueError("Charging is managed by the 80% limit. Disable it in Battery care before changing charging manually.")

        device_config = self._get_device_config()
        changed = [
            key
            for key in self.current_config.keys()
            if self.current_config[key] != device_config[key]
        ]

        for i, addr in enumerate(["i2c_addr", "i2c_addr_rtc"]):
            if addr in changed:
                value = device_config[addr]
                try:
                    new_value = int(str(self.current_config[addr]), 16)
                    if new_value >= 8 and new_value <= 0x77:
                        value = self.current_config[addr]
                    else:
                        self.current_config[addr] = value
                        return confirmation_dialog(
                            "I2C address has to be between 0x08 and 0x77",
                            next=self.main,
                        )
                except:
                    pass
                pijuice.config.SetAddress(i + 1, value)
                global pijuiceConfigData
                pijuiceConfigData.setdefault("board", {}).setdefault("general", {})[
                    "i2c_addr" + ["", "_rtc"][i]
                ] = value
                savePiJuiceConfig()
                _InitPiJuiceInterface()

        if "run_pin" in changed:
            pijuice.config.SetRunPinConfig(
                self.RUN_PIN_VALUES[self.current_config["run_pin"]]
            )

        if "eeprom_addr" in changed:
            pijuice.config.SetIdEepromAddress(
                self.EEPROM_ADDRESSES[self.current_config["eeprom_addr"]]
            )
        if "eeprom_write_unprotected" in changed:
            pijuice.config.SetIdEepromWriteProtect(
                not self.current_config["eeprom_write_unprotected"]
            )

        if set(
            [
                "precedence",
                "gpio_in_enabled",
                "usb_micro_current_limit",
                "usb_micro_dpm",
                "no_battery_turn_on",
            ]
        ) & set(changed):
            config = {
                "precedence": self.INPUTS_PRECEDENCE[self.current_config["precedence"]],
                "gpio_in_enabled": self.current_config["gpio_in_enabled"],
                "no_battery_turn_on": self.current_config["no_battery_turn_on"],
                "usb_micro_current_limit": self.USB_CURRENT_LIMITS[
                    self.current_config["usb_micro_current_limit"]
                ],
                "usb_micro_dpm": self.USB_MICRO_IN_DPMS[
                    self.current_config["usb_micro_dpm"]
                ],
            }
            pijuice.config.SetPowerInputsConfig(config, True)

        if "power_reg_mode" in changed:
            pijuice.config.SetPowerRegulatorMode(
                self.POWER_REGULATOR_MODES[self.current_config["power_reg_mode"]]
            )
        if "charging_enabled" in changed:
            pijuice.config.SetChargingConfig(
                {"charging_enabled": self.current_config["charging_enabled"]}, True
            )

        # Give PiJuice MCU sufficient time to change the settings before reading them back
        time.sleep(0.2)
        self.current_config = self._get_device_config()
        confirmation_dialog(
            "Settings successfully updated", single_option=True, next=self.main
        )

    def _reset_settings(self, button, is_confirmed):
        if is_confirmed:
            error = pijuice.config.SetDefaultConfiguration().get("error", "NO_ERROR")
            if error == "NO_ERROR":
                confirmation_dialog(
                    "Settings have been reset to their default values",
                    single_option=True,
                    next=main_menu,
                )
            else:
                confirmation_dialog(
                    "Failed to reset settings: " + error,
                    single_option=True,
                    next=main_menu,
                )
        else:
            self.main()


class LEDTab(object):
    LED_FUNCTIONS_OPTIONS = PiJuiceConfig.ledFunctionsOptions
    LED_NAMES = PiJuiceConfig.leds

    def __init__(self, *args):
        self._refresh_settings()
        self.main()

    def main(self, *args):
        elements = [urwid.Text("LED settings"), urwid.Divider()]
        for i in range(len(self.LED_NAMES)):
            elements.append(
                urwid.Padding(
                    attrmap(
                        ActionButton(
                            self.LED_NAMES[i], on_press=self.configure_led, user_data=i
                        )
                    ),
                    width=6,
                )
            )

        elements.extend(
            [
                urwid.Divider(),
                attrmap(ActionButton("Apply settings", on_press=self._apply_settings)),
                attrmap(ActionButton("Refresh", on_press=self._refresh_settings)),
                attrmap(ActionButton("Back", on_press=main_menu)),
            ]
        )
        main.original_widget = urwid.Padding(
            CyclingListBox(urwid.SimpleFocusListWalker(elements)), width=18
        )

    def configure_led(self, button, index):
        elements = [urwid.Text("LED " + self.LED_NAMES[index]), urwid.Divider()]
        colors = ("R", "G", "B")
        button = attrmap(
            ActionButton(
                "Function: {value}".format(
                    value=self.current_config[index]["function"]
                ),
                on_press=self._list_functions,
                user_data=index,
            )
        )
        elements.append(urwid.Padding(button, width=30))
        for color in colors:
            color_edit = urwid.Edit(
                color + ": ",
                edit_text=str(self.current_config[index]["color"][colors.index(color)]),
            )
            urwid.connect_signal(
                color_edit,
                "change",
                self._set_color,
                user_args=[{"color_index": colors.index(color), "led_index": index}],
            )
            elements.append(attrmap(color_edit))
        elements.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(ActionButton("Back", on_press=self.main)), width=8
                ),
            ]
        )
        main.original_widget = urwid.Filler(urwid.Pile(elements), valign="top")

    def _get_led_config(self):
        config = []
        for i in range(len(self.LED_NAMES)):
            result = pijuice.config.GetLedConfiguration(self.LED_NAMES[i])
            led_config = {}
            try:
                led_config["function"] = result["data"]["function"]
            except ValueError:
                led_config["function"] = self.LED_FUNCTIONS_OPTIONS[0]
            led_config["color"] = [
                result["data"]["parameter"]["r"],
                result["data"]["parameter"]["g"],
                result["data"]["parameter"]["b"],
            ]
            config.append(led_config)
        return config

    def _refresh_settings(self, *args):
        self.current_config = self._get_led_config()

    def _apply_settings(self, *args):
        for led in self.current_config:
            for value in led["color"]:
                validate_value(value, "int", 0, 255, None)
        for i in range(len(self.LED_NAMES)):
            config = {
                "function": self.current_config[i]["function"],
                "parameter": {
                    "r": self.current_config[i]["color"][0],
                    "g": self.current_config[i]["color"][1],
                    "b": self.current_config[i]["color"][2],
                },
            }
            result = pijuice.config.SetLedConfiguration(self.LED_NAMES[i], config)
            if result.get("error") != "NO_ERROR":
                raise ValueError("%s: %s. Earlier LED changes may already have applied." % (self.LED_NAMES[i], result.get("error")))

        self.current_config = self._get_led_config()
        confirmation_dialog(
            "Settings successfully updated", single_option=True, next=self.main
        )

    def _list_functions(self, button, led_index):
        body = [
            urwid.Text("Choose function for " + self.LED_NAMES[led_index]),
            urwid.Divider(),
        ]
        self.bgroup = []
        for choice in self.LED_FUNCTIONS_OPTIONS:
            button = urwid.RadioButton(self.bgroup, choice)
            body.append(attrmap(button))
        self.bgroup[
            self.LED_FUNCTIONS_OPTIONS.index(self.current_config[led_index]["function"])
        ].toggle_state()
        body.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(
                        ActionButton(
                            "Back", on_press=self._set_function, user_data=led_index
                        )
                    ),
                    width=8,
                ),
            ]
        )
        main.original_widget = urwid.Filler(urwid.Pile(body), valign="top")

    def _set_function(self, button, led_index):
        states = [c.state for c in self.bgroup]
        self.current_config[led_index]["function"] = self.LED_FUNCTIONS_OPTIONS[
            states.index(True)
        ]
        self.bgroup = []
        self.configure_led(None, led_index)

    def _set_color(self, data, edit, text):
        led_index = data["led_index"]
        color_index = data["color_index"]
        key = "%s %s" % (self.LED_NAMES[led_index], ("Red", "Green", "Blue")[color_index])
        self.current_config[led_index]["color"][color_index] = _validate_edit(edit, text, "int", 0, 255, key)
        self.current_config[led_index]["function"] = "USER_LED"



class ButtonsTab(object):
    FUNCTIONS = (
        ["NO_FUNC"]
        + pijuice_hard_functions
        + pijuice_sys_functions
        + pijuice_user_functions
    )
    BUTTONS = PiJuiceConfig.buttons
    EVENTS = PiJuiceConfig.buttonEvents

    def __init__(self):
        self.device_config = self._get_device_config()
        self.current_config = self._get_device_config()
        self.main()

    def main(self, *args):
        elements = [urwid.Text("Buttons"), urwid.Divider()]
        for sw_id in self.BUTTONS:
            elements.append(
                urwid.Padding(
                    attrmap(
                        ActionButton(sw_id, on_press=self.configure_sw, user_data=sw_id)
                    ),
                    width=7,
                )
            )
        elements.append(urwid.Divider())
        if self.device_config != self.current_config:
            elements.append(
                urwid.Padding(
                    attrmap(
                        ActionButton("Apply settings", on_press=self._apply_settings)
                    ),
                    width=18,
                )
            )
        elements.extend(
            [
                urwid.Padding(
                    attrmap(ActionButton("Refresh", on_press=self._refresh_settings)),
                    width=11,
                ),
                urwid.Padding(
                    attrmap(ActionButton("Back", on_press=main_menu)), width=11
                ),
            ]
        )
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(elements))

    def configure_sw(self, button, sw_id):
        elements = [urwid.Text("Settings for " + sw_id), urwid.Divider()]
        config = self.current_config[sw_id]
        for action, parameters in config.items():
            elements.append(
                attrmap(
                    ActionButton(
                        "{action}: {function}, {parameter}".format(
                            action=action,
                            function=parameters["function"],
                            parameter=parameters["parameter"],
                        ),
                        on_press=self.configure_action,
                        user_data={"sw_id": sw_id, "action": action},
                    )
                )
            )
        elements += [
            urwid.Divider(),
            urwid.Padding(attrmap(ActionButton("Back", on_press=self.main)), width=8),
        ]
        main.original_widget = urwid.Padding(
            CyclingListBox(urwid.SimpleFocusListWalker(elements)), width=46
        )

    def configure_action(self, button, data):
        sw_id = data["sw_id"]
        action = data["action"]
        functions_btn = attrmap(
            ActionButton(
                "Function: {}".format(self.current_config[sw_id][action]["function"]),
                on_press=self._set_function,
                user_data={"sw_id": sw_id, "action": action},
            )
        )
        parameter_edit = urwid.Edit(
            "Parameter: ",
            edit_text=str(self.current_config[sw_id][action]["parameter"]),
        )
        urwid.connect_signal(
            parameter_edit,
            "change",
            self._set_parameter,
            user_args=[{"sw_id": sw_id, "action": action}],
        )
        parameter_edit = attrmap(parameter_edit)
        parameter_text = urwid.Text(
            "Parameter: " + str(self.current_config[sw_id][action]["parameter"])
        )
        if action != "PRESS" and action != "RELEASE":
            paramline = parameter_edit
        else:
            paramline = parameter_text
        back_btn = urwid.Padding(
            attrmap(ActionButton("Back", on_press=self.configure_sw, user_data=sw_id)),
            width=8,
        )
        elements = [
            urwid.Text("Set function for {} on {}".format(action, sw_id)),
            urwid.Divider(),
            functions_btn,
            paramline,
            urwid.Divider(),
            back_btn,
        ]
        main.original_widget = urwid.Padding(
            CyclingListBox(urwid.SimpleFocusListWalker(elements)), width=37
        )

    def _refresh_settings(self, *args):
        self.device_config = self._get_device_config()
        self.current_config = self._get_device_config()
        confirmation_dialog(
            "Settings have been refreshed", next=self.main, single_option=True
        )

    def _set_function(self, button, data):
        sw_id = data["sw_id"]
        action = data["action"]
        body = [
            urwid.Text("Choose function for {} on {}".format(action, sw_id)),
            urwid.Divider(),
        ]
        self.bgroup = []
        for function in self.FUNCTIONS:
            button = attrmap(urwid.RadioButton(self.bgroup, function))
            body.append(button)
        self.bgroup[
            self.FUNCTIONS.index(self.current_config[sw_id][action]["function"])
        ].toggle_state()
        body.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(
                        ActionButton(
                            "Back", on_press=self._on_function_chosen, user_data=data
                        )
                    ),
                    width=8,
                ),
            ]
        )
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(body))

    def _on_function_chosen(self, button, data):
        states = [c.state for c in self.bgroup]
        self.current_config[data["sw_id"]][data["action"]]["function"] = self.FUNCTIONS[
            states.index(True)
        ]
        self.bgroup = []
        self.configure_action(None, data)

    def _set_parameter(self, data, edit, text):
        sw_id = data["sw_id"]
        action = data["action"]
        # 'PRESS' and 'RELEASE' take no parameter
        if action != "PRESS" and action != "RELEASE":
            self.current_config[sw_id][action]["parameter"] = _validate_edit(edit, text, "int", 0, 25500, sw_id + " " + action + " delay (ms)")
            if text.isdigit() and int(text) % 100:
                _errors[sw_id + " " + action + " delay (ms)"] = "Button delays must use steps of 100 ms."
                _flash("Button delays must use steps of 100 ms.", "error")

    def _get_device_config(self):
        config = {}
        got_error = False
        for button in self.BUTTONS:
            button_config = pijuice.config.GetButtonConfiguration(button)
            if button_config.get("error") == "NO_ERROR":
                config[button] = button_config.get("data")
            else:
                config[button] = {}
                got_error = True

        if got_error:
            confirmation_dialog(
                "Failed to connect to PiJuice", next=main_menu, single_option=True
            )
        else:
            return config

    def _apply_settings(self, *args):
        got_error = False
        errors = []
        for button in self.BUTTONS:
            error_msg = pijuice.config.SetButtonConfiguration(
                button, self.current_config[button]
            ).get("error", "NO_ERROR")
            errors.append(error_msg)
            got_error |= error_msg != "NO_ERROR"

        if got_error:
            confirmation_dialog(
                "Failed to apply settings: " + str(errors),
                next=self.main,
                single_option=True,
            )
        else:
            self.device_config = self._get_device_config()
            self.current_config = copy.deepcopy(self.device_config)
            notify_service()
            confirmation_dialog(
                "Settings have been applied", next=self.main, single_option=True
            )


class IOTab(object):
    IO_PINS_COUNT = 2
    IO_CONFIG_PARAMS = PiJuiceConfig.ioConfigParams  # mode: [var_1, var_2]
    IO_SUPPORTED_MODES = PiJuiceConfig.ioSupportedModes
    IO_PULL_OPTIONS = PiJuiceConfig.ioPullOptions

    def __init__(self):
        self.current_config = self._get_device_config()
        self.main()

    def main(self, *args):
        elements = [urwid.Text("IO settings"), urwid.Divider()]
        for i in range(self.IO_PINS_COUNT):
            elements.append(
                urwid.Padding(
                    attrmap(
                        ActionButton(
                            "IO" + str(i + 1), on_press=self.configure_io, user_data=i
                        )
                    ),
                    width=7,
                )
            )

        elements.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(
                        ActionButton(
                            "Apply settings",
                            on_press=self._apply_settings,
                            user_data=self.IO_PINS_COUNT,
                        )
                    ),
                    width=18,
                ),
                urwid.Padding(
                    attrmap(ActionButton("Back", on_press=main_menu)), width=18
                ),
            ]
        )
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(elements))

    def configure_io(self, button, pin_id):
        global current_fw_version
        elements = [urwid.Text("IO{}".format(pin_id + 1)), urwid.Divider()]
        pin_config = self.current_config[pin_id]
        mode = pin_config["mode"]
        pull = pin_config["pull"]
        # < Mode >
        mode_select_btn = urwid.Padding(
            attrmap(
                ActionButton(
                    "Mode: {}".format(mode),
                    on_press=self._select_mode,
                    user_data=pin_id,
                )
            ),
            width=32,
        )
        # < Pull >
        pull_select_btn = urwid.Padding(
            attrmap(
                ActionButton(
                    "Pull: {}".format(pull),
                    on_press=self._select_pull,
                    user_data=pin_id,
                )
            ),
            width=32,
        )
        elements += [mode_select_btn, pull_select_btn]
        # Edits for vars
        # XXX: Hack to pass var parameters
        if len(self.IO_CONFIG_PARAMS.get(mode, [])) > 0:
            var_config = self.IO_CONFIG_PARAMS[mode][0]
            var_name = var_config.get("name", "")
            var_unit = var_config.get("unit")
            var_type = var_config.get("type", "str")
            var_min = var_config.get("min")
            var_max = var_config.get("max")
            if var_name == "wakeup" and pin_id == 1 and current_fw_version >= 0x13:
                if pin_config["wakeup"] == "":
                    pin_config["wakeup"] = self.IO_CONFIG_PARAMS["DIGITAL_IN"][0][
                        "options"
                    ][0]
                wakeup_select_btn = urwid.Padding(
                    attrmap(
                        ActionButton(
                            "Wakeup: {}".format(pin_config["wakeup"]),
                            on_press=self._select_wakeup,
                            user_data=pin_id,
                        )
                    ),
                    width=32,
                )
                elements.append(wakeup_select_btn)
            elif var_name != "wakeup":
                label = (
                    "{} [{}-{} {}]: ".format(var_name, var_min, var_max, var_unit)
                    if var_unit
                    else "{} [{}-{}]: ".format(var_name, var_min, var_max)
                )
                if pin_config[var_name] == "":
                    pin_config[var_name] = var_min
                var_edit_1 = urwid.Edit(label, edit_text=str(pin_config[var_name]))
                # Validate int/float
                urwid.connect_signal(
                    var_edit_1,
                    "change",
                    self.check_value,
                    user_args=[pin_id, var_name, var_type, var_min, var_max],
                )
                elements.append(urwid.Padding(attrmap(var_edit_1), width=32))

        if len(self.IO_CONFIG_PARAMS.get(mode, [])) > 1:
            var_config = self.IO_CONFIG_PARAMS[mode][1]
            var_name = var_config.get("name", "")
            var_unit = var_config.get("unit")
            var_type = var_config.get("type", "str")
            var_min = var_config.get("min")
            var_max = var_config.get("max")
            label = (
                "{} [{}-{} {}]: ".format(var_name, var_min, var_max, var_unit)
                if var_unit
                else "{} [{}-{}]: ".format(var_name, var_min, var_max)
            )
            if pin_config[var_name] == "":
                pin_config[var_name] = var_min
            var_edit_2 = urwid.Edit(label, edit_text=str(pin_config[var_name]))
            # Validate int/float
            urwid.connect_signal(
                var_edit_2,
                "change",
                self.check_value,
                user_args=[pin_id, var_name, var_type, var_min, var_max],
            )
            elements.append(urwid.Padding(attrmap(var_edit_2), width=32))

        elements += [
            urwid.Divider(),
            urwid.Padding(
                attrmap(
                    ActionButton(
                        "Apply", on_press=self._apply_settings, user_data=pin_id
                    )
                ),
                width=9,
            ),
            urwid.Padding(attrmap(ActionButton("Back", on_press=self.main)), width=9),
        ]
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(elements))

    def check_value(self, pin_id, var_name, type, var_min, var_max, widget, text):
        self.current_config[pin_id][var_name] = _validate_edit(widget, text, type, var_min, var_max, "IO%s %s" % (pin_id + 1, var_name))

    def _select_mode(self, button, pin_id):
        elements = [urwid.Text("Mode for IO{}".format(pin_id + 1)), urwid.Divider()]
        self.bgroup = []
        for choice in self.IO_SUPPORTED_MODES[pin_id + 1]:
            elements.append(
                urwid.Padding(attrmap(urwid.RadioButton(self.bgroup, choice)), width=26)
            )
        # Toggle the configured state
        self.bgroup[
            self.IO_SUPPORTED_MODES[pin_id + 1].index(
                self.current_config[pin_id]["mode"]
            )
        ].toggle_state()

        elements.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(
                        ActionButton(
                            "Back", on_press=self._on_mode_selected, user_data=pin_id
                        )
                    ),
                    width=8,
                ),
            ]
        )
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(elements))

    def _on_mode_selected(self, button, pin_id):
        states = [c.state for c in self.bgroup]
        mode = self.IO_SUPPORTED_MODES[pin_id + 1][states.index(True)]
        pull = self.current_config[pin_id]["pull"]
        config = {"mode": mode, "pull": pull}
        for var in self.IO_CONFIG_PARAMS.get(mode, []):
            config[var["name"]] = ""
        self.current_config[pin_id] = config
        self.bgroup = []
        self.configure_io(None, pin_id)

    def _select_pull(self, button, pin_id):
        elements = [urwid.Text("Pull for IO{}".format(pin_id + 1)), urwid.Divider()]
        self.bgroup = []
        for choice in self.IO_PULL_OPTIONS:
            elements.append(
                urwid.Padding(attrmap(urwid.RadioButton(self.bgroup, choice)), width=13)
            )
        # Toggle the configured state
        self.bgroup[
            self.IO_PULL_OPTIONS.index(self.current_config[pin_id]["pull"])
        ].toggle_state()

        elements.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(
                        ActionButton(
                            "Back", on_press=self._on_pull_selected, user_data=pin_id
                        )
                    ),
                    width=8,
                ),
            ]
        )
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(elements))

    def _on_pull_selected(self, button, pin_id):
        states = [c.state for c in self.bgroup]
        self.current_config[pin_id]["pull"] = self.IO_PULL_OPTIONS[states.index(True)]
        self.bgroup = []
        self.configure_io(None, pin_id)

    def _select_wakeup(self, button, pin_id):
        elements = [urwid.Text("Select Wakeup Option"), urwid.Divider()]
        self.bgroup = []
        for choice in self.IO_CONFIG_PARAMS["DIGITAL_IN"][0]["options"]:
            elements.append(
                urwid.Padding(attrmap(urwid.RadioButton(self.bgroup, choice)), width=16)
            )
        # Toggle the configured state
        self.bgroup[
            self.IO_CONFIG_PARAMS["DIGITAL_IN"][0]["options"].index(
                self.current_config[pin_id]["wakeup"]
            )
        ].toggle_state()

        elements.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(
                        ActionButton(
                            "Back", on_press=self._on_wakeup_selected, user_data=pin_id
                        )
                    ),
                    width=8,
                ),
            ]
        )
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(elements))

    def _on_wakeup_selected(self, button, pin_id):
        states = [c.state for c in self.bgroup]
        self.current_config[pin_id]["wakeup"] = self.IO_CONFIG_PARAMS["DIGITAL_IN"][0][
            "options"
        ][states.index(True)]
        self.bgroup = []
        self.configure_io(None, pin_id)

    def _get_device_config(self, *args):
        config = []
        for i in range(self.IO_PINS_COUNT):
            result = pijuice.config.GetIoConfiguration(i + 1)
            if result["error"] != "NO_ERROR":
                confirmation_dialog(
                    "Unable to connect to device: {}".format(result["error"]),
                    next=main_menu,
                    single_option=True,
                )
            else:
                config.append(result["data"])
        return config

    def _apply_settings(self, button, pin_id):
        if pin_id >= self.IO_PINS_COUNT:
            # Apply for all pins
            errors = []
            for i in range(self.IO_PINS_COUNT):
                error_msg = self._apply_for_pin(i)
                if error_msg != "NO_ERROR":
                    errors.append(error_msg)
            if errors:
                confirmation_dialog(
                    "Failed to apply some settings. Error: {}".format(errors),
                    next=self.main,
                    single_option=True,
                )
            else:
                confirmation_dialog(
                    "Updated settings for all pins", next=self.main, single_option=True
                )
        else:
            error_msg = self._apply_for_pin(pin_id)
            if error_msg != "NO_ERROR":
                confirmation_dialog(
                    "Failed to apply settings for IO{}. Error: {}".format(
                        pin_id + 1, error_msg
                    ),
                    next=self.main,
                    single_option=True,
                )
            else:
                confirmation_dialog(
                    "Updated settings for IO{}".format(pin_id + 1),
                    next=self.main,
                    single_option=True,
                )

    def _apply_for_pin(self, pin_id):
        result = pijuice.config.SetIoConfiguration(
            pin_id + 1, self.current_config[pin_id], True
        )
        return result.get("error", "NO_ERROR")


class BatteryProfileTab(object):
    TEMP_SENSE_OPTIONS = PiJuiceConfig.batteryTempSenseOptions
    RSOC_ESTIMATION_OPTIONS = PiJuiceConfig.rsocEstimationOptions
    CHEMISTRY_OPTIONS = PiJuiceConfig.batteryChemistries
    EDIT_KEYS = [
        "capacity",
        "chargeCurrent",
        "terminationCurrent",
        "regulationVoltage",
        "cutoffVoltage",
        "tempCold",
        "tempCool",
        "tempWarm",
        "tempHot",
        "ntcB",
        "ntcResistance",
    ]
    EDIT_EXTKEYS = ["ocv10", "ocv50", "ocv90", "r10", "r50", "r90"]

    def __init__(self, *args):
        global current_fw_version
        self.status_text = ""
        self.custom_values = False
        pijuice.config.SelectBatteryProfiles(current_fw_version)
        self.BATTERY_PROFILES = pijuice.config.batteryProfiles + ["CUSTOM", "DEFAULT"]
        self.refresh()

    def main(self, *args):
        global current_fw_version
        elements = [
            urwid.Text("Battery settings"),
            urwid.Divider(),
            urwid.Text("Status: " + self.status_text),
            urwid.Padding(
                attrmap(
                    ActionButton(
                        "Profile: {}".format(profile_label(self.profile_name)),
                        on_press=self.select_profile,
                    )
                ),
                width=25,
            ),
            urwid.Divider(),
            urwid.Padding(
                attrmap(
                    urwid.CheckBox(
                        "Custom",
                        state=self.custom_values,
                        on_state_change=self._toggle_custom_values,
                    )
                ),
                width=32,
            ),
        ]

        if self.profile_data == "INVALID":
            elements.extend(
                [
                    urwid.Divider(),
                    urwid.Padding(
                        attrmap(ActionButton("Refresh", on_press=self.refresh)),
                        width=18,
                    ),
                    urwid.Padding(
                        attrmap(
                            ActionButton(
                                "Apply settings", on_press=self._apply_settings
                            )
                        ),
                        width=18,
                    ),
                    urwid.Padding(
                        attrmap(ActionButton("Back", on_press=main_menu)), width=18
                    ),
                ]
            )

            main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(elements))
            return

        self.param_edits = [
            urwid.IntEdit(
                "Capacity [mAh]:           ", default=self.profile_data["capacity"]
            ),
            urwid.IntEdit(
                "Charge current [mA]:      ", default=self.profile_data["chargeCurrent"]
            ),
            urwid.IntEdit(
                "Termination current [mA]: ",
                default=self.profile_data["terminationCurrent"],
            ),
            urwid.IntEdit(
                "Regulation voltage [mV]:  ",
                default=self.profile_data["regulationVoltage"],
            ),
            urwid.IntEdit(
                "Cutoff voltage [mV]:      ", default=self.profile_data["cutoffVoltage"]
            ),
            urwid.IntEdit(
                "Cold temperature [C]:     ", default=self.profile_data["tempCold"]
            ),
            urwid.IntEdit(
                "Cool temperature [C]:     ", default=self.profile_data["tempCool"]
            ),
            urwid.IntEdit(
                "Warm temperature [C]:     ", default=self.profile_data["tempWarm"]
            ),
            urwid.IntEdit(
                "Hot temperature [C]:      ", default=self.profile_data["tempHot"]
            ),
            urwid.IntEdit(
                "NTC B constant [1k]:      ", default=self.profile_data["ntcB"]
            ),
            urwid.IntEdit(
                "NTC resistance [ohm]:     ", default=self.profile_data["ntcResistance"]
            ),
        ]
        if current_fw_version >= 0x13:
            self.param_edits += [
                urwid.IntEdit(
                    "OCV10 [mV]:               ", default=self.ext_profile_data["ocv10"]
                ),
                urwid.IntEdit(
                    "OCV50 [mV]:               ", default=self.ext_profile_data["ocv50"]
                ),
                urwid.IntEdit(
                    "OCV90 [mV]:               ", default=self.ext_profile_data["ocv90"]
                ),
                FloatEdit(
                    "R10 [mOhm]:               ", default=self.ext_profile_data["r10"]
                ),
                FloatEdit(
                    "R50 [mOhm]:               ", default=self.ext_profile_data["r50"]
                ),
                FloatEdit(
                    "R90 [mOhm]:               ", default=self.ext_profile_data["r90"]
                ),
            ]

        for i, edit in enumerate(self.param_edits):
            if i < 11:
                urwid.connect_signal(
                    edit,
                    "change",
                    lambda x, text, idx: self.profile_data.update(
                        {self.EDIT_KEYS[idx]: text}
                    ),
                    i,
                )
            else:
                urwid.connect_signal(
                    edit,
                    "change",
                    lambda x, text, idx: self.ext_profile_data.update(
                        {self.EDIT_EXTKEYS[idx]: text}
                    ),
                    i - 11,
                )

        if self.custom_values:
            elements.extend(
                [
                    urwid.Padding(
                        attrmap(
                            ActionButton(
                                "Chemistry:              {}".format(
                                    self.CHEMISTRY_OPTIONS[self.chemistries_idx]
                                ),
                                on_press=self.select_chemistry,
                            )
                        ),
                        width=36,
                    ),
                ]
            )
            for edit in self.param_edits:
                elements += [
                    urwid.Padding(attrmap(edit), width=32),
                ]
        else:
            if current_fw_version >= 0x13:
                elements += [
                    urwid.Text(
                        "Chemistry:                "
                        + self.ext_profile_data["chemistry"]
                    ),
                ]
            elements += [
                urwid.Text(
                    "Capacity [mAh]:           " + str(self.profile_data["capacity"])
                ),
                urwid.Text(
                    "Charge current [mA]:      "
                    + str(self.profile_data["chargeCurrent"])
                ),
                urwid.Text(
                    "Termination current [mA]: "
                    + str(self.profile_data["terminationCurrent"])
                ),
                urwid.Text(
                    "Regulation voltage [mV]:  "
                    + str(self.profile_data["regulationVoltage"])
                ),
                urwid.Text(
                    "Cutoff voltage [mV]:      "
                    + str(self.profile_data["cutoffVoltage"])
                ),
                urwid.Text(
                    "Cold temperature [C]:     " + str(self.profile_data["tempCold"])
                ),
                urwid.Text(
                    "Cool temperature [C]:     " + str(self.profile_data["tempCool"])
                ),
                urwid.Text(
                    "Warm temperature [C]:     " + str(self.profile_data["tempWarm"])
                ),
                urwid.Text(
                    "Hot temperature [C]:      " + str(self.profile_data["tempHot"])
                ),
                urwid.Text(
                    "NTC B constant [1k]:      " + str(self.profile_data["ntcB"])
                ),
                urwid.Text(
                    "NTC resistance [ohm]:     "
                    + str(self.profile_data["ntcResistance"])
                ),
            ]
            if current_fw_version >= 0x13:
                elements += [
                    urwid.Text(
                        "OCV10 [mV]:               "
                        + str(self.ext_profile_data["ocv10"])
                    ),
                    urwid.Text(
                        "OCV50 [mV]:               "
                        + str(self.ext_profile_data["ocv50"])
                    ),
                    urwid.Text(
                        "OCV90 [mV]:               "
                        + str(self.ext_profile_data["ocv90"])
                    ),
                    urwid.Text(
                        "R10 [mOhm]:               " + str(self.ext_profile_data["r10"])
                    ),
                    urwid.Text(
                        "R50 [mOhm]:               " + str(self.ext_profile_data["r50"])
                    ),
                    urwid.Text(
                        "R90 [mOhm]:               " + str(self.ext_profile_data["r90"])
                    ),
                ]

        elements.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(
                        ActionButton(
                            "Temperature sense: {}".format(
                                self.TEMP_SENSE_OPTIONS[self.temp_sense_profile_idx]
                            ),
                            on_press=self.select_sense,
                        )
                    ),
                    width=34,
                ),
                urwid.Divider(),
            ]
        )
        if current_fw_version >= 0x13:
            elements.extend(
                [
                    urwid.Padding(
                        attrmap(
                            ActionButton(
                                "Rsoc estimation: {}".format(
                                    self.RSOC_ESTIMATION_OPTIONS[
                                        self.rsoc_estimation_profile_idx
                                    ]
                                ),
                                on_press=self.select_rsoc_estimate,
                            )
                        ),
                        width=34,
                    ),
                    urwid.Divider(),
                ]
            )
        elements.extend(
            [
                urwid.Padding(
                    attrmap(ActionButton("Refresh", on_press=self.refresh)), width=18
                ),
                urwid.Padding(
                    attrmap(
                        ActionButton("Apply settings", on_press=self._apply_settings)
                    ),
                    width=18,
                ),
                urwid.Padding(
                    attrmap(ActionButton("Back", on_press=main_menu)), width=18
                ),
            ]
        )

        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(elements))

    def refresh(self, *args):
        global current_fw_version
        self._read_profile_status()
        self._read_profile_data()
        self._read_temp_sense()
        if current_fw_version >= 0x13:
            self._read_rsoc_estimation()
            self._read_chemistry()
        self.main()

    def _read_profile_data(self, *args):
        global current_fw_version
        config = pijuice.config.GetBatteryProfile()
        if config["error"] == "NO_ERROR":
            self.profile_data = config["data"]
        else:
            confirmation_dialog(
                "Unable to read battery data. Error: {}".format(config["error"]),
                next=main_menu,
                single_option=True,
            )
        if current_fw_version >= 0x13:
            extconfig = pijuice.config.GetBatteryExtProfile()
            if extconfig["error"] == "NO_ERROR":
                self.ext_profile_data = extconfig["data"]
            else:
                confirmation_dialog(
                    "Unable to read battery data. Error: {}".format(extconfig["error"]),
                    next=main_menu,
                    single_option=True,
                )

    def _read_profile_status(self, *args):
        self.profile_name = "CUSTOM"
        self.status_text = ""
        status = pijuice.config.GetBatteryProfileStatus()
        if status["error"] == "NO_ERROR":
            self.profile_status = status["data"]

            if self.profile_status["validity"] == "VALID":
                if self.profile_status["origin"] == "PREDEFINED":
                    self.profile_name = self.profile_status["profile"]
            else:
                self.status_text = "Invalid battery profile"
                return

            if (
                self.profile_status["source"] == "DIP_SWITCH"
                and self.profile_status["origin"] == "PREDEFINED"
                and self.BATTERY_PROFILES.index(self.profile_name) == 1
            ):
                self.status_text = "Default profile"
            else:
                self.status_text = (
                    "Custom profile by: "
                    if self.profile_status["origin"] == "CUSTOM"
                    else "Profile selected by: "
                )
                self.status_text += self.profile_status["source"]
        else:
            confirmation_dialog(
                "Unable to read battery data. Error: {}".format(status["error"]),
                next=main_menu,
                single_option=True,
            )

    def _read_temp_sense(self, *args):
        temp_sense_config = pijuice.config.GetBatteryTempSenseConfig()
        if temp_sense_config["error"] == "NO_ERROR":
            self.temp_sense_profile_idx = self.TEMP_SENSE_OPTIONS.index(
                temp_sense_config["data"]
            )
        else:
            confirmation_dialog(
                "Unable to read battery data. Error: {}".format(
                    temp_sense_config["error"]
                ),
                next=main_menu,
                single_option=True,
            )

    def _read_rsoc_estimation(self, *args):
        rsoc_estimation_config = pijuice.config.GetRsocEstimationConfig()
        if rsoc_estimation_config["error"] == "NO_ERROR":
            self.rsoc_estimation_profile_idx = self.RSOC_ESTIMATION_OPTIONS.index(
                rsoc_estimation_config["data"]
            )
        else:
            confirmation_dialog(
                "Unable to read battery data. Error: {}".format(
                    rsoc_estimation_config["error"]
                ),
                next=main_menu,
                single_option=True,
            )

    def _read_chemistry(self, *args):
        self.chemistries_idx = self.CHEMISTRY_OPTIONS.index(
            self.ext_profile_data["chemistry"]
        )

    def _clear_text_edits(self, *args):
        for edit in self.param_edits:
            edit.set_edit_text("")

    def _toggle_custom_values(self, *args):
        self.custom_values ^= True
        self.main()

    def select_profile(self, *args):
        body = [urwid.Text(("title", "Select battery profile")),
                urwid.Text("Use the exact model, not just matching capacity. These profiles come from the installed firmware."), urwid.Divider()]
        self.bgroup = []
        for choice in self.BATTERY_PROFILES:
            button = urwid.RadioButton(self.bgroup, profile_label(choice))
            body.append(attrmap(button))
        self.bgroup[self.BATTERY_PROFILES.index(self.profile_name)].toggle_state()
        body.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(ActionButton("Back", on_press=self._set_profile)), width=8
                ),
            ]
        )
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(body))

    def _set_profile(self, *args):
        states = [c.state for c in self.bgroup]
        self.profile_name = self.BATTERY_PROFILES[states.index(True)]
        self.bgroup = []
        self.main()

    def select_sense(self, *args):
        body = [urwid.Text("Select temperature sense"), urwid.Divider()]
        self.bgroup = []
        for choice in self.TEMP_SENSE_OPTIONS:
            button = urwid.RadioButton(self.bgroup, choice)
            body.append(urwid.Padding(attrmap(button), width=16))
        self.bgroup[self.temp_sense_profile_idx].toggle_state()
        body.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(ActionButton("Back", on_press=self._set_sense)), width=8
                ),
            ]
        )
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(body))

    def select_rsoc_estimate(self, *args):
        body = [urwid.Text("Select Rsoc estimation"), urwid.Divider()]
        self.bgroup = []
        for choice in self.RSOC_ESTIMATION_OPTIONS:
            button = urwid.RadioButton(self.bgroup, choice)
            body.append(urwid.Padding(attrmap(button), width=18))
        self.bgroup[self.rsoc_estimation_profile_idx].toggle_state()
        body.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(ActionButton("Back", on_press=self._set_rsoc_estimation)),
                    width=8,
                ),
            ]
        )
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(body))

    def select_chemistry(self, *args):
        body = [urwid.Text("Select Chemistry"), urwid.Divider()]
        self.bgroup = []
        for choice in self.CHEMISTRY_OPTIONS:
            button = urwid.RadioButton(self.bgroup, choice)
            body.append(urwid.Padding(attrmap(button), width=18))
        self.bgroup[self.chemistries_idx].toggle_state()
        body.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(ActionButton("Back", on_press=self._set_chemistry)), width=8
                ),
            ]
        )
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(body))

    def _set_sense(self, *args):
        states = [c.state for c in self.bgroup]
        self.temp_sense_profile_idx = states.index(True)
        self.bgroup = []
        self.main()

    def _set_rsoc_estimation(self, *args):
        states = [c.state for c in self.bgroup]
        self.rsoc_estimation_profile_idx = states.index(True)
        self.bgroup = []
        self.main()

    def _set_chemistry(self, *args):
        states = [c.state for c in self.bgroup]
        self.chemistries_idx = states.index(True)
        self.bgroup = []
        self.main()

    def _apply_settings(self, *args):
        if self.custom_values:
            self._validated_custom_values()  # Validate all fields before the first device write.

        status = pijuice.config.SetBatteryTempSenseConfig(
            self.TEMP_SENSE_OPTIONS[self.temp_sense_profile_idx]
        )
        if status["error"] != "NO_ERROR":
            confirmation_dialog(
                "Failed to apply temperature sense options. Error: {}".format(
                    status["error"]
                ),
                next=main_menu,
                single_option=True,
            )

        status = pijuice.config.SetRsocEstimationConfig(
            self.RSOC_ESTIMATION_OPTIONS[self.rsoc_estimation_profile_idx]
        )
        if status["error"] != "NO_ERROR":
            confirmation_dialog(
                "Failed to apply rsoc estimation options. Error: {}".format(
                    status["error"]
                ),
                next=main_menu,
                single_option=True,
            )

        if self.custom_values:
            status = self.write_custom_values()
            self.custom_values = False
        else:
            status = pijuice.config.SetBatteryProfile(self.profile_name)

        if status["error"] != "NO_ERROR":
            confirmation_dialog(
                "Failed to apply profile options. Error: {}".format(status["error"]),
                next=main_menu,
                single_option=True,
            )
        else:
            confirmation_dialog(
                "Settings successfully updated", single_option=True, next=self.refresh
            )

    def _validated_custom_values(self):
        ranges = [(1, 4194175), (550, 2500), (50, 400), (3500, 4440), (0, 5100),
                  (-128, 127), (-128, 127), (-128, 127), (-128, 127), (1, 65535), (10, 655350)]
        profile = {}
        for key, edit, (lo, hi) in zip(self.EDIT_KEYS, self.param_edits, ranges):
            try:
                profile[key] = int(validate_value(edit.edit_text, "int", lo, hi, None))
            except ValueError as exc:
                raise ValueError(key + ": " + str(exc))
        for key, origin, step in (("chargeCurrent", 550, 75), ("terminationCurrent", 50, 50),
                                  ("regulationVoltage", 3500, 20), ("cutoffVoltage", 0, 20), ("ntcResistance", 0, 10)):
            if (profile[key] - origin) % step:
                raise ValueError("%s must use steps of %s starting at %s; no silent rounding." % (key, step, origin))
        if profile["capacity"] >= 32768 and profile["capacity"] % 128:
            raise ValueError("Capacities above 32767 mAh must use steps of 128 mAh.")
        if not profile["tempCold"] <= profile["tempCool"] < profile["tempWarm"] <= profile["tempHot"]:
            raise ValueError("Temperature thresholds must be ordered: cold ≤ cool < warm ≤ hot.")
        if profile["cutoffVoltage"] >= profile["regulationVoltage"]:
            raise ValueError("Cutoff voltage must be below regulation voltage.")
        extension = None
        if current_fw_version >= 0x13:
            extension = {"chemistry": self.CHEMISTRY_OPTIONS[self.chemistries_idx]}
            for key, edit in zip(self.EDIT_EXTKEYS, self.param_edits[11:]):
                kind, lo, hi = ("float", 0, 655.35) if key.startswith("r") else ("int", 1, 65535)
                extension[key] = float(validate_value(edit.edit_text, kind, lo, hi, None)) if kind == "float" else int(validate_value(edit.edit_text, kind, lo, hi, None))
            if not extension["ocv10"] <= extension["ocv50"] <= extension["ocv90"]:
                raise ValueError("Open-circuit voltages must increase from 10% to 90% charge.")
        return profile, extension

    def write_custom_values(self, *args):
        profile, extension = self._validated_custom_values()
        status = pijuice.config.SetCustomBatteryProfile(profile)
        if status["error"] != "NO_ERROR" or extension is None:
            return status
        return pijuice.config.SetCustomBatteryExtProfile(extension)


class WakeupAlarmTab(object):
    def __init__(self, *args):
        try:
            self.current_config = self._get_alarm()
        except Exception as e:
            confirmation_dialog(
                "Unable to connect to device: {}".format(str(e)),
                next=main_menu,
                single_option=True,
            )
        else:
            self.status = "OK"
            self.device_time = self._get_device_time()
            self.main()

    def _get_alarm(self, *args):
        status = {unit: {} for unit in ("day", "hour", "minute", "second")}
        # Empty by default
        for unit in ("day", "hour", "minute", "second"):
            status[unit]["value"] = ""

        ctr = pijuice.rtcAlarm.GetControlStatus()
        if ctr["error"] != "NO_ERROR":
            raise Exception(ctr["error"])
        status["enabled"] = ctr["data"]["alarm_wakeup_enabled"]

        alarm = pijuice.rtcAlarm.GetAlarm()
        if alarm["error"] != "NO_ERROR":
            raise Exception(alarm["error"])

        alarm = alarm["data"]

        if "day" in alarm:
            status["day"]["type"] = 0  # Day number
            if alarm["day"] == "EVERY_DAY":
                status["day"]["every_day"] = True
            else:
                status["day"]["every_day"] = False
                status["day"]["value"] = alarm["day"]
        elif "weekday" in alarm:
            status["day"]["type"] = 1  # Day of week number
            if alarm["weekday"] == "EVERY_DAY":
                status["day"]["every_day"] = True
            else:
                status["day"]["every_day"] = False
                status["day"]["value"] = alarm["weekday"]

        if "hour" in alarm:
            if alarm["hour"] == "EVERY_HOUR":
                status["hour"]["every_hour"] = True
            else:
                status["hour"]["every_hour"] = False
                status["hour"]["value"] = alarm["hour"]

        if "minute" in alarm:
            status["minute"]["type"] = 0  # Minute
            status["minute"]["value"] = alarm["minute"]
        elif "minute_period" in alarm:
            status["minute"]["type"] = 1  # Minute period
            status["minute"]["value"] = alarm["minute_period"]

        if "second" in alarm:
            status["second"]["value"] = alarm["second"]

        return status

    def _get_device_time(self, *args):
        device_time = ""
        t = pijuice.rtcAlarm.GetTime()
        if t["error"] == "NO_ERROR":
            t = t["data"]
            dt = datetime.datetime(
                t["year"], t["month"], t["day"], t["hour"], t["minute"], t["second"], tzinfo=datetime.timezone.utc
            )
            dt_fmt = "%a %Y-%m-%d %H:%M:%S"
            device_time = dt.strftime(dt_fmt) + " UTC\nLocal: " + dt.astimezone().strftime("%a %Y-%m-%d %H:%M:%S %Z")
        else:
            device_time = t["error"]

        s = pijuice.rtcAlarm.GetControlStatus()
        if s["error"] == "NO_ERROR" and s["data"]["alarm_flag"] and isinstance(t, dict) and "hour" in t:
            self.status = "Last: {}:{}:{}".format(
                str(t["hour"]).rjust(2, "0"),
                str(t["minute"]).rjust(2, "0"),
                str(t["second"]).rjust(2, "0"),
            )
        return device_time

    def _update_time(self, *args):
        if _active_tab is not self or _in_dialog:
            return
        self.device_time = self._get_device_time()
        self.time_text.set_text(self.device_time)
        self.status_text.set_text("Status: " + self.status)
        self.alarm_handle = loop.set_alarm_in(1, self._update_time)

    def _set_alarm(self, *args):
        c = self.current_config
        alarm = {"second": int(validate_value(c["second"]["value"], "int", 0, 59, None))}
        period = c["minute"].get("type") == 1
        alarm["minute_period" if period else "minute"] = int(validate_value(
            c["minute"]["value"], "int", 1 if period else 0, 60 if period else 59, None))
        alarm["hour"] = "EVERY_HOUR" if c["hour"].get("every_hour") else schedule_values(c["hour"]["value"], 0, 23, hours=True)
        weekday = c["day"].get("type") == 1
        alarm["weekday" if weekday else "day"] = ("EVERY_DAY" if c["day"].get("every_day") else
            schedule_values(c["day"]["value"], 1, 7) if weekday else int(validate_value(c["day"]["value"], "int", 1, 31, None)))
        status = pijuice.rtcAlarm.SetAlarm(alarm)
        if status.get("error") != "NO_ERROR":
            raise ValueError("Alarm was not saved: " + status.get("error", "Unknown error"))
        _saved_draft()
        _flash("Schedule saved. Wakeup enablement is unchanged.", "ok")

    def _toggle_wakeup(self, checkbox, state, *args):
        previous = self.current_config["enabled"]
        ret = pijuice.rtcAlarm.SetWakeupEnabled(state)
        if ret.get("error") != "NO_ERROR":
            checkbox.set_state(previous, do_callback=False)
            _flash("Wakeup unchanged: " + ret.get("error", "Unknown error"), "error")
            return
        self.current_config["enabled"] = state
        if isinstance(_baseline, dict):
            _baseline["enabled"] = state
        _flash("Wakeup " + ("enabled." if state else "disabled."), "ok")

    def _set_time(self, *args):
        t = datetime.datetime.now(datetime.timezone.utc)
        fields = {key: getattr(t, key) for key in ("second", "minute", "hour", "day", "month", "year")}
        fields.update(weekday=(t.weekday() + 1) % 7 + 1, subsecond=0)
        result = pijuice.rtcAlarm.SetTime(fields)
        if result.get("error") != "NO_ERROR":
            raise ValueError("Could not set RTC: " + result.get("error", "Unknown error"))
        self.time_text.set_text(self._get_device_time())
        _flash("RTC synchronised with the Pi.", "ok")

    def _set_day_type(self, rb, state, period_type):
        if state:
            self.current_config["day"]["type"] = period_type

    def _set_minute_type(self, rb, state, period_type):
        if state:
            self.current_config["minute"]["type"] = period_type

    def main(self, *args):
        self.time_text = urwid.Text(self._get_device_time())
        self.status_text = urwid.Text("Status: " + self.status)
        wakeup_cbox = urwid.Padding(
            attrmap(
                urwid.CheckBox(
                    "Wakeup enabled",
                    state=self.current_config["enabled"],
                    on_state_change=self._toggle_wakeup,
                )
            ),
            width=19,
        )
        elements = [
            urwid.Text(("title", "Wakeup Alarm")),
            urwid.Text(("muted", "Schedules use UTC. Wakeup toggle applies immediately. Hours: 0–23 or AM/PM; separate multiple hours or weekdays with ;")),
            urwid.Divider(),
            self.status_text,
            self.time_text,
            wakeup_cbox,
            urwid.Padding(
                attrmap(ActionButton("Set RTC time", on_press=self._set_time)), width=19
            ),
            urwid.Divider(),
        ]
        self.day_bgroup = []
        day_radio = attrmap(
            urwid.RadioButton(
                self.day_bgroup, "Day", on_state_change=self._set_day_type, user_data=0
            )
        )
        weekday_radio = attrmap(
            urwid.RadioButton(
                self.day_bgroup,
                "Weekday",
                on_state_change=self._set_day_type,
                user_data=1,
            )
        )
        self.day_bgroup[self.current_config["day"]["type"]].set_state(
            True, do_callback=False
        )
        day_type_row = urwid.Columns(
            [urwid.Padding(day_radio, width=10), urwid.Padding(weekday_radio, width=15)]
        )

        day_edit = urwid.Edit(
            "Day: ", edit_text=str(self.current_config["day"]["value"])
        )
        urwid.connect_signal(
            day_edit,
            "change",
            lambda x, text: self.current_config["day"].update({"value": text}),
        )
        day_checkbox = urwid.CheckBox(
            "Every day",
            state=self.current_config["day"]["every_day"],
            on_state_change=lambda x, state: self.current_config["day"].update(
                {"every_day": state}
            ),
        )
        day_value_row = urwid.Columns(
            [
                urwid.Padding(attrmap(day_edit), width=10),
                urwid.Padding(attrmap(day_checkbox), width=15),
            ]
        )

        hour_edit = urwid.Edit(
            "Hour: ", edit_text=str(self.current_config["hour"]["value"])
        )
        urwid.connect_signal(
            hour_edit,
            "change",
            lambda x, text: self.current_config["hour"].update({"value": text}),
        )
        hour_checkbox = urwid.CheckBox(
            "Every hour",
            state=self.current_config["hour"]["every_hour"],
            on_state_change=lambda x, state: self.current_config["hour"].update(
                {"every_hour": state}
            ),
        )
        hour_value_row = urwid.Columns(
            [
                urwid.Padding(attrmap(hour_edit), width=10),
                urwid.Padding(attrmap(hour_checkbox), width=15),
            ]
        )

        self.minute_bgroup = []
        minute_radio = attrmap(
            urwid.RadioButton(
                self.minute_bgroup,
                "Minute",
                on_state_change=self._set_minute_type,
                user_data=0,
            )
        )
        minute_period_radio = attrmap(
            urwid.RadioButton(
                self.minute_bgroup,
                "Minutes period",
                on_state_change=self._set_minute_type,
                user_data=1,
            )
        )
        self.minute_bgroup[self.current_config["minute"]["type"]].toggle_state()
        minute_type_row = urwid.Columns(
            [
                urwid.Padding(minute_radio, width=11),
                urwid.Padding(minute_period_radio, width=19),
            ]
        )

        minute_edit = urwid.Edit(
            "Minute: ", edit_text=str(self.current_config["minute"]["value"])
        )
        urwid.connect_signal(
            minute_edit,
            "change",
            lambda x, text: self.current_config["minute"].update({"value": text}),
        )

        second_edit = urwid.Edit(
            "Second: ", edit_text=str(self.current_config["second"]["value"])
        )
        urwid.connect_signal(
            second_edit,
            "change",
            lambda x, text: self.current_config["second"].update({"value": text}),
        )

        elements += [
            day_type_row,
            day_value_row,
            hour_value_row,
            urwid.Divider(),
            minute_type_row,
            urwid.Padding(attrmap(minute_edit), width=11),
            urwid.Padding(attrmap(second_edit), width=11),
            urwid.Divider(),
            urwid.Padding(
                attrmap(ActionButton("Set alarm", on_press=self._set_alarm)), width=13
            ),
            urwid.Padding(
                attrmap(ActionButton("Back", on_press=self._goto_main_menu)), width=13
            ),
        ]
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(elements))
        self.alarm_handle = loop.set_alarm_in(1, self._update_time)

    def _goto_main_menu(self, *args):
        loop.remove_alarm(self.alarm_handle)
        main_menu()


class SystemTaskTab(object):
    def __init__(self, *args):
        global pijuiceConfigData
        if pijuiceConfigData == None:
            pijuiceConfigData = loadPiJuiceConfig()
        self.main()

    def main(self, *args):
        global pijuiceConfigData
        elements = [urwid.Text("System Task"), urwid.Divider()]

        ## System Task ##
        if not ("system_task" in pijuiceConfigData):
            pijuiceConfigData["system_task"] = {}
        if not ("enabled" in pijuiceConfigData["system_task"]):
            pijuiceConfigData["system_task"]["enabled"] = False
        elements.extend(
            [
                attrmap(
                    urwid.CheckBox(
                        "System task enabled",
                        state=pijuiceConfigData["system_task"]["enabled"],
                        on_state_change=lambda x, state: pijuiceConfigData[
                            "system_task"
                        ].update({"enabled": state}),
                    )
                ),
                urwid.Divider(),
            ]
        )

        ## Watchdog ##
        if not ("watchdog" in pijuiceConfigData["system_task"]):
            pijuiceConfigData["system_task"]["watchdog"] = {}
        if not ("enabled" in pijuiceConfigData["system_task"]["watchdog"]):
            pijuiceConfigData["system_task"]["watchdog"]["enabled"] = False
        self.wdenabled = pijuiceConfigData["system_task"]["watchdog"]["enabled"]
        if not ("period" in pijuiceConfigData["system_task"]["watchdog"]):
            pijuiceConfigData["system_task"]["watchdog"]["period"] = 4
        if current_fw_version >= 0x15:
            ret = pijuice.power.GetWatchdog()
            self.watchdogRestoreEn = False
            if ret["error"] == "NO_ERROR":
                self.watchdogRestoreEn = ret["non_volatile"]
        wdCheckBox = attrmap(
            urwid.CheckBox(
                "Watchdog", state=self.wdenabled, on_state_change=self._toggle_wdenabled
            )
        )
        wdperiodEdit = urwid.IntEdit(
            "Expire period [minutes]: ",
            default=pijuiceConfigData["system_task"]["watchdog"]["period"],
        )
        urwid.connect_signal(wdperiodEdit, "change", self.validate_wdperiod)
        wdperiodEditItem = attrmap(wdperiodEdit)
        wdperiodTextItem = attrmap(
            urwid.Text(
                "Expire period [minutes]: "
                + str(pijuiceConfigData["system_task"]["watchdog"]["period"])
            )
        )
        wdperiodItem = wdperiodEditItem if self.wdenabled else wdperiodTextItem
        if current_fw_version >= 0x15:
            wdRestoreCheckBox = attrmap(
                urwid.CheckBox(
                    "Restore",
                    state=self.watchdogRestoreEn,
                    on_state_change=self._toggle_wdrestore,
                )
            )
            wdperiodRow = urwid.Columns(
                [(24, wdCheckBox), (23, wdperiodItem), (11, wdRestoreCheckBox)]
            )
        else:
            wdperiodRow = urwid.Columns([(24, wdCheckBox), (34, wdperiodItem)])
        elements.extend([wdperiodRow, urwid.Divider()])

        ## Wakeup on charge ##
        if not ("wakeup_on_charge" in pijuiceConfigData["system_task"]):
            pijuiceConfigData["system_task"]["wakeup_on_charge"] = {}
        if not ("enabled" in pijuiceConfigData["system_task"]["wakeup_on_charge"]):
            pijuiceConfigData["system_task"]["wakeup_on_charge"]["enabled"] = False
        self.wkupenabled = pijuiceConfigData["system_task"]["wakeup_on_charge"][
            "enabled"
        ]
        if not (
            "trigger_level" in pijuiceConfigData["system_task"]["wakeup_on_charge"]
        ):
            pijuiceConfigData["system_task"]["wakeup_on_charge"]["trigger_level"] = 50

        self.wkupOnChargeLevel = pijuiceConfigData["system_task"]["wakeup_on_charge"][
            "trigger_level"
        ]

        if current_fw_version >= 0x15:
            ret = pijuice.power.GetWakeUpOnCharge()
            self.wakeupRestoreEn = False

            if ret["error"] == "NO_ERROR":
                self.wakeupRestoreEn = ret["non_volatile"]
                if self.wakeupRestoreEn:
                    self.wkupOnChargeLevel = ret["data"]

        wkupCheckBox = attrmap(
            urwid.CheckBox(
                "Wakeup on charge",
                state=self.wkupenabled,
                on_state_change=self._toggle_wkupenabled,
            )
        )

        wkuplevelEdit = urwid.IntEdit(
            "Trigger level [%]: ", default=self.wkupOnChargeLevel
        )
        urwid.connect_signal(wkuplevelEdit, "change", self.validate_wkuplevel)
        wkuplevelEditItem = attrmap(wkuplevelEdit)
        wkuplevelTextItem = attrmap(
            urwid.Text("Trigger level [%]: " + str(self.wkupOnChargeLevel))
        )
        wkuplevelItem = wkuplevelEditItem if self.wkupenabled else wkuplevelTextItem

        if current_fw_version >= 0x15:
            wkupRestoreCheckBox = attrmap(
                urwid.CheckBox(
                    "Restore",
                    state=self.wakeupRestoreEn,
                    on_state_change=self._toggle_wkuprestore,
                )
            )
            wkuplevelRow = urwid.Columns(
                [(24, wkupCheckBox), (23, wkuplevelItem), (11, wkupRestoreCheckBox)]
            )
        else:
            wkuplevelRow = urwid.Columns([(24, wkupCheckBox), (34, wkuplevelItem)])

        elements.extend([wkuplevelRow, urwid.Divider()])

        ## Minimum charge ##
        if not ("min_charge" in pijuiceConfigData["system_task"]):
            pijuiceConfigData["system_task"]["min_charge"] = {}
        if not ("enabled" in pijuiceConfigData["system_task"]["min_charge"]):
            pijuiceConfigData["system_task"]["min_charge"]["enabled"] = False
        self.minchgenabled = pijuiceConfigData["system_task"]["min_charge"]["enabled"]
        if not ("threshold" in pijuiceConfigData["system_task"]["min_charge"]):
            pijuiceConfigData["system_task"]["min_charge"]["threshold"] = 10
        minchgCheckBox = attrmap(
            urwid.CheckBox(
                "Min charge",
                state=self.minchgenabled,
                on_state_change=self._toggle_minchgenabled,
            )
        )
        thresholdEdit = urwid.IntEdit(
            "Threshold [%]: ",
            default=pijuiceConfigData["system_task"]["min_charge"]["threshold"],
        )
        urwid.connect_signal(thresholdEdit, "change", self.validate_minchglevel)
        thresholdEditItem = attrmap(thresholdEdit)
        thresholdTextItem = attrmap(
            urwid.Text(
                "Threshold [%]: "
                + str(pijuiceConfigData["system_task"]["min_charge"]["threshold"])
            )
        )
        thresholdItem = thresholdEditItem if self.minchgenabled else thresholdTextItem
        thresholdRow = urwid.Columns([(24, minchgCheckBox), (34, thresholdItem)])
        elements.extend([thresholdRow, urwid.Divider()])

        ## Min Battery voltage ##
        if not ("min_bat_voltage" in pijuiceConfigData["system_task"]):
            pijuiceConfigData["system_task"]["min_bat_voltage"] = {}
        if not ("enabled" in pijuiceConfigData["system_task"]["min_bat_voltage"]):
            pijuiceConfigData["system_task"]["min_bat_voltage"]["enabled"] = False
        self.minbatvenabled = pijuiceConfigData["system_task"]["min_bat_voltage"][
            "enabled"
        ]
        if not ("threshold" in pijuiceConfigData["system_task"]["min_bat_voltage"]):
            pijuiceConfigData["system_task"]["min_bat_voltage"]["threshold"] = 3.3
        minbatvCheckBox = attrmap(
            urwid.CheckBox(
                "Min battery voltage",
                state=self.minbatvenabled,
                on_state_change=self._toggle_minbatvenabled,
            )
        )
        vthresholdEdit = FloatEdit(
            default=str(
                pijuiceConfigData["system_task"]["min_bat_voltage"]["threshold"]
            )
        )
        urwid.connect_signal(vthresholdEdit, "change", self.validate_minbatvlevel)
        vthresholdEditItem = attrmap(vthresholdEdit)
        vthresholdTextItem = attrmap(
            urwid.Text(
                str(pijuiceConfigData["system_task"]["min_bat_voltage"]["threshold"])
            )
        )
        vthresholdItem = (
            vthresholdEditItem if self.minbatvenabled else vthresholdTextItem
        )
        vthresholdRow = urwid.Columns([(24, minbatvCheckBox), (34, vthresholdItem)])
        elements.extend([vthresholdRow, urwid.Divider()])

        ## Software Halt Power Off ##
        if not ("ext_halt_power_off" in pijuiceConfigData["system_task"]):
            pijuiceConfigData["system_task"]["ext_halt_power_off"] = {}
        if not ("enabled" in pijuiceConfigData["system_task"]["ext_halt_power_off"]):
            pijuiceConfigData["system_task"]["ext_halt_power_off"]["enabled"] = False
        self.exthaltenabled = pijuiceConfigData["system_task"]["ext_halt_power_off"][
            "enabled"
        ]
        if not ("period" in pijuiceConfigData["system_task"]["ext_halt_power_off"]):
            pijuiceConfigData["system_task"]["ext_halt_power_off"]["period"] = 10
        exthaltCheckBox = attrmap(
            urwid.CheckBox(
                "Software Halt Power Off",
                state=self.exthaltenabled,
                on_state_change=self._toggle_exthaltenabled,
            )
        )
        periodEdit = urwid.IntEdit(
            "Delay period [seconds]: ",
            default=pijuiceConfigData["system_task"]["ext_halt_power_off"]["period"],
        )
        urwid.connect_signal(periodEdit, "change", self.validate_exthaltdelay)
        periodEditItem = attrmap(periodEdit)
        periodTextItem = attrmap(
            urwid.Text(
                "Delay period [seconds]: "
                + str(pijuiceConfigData["system_task"]["ext_halt_power_off"]["period"])
            )
        )
        periodItem = periodEditItem if self.exthaltenabled else periodTextItem
        periodRow = urwid.Columns([(24, exthaltCheckBox), (34, periodItem)])
        elements.extend([periodRow, urwid.Divider()])

        ## Footer ##
        elements.extend(
            [
                urwid.Padding(
                    attrmap(ActionButton("Refresh", on_press=self.refresh)), width=18
                ),
                urwid.Padding(
                    attrmap(ActionButton("Apply settings", on_press=savePiJuiceConfig)),
                    width=18,
                ),
                urwid.Padding(
                    attrmap(ActionButton("Back", on_press=main_menu)), width=18
                ),
            ]
        )
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(elements))

    def refresh(self, *args):
        section = _CONFIG_SECTIONS[_last_choice]
        pijuiceConfigData[section] = loadPiJuiceConfig().get(section, {})
        self.main()
        _saved_draft()
        _flash("Saved settings reloaded.", "ok")

    def _toggle_wdenabled(self, *args):
        global pijuiceConfigData
        self.wdenabled ^= True
        pijuiceConfigData["system_task"]["watchdog"]["enabled"] = self.wdenabled
        self.main()

    def _toggle_wdrestore(self, *args):
        global pijuiceConfigData
        if self.watchdogRestoreEn:
            ret = pijuice.power.SetWatchdog(0, True)
            if ret["error"] == "NO_ERROR":
                self.watchdogRestoreEn = False
        elif pijuiceConfigData["system_task"]["watchdog"]["enabled"]:
            ret = pijuice.power.SetWatchdog(
                pijuiceConfigData["system_task"]["watchdog"]["period"], True
            )
            if ret["error"] == "NO_ERROR":
                self.watchdogRestoreEn = True
        self.main()

    def _toggle_wkupenabled(self, *args):
        global pijuiceConfigData
        self.wkupenabled ^= True
        pijuiceConfigData["system_task"]["wakeup_on_charge"]["enabled"] = (
            self.wkupenabled
        )
        self.main()

    def _toggle_wkuprestore(self, *args):
        global pijuiceConfigData
        if self.wakeupRestoreEn:
            ret = pijuice.power.SetWakeUpOnCharge("DISABLED", True)
            if ret["error"] == "NO_ERROR":
                self.wakeupRestoreEn = False
        else:
            ret = pijuice.power.SetWakeUpOnCharge(
                pijuiceConfigData["system_task"]["wakeup_on_charge"]["trigger_level"],
                True,
            )
            if ret["error"] == "NO_ERROR":
                self.wakeupRestoreEn = True
        self.main()

    def _toggle_minchgenabled(self, *args):
        global pijuiceConfigData
        self.minchgenabled ^= True
        pijuiceConfigData["system_task"]["min_charge"]["enabled"] = self.minchgenabled
        self.main()

    def _toggle_minbatvenabled(self, *args):
        global pijuiceConfigData
        self.minbatvenabled ^= True
        pijuiceConfigData["system_task"]["min_bat_voltage"]["enabled"] = (
            self.minbatvenabled
        )
        self.main()

    def _toggle_exthaltenabled(self, *args):
        global pijuiceConfigData
        self.exthaltenabled ^= True
        pijuiceConfigData["system_task"]["ext_halt_power_off"]["enabled"] = (
            self.exthaltenabled
        )
        self.main()

    def validate_wdperiod(self, widget, newtext):
        pijuiceConfigData["system_task"]['watchdog']['period'] = _validate_edit(widget, newtext, 'int', 1, 65535, 'watchdog')

    def validate_wkuplevel(self, widget, newtext):
        pijuiceConfigData["system_task"]['wakeup_on_charge']['trigger_level'] = _validate_edit(widget, newtext, 'int', 0, 100, 'wakeup on charge')

    def validate_minchglevel(self, widget, newtext):
        pijuiceConfigData["system_task"]['min_charge']['threshold'] = _validate_edit(widget, newtext, 'int', 0, 100, 'min charge')

    def validate_minbatvlevel(self, widget, newtext):
        pijuiceConfigData["system_task"]['min_bat_voltage']['threshold'] = _validate_edit(widget, newtext, 'float', 0.01, 10, 'min bat voltage')

    def validate_exthaltdelay(self, widget, newtext):
        pijuiceConfigData["system_task"]['ext_halt_power_off']['period'] = _validate_edit(widget, newtext, 'int', 20, 65535, 'ext halt power off')



class SystemEventsTab(object):
    EVENTS = [
        "low_charge",
        "low_battery_voltage",
        "no_power",
        "power",
        "watchdog_reset",
        "button_power_off",
        "forced_power_off",
        "forced_sys_power_off",
        "sys_start",
        "sys_stop",
    ]
    EVTTXT = [
        "Low charge",
        "Low battery voltage",
        "No power",
        "Power present",
        "Watchdog reset",
        "Button power off",
        "Forced power off",
        "Forced sys power off",
        "System start",
        "System stop",
    ]
    FUNCTIONS1 = ["NO_FUNC"] + pijuice_sys_functions + pijuice_user_functions
    FUNCTIONS2 = ["NO_FUNC"] + pijuice_user_functions

    def __init__(self, *args):
        global pijuiceConfigData
        if pijuiceConfigData == None:
            pijuiceConfigData = loadPiJuiceConfig()
        if not ("system_events" in pijuiceConfigData):
            pijuiceConfigData["system_events"] = {}
        for event in self.EVENTS:
            if not (event in pijuiceConfigData["system_events"]):
                pijuiceConfigData["system_events"][event] = {}
            if not ("enabled" in pijuiceConfigData["system_events"][event]):
                pijuiceConfigData["system_events"][event]["enabled"] = False
            if not ("function" in pijuiceConfigData["system_events"][event]):
                pijuiceConfigData["system_events"][event]["function"] = "NO_FUNC"
        self.main()

    def main(self, *args):
        global pijuiceConfigData
        elements = [urwid.Text("System Events"), urwid.Divider()]

        for i, event in enumerate(self.EVENTS):
            eventchkbox = urwid.CheckBox(
                self.EVTTXT[i] + ":",
                state=pijuiceConfigData["system_events"][event]["enabled"],
                on_state_change=self._toggle_eventenabled,
                user_data=event,
            )
            eventitem = attrmap(eventchkbox)
            func = pijuiceConfigData["system_events"][event]["function"]
            fbutton = attrmap(
                ActionButton(func, on_press=self.set_function, user_data=[i, func])
            )
            ftext = attrmap(urwid.Text("  " + func))
            funcitem = fbutton if eventchkbox.state else ftext
            row = urwid.Columns(
                [urwid.Padding(eventitem, width=25), urwid.Padding(funcitem, width=25)]
            )
            elements.append(row)
        elements.append(urwid.Divider())

        ## Footer ##
        elements.extend(
            [
                urwid.Padding(
                    attrmap(ActionButton("Refresh", on_press=self.refresh)), width=18
                ),
                urwid.Padding(
                    attrmap(ActionButton("Apply settings", on_press=savePiJuiceConfig)),
                    width=18,
                ),
                urwid.Padding(
                    attrmap(ActionButton("Back", on_press=main_menu)), width=18
                ),
            ]
        )
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(elements))

    def refresh(self, *args):
        section = _CONFIG_SECTIONS[_last_choice]
        pijuiceConfigData[section] = loadPiJuiceConfig().get(section, {})
        self.main()
        _saved_draft()
        _flash("Saved settings reloaded.", "ok")

    def _toggle_eventenabled(self, widget, state, event):
        pijuiceConfigData["system_events"][event]["enabled"] = state
        self.main()

    def set_function(self, button, data):
        global pijuiceConfigData
        index = data[0]
        func = data[1]
        elements = [
            urwid.Text("Select function for '" + self.EVTTXT[index] + "'"),
            urwid.Divider(),
        ]
        self.functions = self.FUNCTIONS1 if index < 3 else self.FUNCTIONS2
        self.bgroup = []
        for function in self.functions:
            button = attrmap(urwid.RadioButton(self.bgroup, function))
            elements.append(button)
        self.bgroup[self.functions.index(func)].toggle_state()
        elements.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(
                        ActionButton(
                            "Back", on_press=self._on_function_chosen, user_data=index
                        )
                    ),
                    width=8,
                ),
            ]
        )
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(elements))

    def _on_function_chosen(self, button, index):
        states = [c.state for c in self.bgroup]
        pijuiceConfigData["system_events"][self.EVENTS[index]]["function"] = (
            self.functions[states.index(True)]
        )
        self.bgroup = []
        self.main()


class FileNavigator(object):
    """Filesystem picker shown over the current view; selecting a file calls
    on_pick(path). Navigates with the usual keys (CyclingListBox); Cancel/back
    aborts. ponytail: fills the bare path -- a USER_FUNC may be a full shell
    command, so any args are dropped; edit them in after picking."""

    def __init__(self, start, on_pick):
        self.on_pick = on_pick
        self.prev = main.original_widget
        self.prev_back = _current_back
        self.prev_loc = _location
        start_dir = start if os.path.isdir(start) else os.path.dirname(start)
        self.cur = "/"
        for cand in (start_dir, "/usr/local/bin", os.path.expanduser("~"), "/"):
            if cand and os.path.isdir(cand):
                self.cur = cand
                break
        self.show()

    def show(self):
        elements = [
            urwid.Text("Select script  [" + self.cur + "]"),
            urwid.Divider(),
            attrmap(ActionButton("../", on_press=self._go, user_data="..")),
        ]
        try:
            entries = sorted(
                os.scandir(self.cur), key=lambda e: (not e.is_dir(), e.name.lower())
            )
        except OSError:
            entries = []
        for e in entries:
            try:
                is_dir = e.is_dir()
            except OSError:
                is_dir = False
            elements.append(
                attrmap(
                    ActionButton(
                        e.name + ("/" if is_dir else ""),
                        on_press=self._go,
                        user_data=e.name,
                    )
                )
            )
        elements.extend(
            [
                urwid.Divider(),
                urwid.Padding(
                    attrmap(ActionButton("Cancel", on_press=self._cancel)), width=10
                ),
            ]
        )
        main.original_widget = urwid.Padding(
            CyclingListBox(urwid.SimpleFocusListWalker(elements)), left=1, right=1
        )

    def _go(self, button, name):
        path = os.path.normpath(os.path.join(self.cur, name))
        if os.path.isdir(path):
            self.cur = path
            self.show()
        else:
            _restore_view(self.prev, self.prev_back, self.prev_loc)
            self.on_pick(path)

    def _cancel(self, *args):
        _restore_view(self.prev, self.prev_back, self.prev_loc)


class ScriptEdit(urwid.Edit):
    """Edit whose Enter opens the file picker (vim normal: l -> enter)."""

    _on_browse = None

    def set_on_browse(self, cb):
        self._on_browse = cb

    def keypress(self, size, key):
        if key == "enter" and self._on_browse:
            self._on_browse()
            return None
        return super().keypress(size, key)


USER_FUNCS_TOTAL = 15


class UserScriptsTab(object):
    def __init__(self, *args):
        global pijuiceConfigData
        if pijuiceConfigData == None:
            pijuiceConfigData = loadPiJuiceConfig()
        if not ("user_functions" in pijuiceConfigData):
            pijuiceConfigData["user_functions"] = {}
        for i in range(USER_FUNCS_TOTAL):
            fkey = "USER_FUNC" + str(i + 1)
            if not (fkey in pijuiceConfigData["user_functions"]):
                pijuiceConfigData["user_functions"][fkey] = ""
        self.main()

    def main(self, *args):
        global pijuiceConfigData
        elements = [
            urwid.Text("User Scripts  (Enter on a slot to browse for a script)"),
            urwid.Divider(),
        ]

        for i in range(USER_FUNCS_TOTAL):
            flabel = "USER FUNC" + str(i + 1) + ": "
            fkey = "USER_FUNC" + str(i + 1)
            edititem = ScriptEdit(
                flabel, edit_text=pijuiceConfigData["user_functions"][fkey]
            )
            urwid.connect_signal(
                edititem,
                "change",
                self.updatetext,
                user_args=[
                    fkey,
                ],
            )
            edititem.set_on_browse(lambda e=edititem, k=fkey: self._browse(k, e))
            elements.append(attrmap(edititem))
        elements.append(urwid.Divider())

        ## Footer ##
        elements.extend(
            [
                urwid.Padding(
                    attrmap(ActionButton("Refresh", on_press=self.refresh)), width=18
                ),
                urwid.Padding(
                    attrmap(ActionButton("Apply settings", on_press=savePiJuiceConfig)),
                    width=18,
                ),
                urwid.Padding(
                    attrmap(ActionButton("Back", on_press=main_menu)), width=18
                ),
            ]
        )
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(elements))

    def updatetext(self, key, widget, text):
        global pijuiceConfigData
        pijuiceConfigData["user_functions"][key] = text

    def _browse(self, fkey, edititem):
        FileNavigator(
            pijuiceConfigData["user_functions"].get(fkey, ""),
            lambda path: edititem.set_edit_text(path),
        )

    def refresh(self, *args):
        section = _CONFIG_SECTIONS[_last_choice]
        pijuiceConfigData[section] = loadPiJuiceConfig().get(section, {})
        self.main()
        _saved_draft()
        _flash("Saved settings reloaded.", "ok")


class BatteryCareTab:
    def __init__(self):
        self.current_config = charge_policy(loadPiJuiceConfig().get("battery_management", {}))
        self.main()

    def main(self, *_args):
        report = battery_report(pijuice)
        profile = report.get("profile") or {}
        if not isinstance(profile, dict):
            profile = {}
        rows = [urwid.Text(("title", "BATTERY CARE")), urwid.Divider(),
                urwid.Text(("ok" if report["condition"] == "No battery faults reported" else "warning", report["condition"])),
                urwid.Text("Charge: %s%% · %s" % (report.get("charge", "?"), readable((report.get("status") or {}).get("battery", "UNKNOWN")))),
                urwid.Text("Temperature: %s °C" % report["temperature"] if report["temperature"] is not None else "Temperature: unavailable"),
                urwid.Text("Configured capacity: %s mAh (profile value)" % report['design_capacity'] if report['design_capacity'] else "Configured capacity: unknown"),
                urwid.Text("Profile charging limits: %s mA · %s mV" % (profile.get("chargeCurrent", "?"), profile.get("regulationVoltage", "?"))),
                urwid.Text(("muted", report["health"])), urwid.Divider(),
                attrmap(urwid.CheckBox("80% charge limit (changes immediately)", self.current_config["enabled"], on_state_change=self._toggle_limit)),
                urwid.Text("Pauses at 80%; resumes at 75%. Requires the Pi and background service to be running. Does not discharge a battery already above 80%."),
                urwid.Text(("muted", "Leave off for maximum backup runtime. Normal charging is restored on service shutdown when the limiter owns the pause.")),
                urwid.Divider(), attrmap(ActionButton("New battery / reset tracking…", on_press=self._ask_reset)),
                attrmap(ActionButton("Refresh readings", on_press=self.main)),
                attrmap(ActionButton("Back", on_press=main_menu))]
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(rows))

    def _ask_reset(self, _button):
        confirmation_dialog("Start new battery history? Cycle tracking restarts at zero and health starts learning again. Previous summaries are archived.",
                            self._reset_history, self.main, single_option=False)

    def _reset_history(self, _button, confirmed):
        if confirmed:
            import uuid
            config = loadPiJuiceConfig()
            config["battery_tracking"] = {"reset_token": str(uuid.uuid4())}
            _service_save_config(config, PiJuiceConfigDataPath)
            retry_service_reload()
        self.main()

    def _toggle_limit(self, checkbox, state):
        policy = {"enabled": state, "limit": 80, "resume": 75}
        try:
            config = loadPiJuiceConfig()
            config["battery_management"] = charge_policy(policy)
            _service_save_config(config, PiJuiceConfigDataPath)
        except Exception as exc:
            checkbox.set_state(self.current_config["enabled"], do_callback=False)
            _flash("Could not save charge limit: %s" % exc, "error")
            return
        self.current_config = policy
        pijuiceConfigData["battery_management"] = policy.copy()
        _saved_draft()
        retry_service_reload()


class SettingsTab(object):
    def __init__(self, *args):
        global pijuiceConfigData
        if pijuiceConfigData is None:
            pijuiceConfigData = loadPiJuiceConfig()
        self.main()

    def main(self, *args):
        global pijuiceConfigData
        cli = pijuiceConfigData.setdefault("cli_settings", {})
        elements = [
            urwid.Text("Settings"),
            urwid.Divider(),
            attrmap(
                urwid.CheckBox(
                    "Enable vim keybindings",
                    state=cli.get("vim_keys", False),
                    on_state_change=self._toggle_vim,
                )
            ),
            urwid.Divider(),
            urwid.Text(("line", "Changes apply immediately.")),
            urwid.Divider(),
            urwid.Padding(attrmap(ActionButton("Back", on_press=main_menu)), width=18),
        ]
        main.original_widget = CyclingListBox(urwid.SimpleFocusListWalker(elements))

    def _toggle_vim(self, checkbox, state):
        # Immediate-apply: toggling already activates it live, and it's CLI-only
        # JSON the service never reads, so persist now — no Apply, no dirty-warning.
        global VIM_ENABLED, pijuiceConfigData
        pijuiceConfigData.setdefault("cli_settings", {})["vim_keys"] = state
        previous = VIM_ENABLED
        if save_cli_config_quiet():
            VIM_ENABLED = state
            _saved_draft()
            _flash("Terminal preference saved.", "ok")
        else:
            pijuiceConfigData["cli_settings"]["vim_keys"] = previous
            checkbox.set_state(previous, do_callback=False)
        _update_title()


class CyclingListBox(urwid.ListBox):
    """ListBox that wraps focus around at the top/bottom edges (README TODO).

    urwid's ListBox.keypress returns the motion key unchanged when it can't move
    (i.e. focus is already at an edge). We catch that and jump to the first/last
    *selectable* row, skipping dividers and static text.
    """

    def keypress(self, size, key):
        # gg/G (mapped to home/end) land on the first/last *selectable* row so
        # the highlight never lands on the non-selectable title.
        if key == "home":
            self._focus_edge(from_top=True)
            return None
        if key == "end":
            self._focus_edge(from_top=False)
            return None
        result = super().keypress(size, key)
        if result == "down":
            self._focus_edge(from_top=True)
            return None
        if result == "up":
            self._focus_edge(from_top=False)
            return None
        return result

    def _focus_edge(self, from_top):
        count = len(self.body)
        order = range(count) if from_top else range(count - 1, -1, -1)
        for i in order:
            if self.body[i].selectable():
                self.set_focus(i)
                return


def schedule_values(text, lo, hi, hours=False):
    values = []
    for token in str(text).upper().split(";"):
        token = token.strip()
        if hours and token.endswith(("AM", "PM")):
            hour = int(validate_value(token[:-2].strip(), "int", 1, 12, None))
            value = hour % 12 + (12 if token.endswith("PM") else 0)
        else:
            value = int(validate_value(token, "int", lo, hi, None))
        values.append(value)
    return values[0] if len(values) == 1 else ";".join(str(v) for v in sorted(set(values)))


def readable(value):
    aliases = {"PRESENT": "Connected", "NOT_PRESENT": "Not connected", "NORMAL": "On battery",
               "CHARGING_FROM_IN": "Charging via USB", "CHARGING_FROM_5V_IO": "Charging via GPIO",
               "NO_FUNC": "No action", "USER_LED": "Custom colour", "CHARGE_STATUS": "Charge status"}
    return aliases.get(value, str(value).replace("_", " ").capitalize())


MENU_HELP = {
    "Status": "Live battery, power and health", "General": "Power inputs and board settings",
    "Buttons": "Press and hold actions", "LEDs": "Charge indicators and custom colours",
    "Battery care": "Battery condition and an 80% charge limit",
    "Battery profile": "Battery type and charging limits", "IO": "Pins, pull resistors and PWM",
    "Wakeup Alarm": "UTC schedules and wakeup", "Firmware": "Installed version and updates",
    "System Task": "Shutdown and watchdog rules", "System Events": "Actions triggered by power events",
    "User Scripts": "Choose scripts for custom actions", "Settings": "Terminal preferences", "Exit": "Close PiJuice CLI",
}


def menu(title, choices):
    body = [urwid.Text(("muted", "Choose a section. Drafts stay here until you apply or discard them.")), urwid.Divider()]
    for choice in choices:
        if choice:
            button = ActionButton(choice)
            urwid.connect_signal(button, "click", item_chosen, user_args=[choice])
            description = MENU_HELP.get(choice, "")
            if choice in _drafts:
                description = "Unsaved draft · " + description
            body.append(urwid.Columns([(23, attrmap(button)), urwid.Text(("warning" if choice in _drafts else "muted", description))], dividechars=2))
        else:
            body.append(urwid.Divider())
    return CyclingListBox(urwid.SimpleFocusListWalker(body))


def item_chosen(choice, button=None):
    global _location, _dirty, _last_choice, _active_tab, _errors, _baseline
    if choice == "Exit":
        return exit_program()
    _location = _last_choice = choice
    _dirty = False
    _errors = {}
    try:
        if choice in _drafts:
            _active_tab, _baseline, _errors = _drafts.pop(choice)
            _active_tab.main()
            _dirty = True
        else:
            if choice not in ("User Scripts", "Settings", "System Events"):
                _InitPiJuiceInterface()
            _active_tab = menu_mapping[choice]()
            _baseline = _snapshot()
            _dirty = False
        _render_header()
    except Exception as exc:
        _active_tab = None
        main_menu()
        _flash("Could not open %s: %s. Check the connection and try again." % (choice, exc), "error")


def main_menu(*args):
    global _location, _active_tab, _dirty, _errors
    if _active_tab is not None and _dirty and hasattr(_active_tab, "main"):
        _drafts[_last_choice] = (_active_tab, copy.deepcopy(_baseline), _errors.copy())
    _active_tab = None
    _dirty = False
    _errors = {}
    _location = "Settings"
    m = menu("PiJuice HAT Configuration", choices)
    if _last_choice is not None:  # land on the item we came from
        for i, w in enumerate(m.body):
            if any(isinstance(b, urwid.Button) and b.label == _last_choice for b in _walk_widgets(w)):
                m.set_focus(i)
                break
    main.original_widget = m


def exit_program(button=None):
    if _dirty or _drafts:
        previous = (main.original_widget, _current_back, _location)
        confirmation_dialog("Discard all unsaved drafts and exit? Saved settings will be kept.",
                            next=exit_cli, nextno=lambda *_a: _restore_view(*previous), single_option=False)
    else:
        raise urwid.ExitMainLoop()


def attrmap(w):
    if isinstance(w, (urwid.Edit, urwid.CheckBox)):
        urwid.connect_signal(w, "change", _mark_dirty)
    style = "field" if isinstance(w, urwid.Edit) else "button"
    if isinstance(w, urwid.Button):
        if w.label.lower().startswith(("apply", "set alarm")):
            style = "action"
        elif w.label.lower().startswith(("flash", "reset")):
            style = "warning"
    return urwid.AttrMap(w, style, focus_map="button_focus")


def loadPiJuiceConfig():
    return _service_load_config(PiJuiceConfigDataPath)


def savePiJuiceConfig(*args):
    if _errors:
        _flash(next(iter(_errors.values())), "error")
        return
    section = _CONFIG_SECTIONS.get(_last_choice)
    if not section:
        _flash("Open a settings section before saving.", "warning")
        return
    try:
        config = loadPiJuiceConfig()
        config[section] = copy.deepcopy(pijuiceConfigData.get(section, {}))
        if section == "user_functions":
            for key, value in config[section].items():
                if value and (not os.path.isabs(value) or not os.path.isfile(value)):
                    raise ValueError("%s: choose an existing script using its full path." % key)
        _service_save_config(config, PiJuiceConfigDataPath)
    except Exception as exc:
        _flash("Could not save: %s. Your edits are kept; retry with F5." % exc, "error")
        return
    _saved_draft()
    retry_service_reload()


def retry_service_reload(*args):
    try:
        result = notify_service()
    except Exception:
        result = -1
    _flash("Settings saved." if result == 0 else "Saved, but the service did not reload. F8 retries the reload.",
           "ok" if result == 0 else "warning")


def save_cli_config_quiet():
    try:
        config = loadPiJuiceConfig()
        config["cli_settings"] = copy.deepcopy(pijuiceConfigData.get("cli_settings", {}))
        _service_save_config(config, PiJuiceConfigDataPath)
        return True
    except Exception as exc:
        _flash("Could not save terminal preference: %s" % exc, "error")
        return False


def notify_service(*args):
    # Delegate to the shared, shell-free SIGHUP notifier (no os.system).
    return _service_notify_service(PID_FILE)


menu_mapping = {
    "Status": StatusTab,
    "General": GeneralTab,
    "Buttons": ButtonsTab,
    "LEDs": LEDTab,
    "Battery care": BatteryCareTab,
    "Battery profile": BatteryProfileTab,
    "IO": IOTab,
    "Wakeup Alarm": WakeupAlarmTab,
    "Firmware": FirmwareTab,
    "System Task": SystemTaskTab,
    "System Events": SystemEventsTab,
    "User Scripts": UserScriptsTab,
    "Settings": SettingsTab,
    "Exit": exit_program,
}

# Use list of entries to set order
choices = [
    "Status",
    "General",
    "Buttons",
    "LEDs",
    "Battery care",
    "Battery profile",
    "IO",
    "Wakeup Alarm",
    "Firmware",
    "",
    "System Task",
    "System Events",
    "User Scripts",
    "",
    "Settings",
    "Exit",
]

# ── navigation state ─────────────────────────────────────────────────────────
# Each view still builds a "Back"/"Cancel" button as before, but _ContentArea
# hoists it out of the body into a clickable header. So the button never shows in
# the body; it fires from go_back() -- the header "back", the Esc key, and a
# 'left'/'h' that goes unhandled (focus at the left edge) all call it. The action
# may navigate OR commit a chooser selection (behaviour unchanged). New views get
# this for free just by keeping a Back/Cancel button.
_drafts = {}
_active_tab = None
_baseline = None
_errors = {}
_notice = ("muted", "Ready")
_CONFIG_SECTIONS = {"System Task": "system_task", "System Events": "system_events", "User Scripts": "user_functions"}


def _snapshot():
    if isinstance(_active_tab, BatteryProfileTab):
        return copy.deepcopy({key: getattr(_active_tab, key, None) for key in (
            "profile_name", "profile_data", "ext_profile_data", "temp_sense_profile_idx",
            "rsoc_estimation_profile_idx", "chemistries_idx", "custom_values")})
    section = _CONFIG_SECTIONS.get(_last_choice)
    return copy.deepcopy(pijuiceConfigData.get(section, {}) if section else getattr(_active_tab, "current_config", None))


def _saved_draft():
    global _dirty, _baseline
    _dirty = False
    _errors.clear()
    _drafts.pop(_last_choice, None)
    _baseline = _snapshot()
    _render_header()


def _flash(message, style="muted"):
    global _notice
    _notice = (style, message)
    _render_header()


def discard_draft(*args):
    if _active_tab is None:
        return
    choice = _last_choice
    def discard(*_args):
        global _dirty, _active_tab, pijuiceConfigData
        if hasattr(_active_tab, "alarm_handle") and loop:
            loop.remove_alarm(_active_tab.alarm_handle)
        section = _CONFIG_SECTIONS.get(choice)
        if section:
            pijuiceConfigData[section] = loadPiJuiceConfig().get(section, {})
        _dirty = False
        _active_tab = None
        _drafts.pop(choice, None)
        item_chosen(choice)
        _flash("Draft discarded.")
    previous = (main.original_widget, _current_back, _location)
    confirmation_dialog("Discard this section's edits and reload saved settings?", next=discard,
                        nextno=lambda *_a: _restore_view(*previous), single_option=False)


def _walk_widgets(widget):
    yield widget
    if isinstance(widget, (urwid.Edit, urwid.Button, urwid.CheckBox)):
        return
    if isinstance(widget, (urwid.Pile, urwid.Columns)):
        for child, _options in widget.contents:
            yield from _walk_widgets(child)
    elif isinstance(widget, urwid.ListBox):
        for child in widget.body:
            yield from _walk_widgets(child)
    elif hasattr(widget, "original_widget"):
        yield from _walk_widgets(widget.original_widget)


def apply_draft():
    if _in_dialog:
        return
    if _errors:
        _flash(next(iter(_errors.values())), "error")
        return
    for w in _walk_widgets(main.original_widget):
        if isinstance(w, urwid.Button) and w.label.lower().startswith(("apply", "set alarm")):
            urwid.emit_signal(w, "click", w)
            return
    if _active_tab is not None and hasattr(_active_tab, "main"):
        _active_tab.main()
        _flash("Returned to section actions. Press F5 to apply.", "warning")


_dirty = False  # unapplied text edits in the current view
_current_back = None  # the hoisted Back/Cancel button of the current view
_location = ""  # breadcrumb shown in the header
_last_choice = None  # last main-menu item entered (restore focus on back)
_suppress_hoist = False  # re-render a view without re-hoisting (dirty cancel)
_in_dialog = False  # a confirmation_dialog is showing
_dialog_cancel = None  # Esc action while a dialog is up
frame = None  # set once the UI is built
linebox = None
main = None
loop = None

VIM_ENABLED = bool(loadPiJuiceConfig().get("cli_settings", {}).get("vim_keys", False))
_vim_mode = "normal"  # 'normal' | 'insert' (only meaningful when VIM_ENABLED)
_vim_pending_g = False
_VIM_MOTIONS = {
    "j": "down",
    "k": "up",
    "h": "left",
    "l": "right",
    "G": "end",
    "ctrl f": "page down",
    "ctrl b": "page up",
}


def _mark_dirty(*args):
    global _dirty
    _dirty = True
    _render_header()


def _focus_is_editable(widget):
    """Descend the focus chain; True if the focused leaf is a text Edit."""
    seen = set()
    for _ in range(50):
        if isinstance(widget, urwid.Edit):
            return True
        if id(widget) in seen:
            break
        seen.add(id(widget))
        child = None
        for attr in ("focus", "original_widget"):
            candidate = getattr(widget, attr, None)
            if candidate is not None and candidate is not widget:
                child = candidate
                break
        if child is None:
            break
        widget = child
    return False


def _rows_container(widget):
    """Unwrap decorations until a Pile/ListBox; return (kind, row-list)."""
    seen = set()
    while widget is not None and id(widget) not in seen:
        seen.add(id(widget))
        if isinstance(widget, urwid.Pile):
            return "pile", widget.contents
        if isinstance(widget, urwid.ListBox):
            return "walker", widget.body
        widget = getattr(widget, "original_widget", None)
    return None, None


def _hoist_back(widget):
    """Remove the Back/Cancel row from a freshly built view and return its button,
    so the frame can drive it from the header / nav keys instead of the body."""
    kind, rows = _rows_container(widget)
    if rows is None:
        return None
    for i, entry in enumerate(rows):
        row = entry[0] if kind == "pile" else entry
        leaf = getattr(row, "base_widget", row)
        if isinstance(leaf, urwid.Button) and leaf.label in ("Back", "Cancel"):
            del rows[i]
            return leaf
    return None


class _ContentArea(urwid.Padding):
    """Padding whose content swap hoists the view's Back button into the header."""

    # urwid >= 2.4 turned WidgetDecoration._get/_set_original_widget into
    # deprecation shims that delegate back to the property; building the
    # property from them recurses forever (RecursionError on first keypress).
    @property
    def original_widget(self):
        return self._original_widget

    @original_widget.setter
    def original_widget(self, widget):
        if isinstance(widget, urwid.Filler) and isinstance(widget.original_widget, urwid.Pile):
            widget = CyclingListBox(urwid.SimpleFocusListWalker([row for row, _options in widget.original_widget.contents]))
        back = None if _suppress_hoist else _hoist_back(widget)
        self._original_widget = widget
        self._invalidate()
        if not _suppress_hoist:
            _on_view_changed(back)


def _on_view_changed(back):
    global _current_back
    _current_back = back
    _render_header()


class _BareButton(urwid.Button):
    """Button without the global [ ] chrome -- used for the header back control."""

    button_left = urwid.Text("")
    button_right = urwid.Text("")


def _render_header():
    if frame is None:
        return
    cols = []
    if _current_back is not None:
        back_btn = _BareButton("← back", on_press=go_back)  # arrow cues left/h
        cols.append((10, urwid.AttrMap(back_btn, "nav", focus_map="button_focus")))
    title = _location + ("  • Unsaved" if _dirty else "")
    if _drafts:
        title += "  • %d draft%s" % (len(_drafts), "s" if len(_drafts) != 1 else "")
    cols.append(urwid.AttrMap(urwid.Text(title), "title"))
    frame.header = urwid.Pile([urwid.Columns(cols, dividechars=1), urwid.Divider()])
    hint = "↑↓/Tab Move  Enter Select  Esc Back  F5 Apply  F6 Discard  F8 Reload  F10 Quit"
    if VIM_ENABLED:
        hint = ("Vim INSERT: Esc Normal  " if _vim_mode == "insert" else "Vim: j/k Move  i Edit  ") + hint
    frame.footer = urwid.Pile([urwid.Divider(), urwid.Text(_notice), urwid.Text(("muted", hint))])


def _update_title():
    if linebox is None:
        return
    if VIM_ENABLED and _vim_mode == "insert":
        linebox.set_title("PiJuice CLI  -- INSERT --")
    else:
        linebox.set_title("PiJuice CLI")


def go_back(*args):
    """Back within a section preserves its draft; returning to the menu caches it."""
    if _current_back is not None:
        urwid.emit_signal(_current_back, "click", _current_back)


def _do_back(btn):
    if btn is not None:
        urwid.emit_signal(btn, "click", btn)


def _back_or_exit(*args):
    """Esc: cancel a dialog, else go back, else (at root) quit the CLI."""
    if _in_dialog:
        if _dialog_cancel:
            _dialog_cancel()
        return
    if _current_back is None:
        exit_program()
    else:
        go_back()


def _restore_view(widget, back, loc):
    global _suppress_hoist, _current_back, _location
    _suppress_hoist = True
    main.original_widget = widget
    _suppress_hoist = False
    _current_back = back
    _location = loc
    _render_header()


def vim_translate(keys, mode, editable, pending_g):
    """Pure key mapping (no urwid). Returns (out_keys, mode, pending_g, want_insert).
    h/l/j/k -> left/right/down/up so focus moves (incl. across columns); back is
    triggered separately when 'left' goes unhandled (focus at the left edge)."""
    out, want_insert = [], False
    for key in keys:
        if mode == "insert":
            if key == "esc":
                mode = "normal"
            else:
                out.append(key)
            continue
        # normal mode
        if key == "g":
            if pending_g:
                out.append("home")
                pending_g = False
            else:
                pending_g = True
            continue
        pending_g = False
        if key in ("i", "a") and editable:
            want_insert = True
            mode = "insert"
        elif key in _VIM_MOTIONS:
            out.append(_VIM_MOTIONS[key])
        elif editable and len(key) == 1 and key.isprintable():
            pass  # swallow: normal-mode keys must not get typed into the field
        else:
            out.append(key)
    return out, mode, pending_g, want_insert


def input_filter(keys, raw):
    global _vim_mode, _vim_pending_g
    editable = _focus_is_editable(loop.widget)
    out = []
    for key in keys:
        if key in ("f5", "f6", "f8", "f10"):
            if _in_dialog:
                continue
            {"f5": apply_draft, "f6": discard_draft, "f8": retry_service_reload, "f10": exit_program}[key]()
            continue
        if key == "tab":
            key = "down"
        elif key == "shift tab":
            key = "up"
        # Esc only special in vim insert mode (-> normal); otherwise it falls
        # through to unhandled_input, which does cancel/back/quit.
        if key == "esc" and VIM_ENABLED and _vim_mode == "insert":
            _vim_mode = "normal"
            _update_title()
            continue
        if not VIM_ENABLED:
            out.append(key)
            continue
        mapped, _vim_mode, _vim_pending_g, want_insert = vim_translate(
            [key], _vim_mode, editable, _vim_pending_g
        )
        if want_insert:
            _update_title()
        out.extend(mapped)
    if loop:
        def track(_loop, _data):
            global _dirty
            if _active_tab is not None and not _in_dialog:
                _dirty = _snapshot() != _baseline or bool(_errors)
                _render_header()
        loop.set_alarm_in(0, track)
    return out


def unhandled_input(key):
    # Reached only when no widget consumed the key. 'left' here means focus is at
    # the left edge (vertical list, or leftmost column) -> go back. Esc -> back/quit.
    if key == "left":
        go_back()
    elif key == "esc":
        _back_or_exit()


def _selftest():
    assert vim_translate(["j"], "normal", False, False)[0] == ["down"]
    assert vim_translate(["k"], "normal", False, False)[0] == ["up"]
    assert vim_translate(["h"], "normal", False, False)[0] == [
        "left"
    ]  # move/back-at-edge
    assert vim_translate(["l"], "normal", False, False)[0] == [
        "right"
    ]  # move right (columns)
    out, m, p, ins = vim_translate(["g"], "normal", False, False)
    assert p is True and out == []
    assert vim_translate(["g"], "normal", False, True)[0] == ["home"]
    out, m, p, ins = vim_translate(["i"], "normal", True, False)
    assert ins is True and m == "insert" and out == []
    assert vim_translate(["x"], "normal", True, False)[0] == []  # swallowed
    assert vim_translate(["x"], "insert", True, False)[0] == ["x"]  # typed
    out, m, p, ins = vim_translate(["esc"], "insert", True, False)
    assert m == "normal" and out == []
    assert vim_translate(["enter"], "normal", False, False)[0] == ["enter"]  # select
    # _hoist_back removes the Back/Cancel row and returns its button.
    bb = ActionButton("Back")
    pile = urwid.Pile(
        [urwid.Text("t"), urwid.Divider(), urwid.Padding(attrmap(bb), width=8)]
    )
    assert _hoist_back(urwid.Filler(pile)) is bb and len(pile.contents) == 2
    lb = CyclingListBox(
        urwid.SimpleFocusListWalker([urwid.Text("t"), attrmap(ActionButton("Cancel"))])
    )
    assert isinstance(_hoist_back(lb), urwid.Button) and len(lb.body) == 1
    assert _hoist_back(urwid.Filler(urwid.Pile([urwid.Text("x")]))) is None
    print("selftest OK")


# Palette: cohesive cyan accent, high-contrast focus -- the single place that
# styles the whole CLI. Backgrounds are "" so they inherit the terminal's own
# light/dark scheme.
PALETTE = [
    ("reversed", "standout", ""),
    ("title", "light cyan,bold", ""),
    ("nav", "light cyan", ""),
    ("button", "default", ""),
    ("button_focus", "black", "light cyan"),
    ("error", "light red,bold", ""),
    ("ok", "light green,bold", ""),
    ("line", "dark cyan", ""),
    ("muted", "default", ""),
    ("value", "default,bold", ""),
    ("warning", "yellow,bold", ""),
    ("field", "default", ""),
    ("action", "light cyan,bold", ""),
]


def exit_cli(*args):
    raise urwid.ExitMainLoop()


class ResponsiveScreen(urwid.WidgetWrap):
    def render(self, size, focus=False):
        if size[0] < 64 or size[1] < 12:
            return urwid.Filler(urwid.Text("PiJuice CLI\nResize to at least 64 columns × 12 rows.\nF10: quit", align="center")).render(size)
        return super().render(size, focus)

    def keypress(self, size, key):
        if size[0] < 64 or size[1] < 12:
            if key in ("f10", "esc"):
                exit_program()
            return None
        return super().keypress(size, key)


def _build_and_run():
    global main, frame, linebox, loop, pijuiceConfigData, _location
    nolock = False
    lock_file = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        nolock = True

    if nolock:
        elements = [
            urwid.Padding(
                urwid.Text(
                    "Another instance of PiJuice Settings is already running",
                    align="center",
                )
            ),
            urwid.Divider(),
        ]
        elements.append(
            urwid.Padding(
                attrmap(ActionButton("OK", on_press=exit_cli)), width=6, align="center"
            )
        )
        main = _ContentArea(urwid.Filler(urwid.Pile(elements)), left=2, right=2)
    else:
        pijuiceConfigData = loadPiJuiceConfig()
        main = _ContentArea(menu("PiJuice HAT Configuration", choices), left=2, right=2)
        _location = "PiJuice HAT Configuration"

    frame = urwid.Frame(body=main)
    linebox = urwid.LineBox(frame, title="PiJuice CLI")
    top = urwid.Overlay(
        linebox,
        urwid.SolidFill(" "),
        align="center",
        width=("relative", 96),
        min_width=64,
        valign="middle",
        height=("relative", 96),
        min_height=12,
    )
    _render_header()

    loop = urwid.MainLoop(
        ResponsiveScreen(top), palette=([(name, "standout" if name == "button_focus" else "default", "default") for name, *_rest in PALETTE]
                      if "NO_COLOR" in os.environ else PALETTE),
        input_filter=input_filter, unhandled_input=unhandled_input
    )
    loop.run()


# Importing the module must not launch the TUI or grab the lock -- only running
# it as a script does.
if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    _build_and_run()
