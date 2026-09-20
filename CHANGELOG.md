## Version 1.10

### Software
pijuice-gui; urgency=low

* src/pijuice_gtk.py:
    - Status page: the power-switch dropdown is no longer a draft, so changing it
      without pressing Set cannot block closing the window
    - Battery page: no toast storm when the HAT disconnects; the health poll
      pauses and the condition row shows the error instead
    - Confirmation dialogs cannot stack (double-click on reset/close no longer
      queues two actions); the close-while-writing warning is a toast, not the
      window title
    - Wakeup: refreshing the alarm resets every field, so typed text never
      survives as if the device had reported it; firmware pre-check is a read
      (no page lock) and the post-flash reconnect retries for up to 30 s
    - Apply handlers catch every exception (draft kept, message shown)
    - IO page: replaced parameter rows are dropped from draft tracking (leak)
    - Shared `readable`, `schedule_values`, alarm parsing and version packing
      now come from `pijuice_service` (one copy for CLI and GUI)
    - Dead code removed: `_int`, base title/slug defaults, a hand copy of
      `immediate()` in the battery view
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
    - All HAT access goes through `pijuice_service.PiJuiceService`, like the
      GUI and tray; the 59 raw library calls and 40 hand-rolled error checks are
      gone, and a failed device call always lands on a message, not a traceback
      (checkbox/radio callbacks included)
    - Fix: a changed I2C address is now persisted to `board.general.i2c_addr`
      (it was silently dropped, so the next launch reconnected on the old one)
    - Fix: firmware update `NameError` on the "unknown address" path; the
      post-flash wait is bounded (30 s) and the flash runs on the service worker
    - Fix: battery profile apply stops at the first failed write instead of
      stacking dialogs and writing the profile anyway; RSoC estimation is only
      written on firmware >= 1.3
    - Fix: the RTC clock keeps ticking behind a dialog; stale IO field errors no
      longer block Apply after a mode change; the LED function label follows a
      colour edit; a missing `/run/pijuice` gives a message, not a traceback
    - Dead code removed (unused `_do_back`, `_clear_text_edits`, vendored
      NumEdit/FloatEdit options, unreachable Pile hoisting path); the CLI-only
      `notify_service`/save copies are replaced by `service.save_section`

* pijuice_service.py:
    - Wrappers for the remaining CLI-only calls (watchdog, wakeup-on-charge,
      run pin, power inputs/regulator, ID EEPROM, I2C address, custom profiles)
    - Shared helpers: `readable`, `schedule_values`, `alarm_fields`,
      `pack_version`/`version_to_str`, `firmware_error`, `rtc_fields_now`
    - Removed unused API (`set_led_color`, `submit_method`, context manager,
      `reload_config`, `save_and_notify`, `get_led_state`,
      `get_battery_current`, `clear_alarm_flag`, `LED_FUNCTIONS`)

