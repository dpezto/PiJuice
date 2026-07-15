## Unreleased

### Software
pijuice-gui; urgency=low

* src/pijuice_gtk.py:
    - Rebuilt on libadwaita: HIG-consistent rows and **automatic light/dark
      theme following the system** (via `Adw.Application`/`StyleManager`)
    - Follow the system dark theme on Raspberry Pi OS too, which signals dark via
      the GTK theme name (`PiXnoir`) rather than the freedesktop color-scheme
      portal: read the theme name before Adw masks it and set the scheme when the
      portal has no preference
    - User Scripts: a file-browser button per row (`Gtk.FileChooserNative`),
      mirroring the CLI's file navigation
    - Hide `Adw.EntryRow`'s edit affordance (its `document-edit-symbolic` icon is
      absent from Pi icon themes, so it rendered as a broken-image glyph)
    - Every tab ported to `Adw.PreferencesPage` groups/rows; the per-tab grid,
      touch-sizing CSS and duplicated Apply/Refresh/`_saved`/combo-index helpers
      collapsed into shared base-view builders
    - `--selftest` builds the window and asserts every page is present
    - New dependency: `gir1.2-adw-1`

* src/pijuice_tray.py:
    - Removed the orphaned SIGUSR1/SIGUSR2 "grey out Settings" handlers and the
      world-writable (0666) PID file — nothing signalled the tray

pijuice-base; urgency=low

* src/pijuice_cli.py:
    - Fix crash (RecursionError) on the first keypress with urwid >= 2.4:
      `_ContentArea.original_widget` was built from urwid's deprecated
      `_get/_set_original_widget` shims, which now delegate back to the property
    - Route config load/save and the service SIGHUP notify through
      `pijuice_service` (single source of truth); drop the duplicated path/bus/
      address constants
    - Security: the notify path no longer shells out via `os.system`
    - Importing the module no longer launches the TUI or grabs the lock
    - Silence urwid's `user_arg` DeprecationWarning: the three remaining
      `connect_signal` calls now pass `user_args=[...]` (which prepends), with
      the callbacks' extra parameter moved to the front to match

* New: expose the battery to system monitors (btop, upower, desktops)
    - kernel/pijuice_power: a DKMS module registering a virtual `power_supply`
      named `pijuice` in `/sys/class/power_supply/`, with writable sysfs attrs.
      Built/loaded on install, rebuilt on kernel updates, autoloaded at boot
      (`/etc/modules-load.d`). Adds `dkms` + `raspberrypi-kernel-headers` deps
    - src/pijuice_sys.py: the service pushes charge/status/voltage/current/temp
      into the module each 5s poll (`_UpdatePowerSupply`)
    - data/99-pijuice-power.rules: udev rule granting the pijuice service group
      write access to the module's otherwise root-only sysfs attrs

## Version 1.2
Added packages to both Raspbian Jessie and Stretch

### Software
pijuice-gui (1.2-1) unstable; urgency=low

* src/pijuice_gui.py:
     - Fix layout for parameters labels on IO tab

## Version 1.1

### Software
pijuice-base (1.1-1) unstable; urgency=low

* pijuice.py:
    - Function for getting versions (OS, Firmware, Software)

* src/pijuice_sys.py:
    - Refactored GetFirmvareVersion to GetFirmwareVersion #34

pijuice-gui (1.1-1) unstable; urgency=low

* data/images/:
    - New icon for desktop menu

* src/pijuice_gui.py:
    - Use "clam" theme for GUI
    - Apply button in main window for saving settings
    - Adjust minimal window sizes
    - Change title for main settings window
    - Fix typo "Temerature sense" in Battery configuration tab
    - "Apply" button now applies settings from fields that use Enter key to update value
    - Various layout fixes for values to fit their elements

* src/pijuice_tray.py:
    - Add versions info to About menu in tray
    - Make tray menu entry Settings launch pijuice_gui in separate process

### Firmware
  
* data/firmware/PiJuice-V1.1_2018_01_15.elf.binary:
    - Wakeup on charge updated to be activated only if power source is present.
    - Further this enables wakeup after plugged if this parameter is set to 0.
    - Button wakeup functions power off and power off can now be assigned to arbitrary button events. Removed constrain to be assigned only to long_press2 for power off, and single_press for power on.
    - Added reset function that can be assigned to some of buttons and button events.
    - Added no battery turn on configuration.
    - Now it can be set whether or not user wants to turn on 5V rail as soon as power input is connected and there is no battery.
    - Added configuration for 2 IO ports. They can be set to analog input, digital input, digital output and pwm output.

## Version 1.0
pijuice (1.0) initial release