* src/pijuice_sys.py:
    - Re-arms at start what the HAT forgets after a full battery drain (upstream
      #1035, #760, #853): the RTC time from the Pi clock, the alarm and wakeup
      enable the UIs saved to `wakeup_alarm` in the config, and wakeup-on-charge
      from System Task
    - Logs through `logging` with journald `<N>` priority prefixes
    - One `GetStatus` per second; battery tracking, the charge limit and the
      power_supply feed run every 5 s regardless of the System Task switch (the
      feed used to stop when System Task was off)
    - Feeds `charge_full_design` (profile) and `charge_full` (learned capacity
      once battery history has a qualifying discharge); writes `present = 0` on
      stop

* pijuice_power 1.3: `cycle_count`, `health` and `time_to_empty_now`, fed by
  the daemon from battery history and the HAT fault flags

* CLI navigation and window:
    - Menus open and close like a menu bar: Right (vim `l`) opens the focused
      menu item, Left/Esc/Backspace/`q` (vim `h`) close it; Enter presses,
      toggles or picks; Tab walks a row's fields before moving down; `q` at the
      main menu and F10/`Q` quit; `?` shows a keys table. Entries that open a
      screen are drawn `Name ›`, the header is a breadcrumb (`PiJuice HAT
      Configuration › Buttons › Button SW1`), focus lands on the first usable
      row of each screen and back returns to the row you left, and the focus
      chain is read from the frame (Right/l, vim insert and q-in-a-field
      detection did not work in the real terminal)
    - Vim keybindings: `0`/`^`/`$` first/last field of a row, `b`/`w`/`e`
      previous/next field, `ctrl-u`/`ctrl-d`; inside a field in NORMAL mode the
      same keys move the cursor by word, `x` deletes, `I`/`A` enter INSERT at
      the start/end, and no other key edits the field
    - Footer notices clear themselves after a few seconds (errors after ten);
      the footer shows only the notice, `? keys` and the vim mode; the "← back"
      header button and the key hint line are gone
    - The window is sized to its content (raspi-config style, up to 78
      columns) instead of filling the terminal

* Cosmetics, both apps:
    - Button and event functions are shown by what they do ("Power on",
      "Halt, then power off") with a one-line description of each; the
      README has the reference table. Button events read "Single press",
      "Long press 1"; the timing field says what it times
    - User Scripts slots take an optional display name (`user_function_names`),
      used wherever the slot is offered; unnamed slots read "User script n"
    - CLI: styled titles on every screen, subtitles where a hint helps,
      Buttons/events/User Scripts laid out in columns, footer fits 78 columns;
      an unknown function name from the firmware (upstream #998) selects "No
      action" instead of crashing

* Firmware update:
    - `pijuiceboot` takes the I2C bus and bootloader address as optional
      arguments (were hardcoded to `/dev/i2c-1` and `0x41`); dead UART/readout
      code removed; rebuilt for arm64 and ARMv6 (Pi Zero)
    - The service refuses an image whose name or size (32–128 KB) is not a
      PiJuice firmware before anything is erased
    - The daemon pauses its polling around a flash (SIGUSR1/SIGUSR2, two-minute
      safety timeout) so it never interleaves with the bootloader protocol
    - Failures carry the flasher's last output lines (e.g. `verify failed 11`)
    - README documents the fail-safe write order and the SW3 recovery

* pijuice_power 1.2:
    - `charge_full_design` is its own value (it aliased `charge_full`)
    - No phantom battery before the daemon writes (`present = 0`, `capacity = 0`)
    - A uevent only when a value changes (was one per write, ~1.4/s)
    - `charge_now` computed without integer truncation
    - postrm removes every registered module version, not a hardcoded one

* pijuice.py (upstream API unchanged): `SetTime` accepts fractional subseconds
  (any non-zero value was rejected); `SetTime`/`SetAlarm` reject 60 for
  seconds/minutes; no-op branch and bare-name handler removed; `--version`
  without an argument no longer raises

* src/pijuice_log.py: wrapped in `main()` (import-safe), honours the configured
  I2C bus/address via `PiJuiceService`, no longer crashes on MESSAGE/VALUE
  records (`'dict' object is not callable`), `--enable`/`--disable` exit after
  acting, bounded read loop

* Packaging:
    - Removed the dead stdeb/distutils path (`setup.py`, `stdeb.cfg`,
      `debian-*/rules`, `VERSION`); `pckg-pijuice.sh` is the only build
    - `pijuice.service` owns `/run/pijuice` (`RuntimeDirectory`, 0770,
      preserved) instead of a tmpfiles.d entry
    - sudoers: `systemctl *` narrowed to `systemctl list-jobs`, dead
      `SIGUSR1/2` rules dropped, `%pijuice` may `kill -SIGHUP` so the desktop
      apps can reload the daemon without the distro's first-user NOPASSWD
    - pijuice-gui depends on pijuice-base >= 1.9 (it imports `pijuice_battery`)
    - Tray PID file and its prerm handling removed (nothing read it)

* tests/: CLI tests drive a service-backed CLI; new checks for profile apply
  stop-on-failure, I2C address persistence, status switch not-a-draft, dialog
  stacking, learned capacity

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
    - /etc/modprobe.d/pijuice_power.conf: an `install` hook hands the pijuice
      group write access to the module's otherwise root-only sysfs attrs after
      every load (replaces the racy udev rule and the service `ExecStartPre`,
      which missed module reloads)
    - pijuice_power 1.1: `charge_full`/`charge_full_design`/`charge_now`, so
      readers that ignore `capacity` (wf-panel-pi batt) show the real charge
    - src/pijuice_sys.py: journal message (once) when the power_supply node is
      missing or not writable, instead of ignoring the error
    - postinst fails the install on DKMS errors
    - Packaging: pckg-pijuice.sh builds with plain `dpkg-deb`; the stdeb/
      distutils/dh_systemd pipeline no longer runs on Debian 13. Python modules
      install to /usr/lib/python3/dist-packages. Depends on
      linux-headers-rpi-v8 | linux-headers-rpi-2712 (raspberrypi-kernel-headers
      is gone on Trixie)

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
