# PiJuice refactor — handoff / status

Self-contained context for continuing the GTK4 / service-layer refactor in a fresh
Claude Code session started in this repo. (Sessions don't carry across dirs; read
this file first.)

Fork: `dpezto/PiJuice`, branch `master`. Code under `Software/Source/`.

## Goal
Make the GUI Wayland-compatible by moving off TkInter to **plain GTK4** (no
libadwaita), and **decouple I2C from the UI**. CLI (urwid) gets usability
improvements, not a toolkit swap. README TODOs (in the dotfiles repo) drive scope.

## Architecture decided
- `pijuice.py` stays as the UI-free I2C library.
- New `pijuice_service.py` (Source ROOT, a py_module) = UI-agnostic facade. Both UIs
  go through it; nothing else touches I2C or the JSON config. Serialises I2C on one
  worker thread (`submit()` -> `concurrent.futures.Future`), unwraps the lib's
  `{data,error}` dicts into return values / `PiJuiceError`, imports off-Pi
  (`available is False` when smbus/HAT absent).
- GTK views marshal worker results back with `GLib.idle_add` (see `_View.run_async`).

## Done (code; NOT yet hardware-validated)
- `pijuice_service.py` — facade; domains: status, battery, IO, fault, power
  (system switch), buttons, LEDs. Config load/save + `notify_service` (SIGHUP).
- `src/pijuice_gtk.py` — plain GTK4 app (`Gtk.Application`, `Stack`+`StackSidebar`,
  touch CSS). Real views: **Status, Buttons, LEDs, System Events, User Scripts**.
  Scaffolds (service wired, widgets TODO): **Battery, IO, Wakeup, System Task, Firmware**.
- `src/pijuice_cli.py` — coloured palette + focus styling; `[ ]` button chrome;
  `CyclingListBox` everywhere (wrap + `gg`/`G` land on selectable rows). **Modal
  vim** (opt-in, default OFF): normal/insert, `i`/`a`→insert, Esc→normal,
  `-- INSERT --` in the LineBox title; toggle in a new **Settings** tab persisted
  to `cli_settings.vim_keys`. **Directional nav**: `h`/`l`/`j`/`k` map to
  left/right/down/up (move focus, incl. across columns), Enter selects; **back is
  not a key** — `go_back` fires from `unhandled_input` when `left` (or `h`) goes
  unhandled, i.e. focus is at the left edge (vertical list / leftmost column). So
  in two-column views (System Task) `h`/`l` move between columns and only `h` at
  the leftmost column backs out. **Back is hoisted into a header row**
  (`_ContentArea` removes each view's Back/Cancel button from the body and drives
  it from a clickable header `← back`, bracket-free `_BareButton`). Header also
  shows the tab name; returning to the main menu restores focus to the item you
  came from (`_last_choice`). **Esc** = leave insert → else cancel dialog → else
  back → else (at root) quit the CLI; works inside text fields too. **Dirty
  guard**: editing a text field warns before discarding on back; the Yes/No dialog
  (horizontal — `←`/`→`/`h`/`l` pick, Enter confirms) captures the back target
  before it's reset, Esc cancels it (`_in_dialog`/`_dialog_cancel`).
  **User Scripts**: each slot is a `ScriptEdit` — Enter (vim `l`→enter) opens the
  **`FileNavigator`** picker (hjkl; fills the bare path, strips shell args); no
  more per-row Browse button. `--selftest` runs the pure `vim_translate`/
  `_hoist_back` checks off-Pi. Known warts: (1) intra-tab back (sub-view → list)
  doesn't restore focus, lands on top; (2) the dirty warning can fire on intra-tab
  back from Edit sub-views (LED colour, button param) where the edit is held, not
  lost — over-warns, never loses silently.
- `USER_FUNCTIONS.md` — user-function + button-mapping guide (on/off/visor examples).
- Packaging: `setup.py` ships `pijuice_service` (base py_module) + `pijuice_gtk.py`;
  `debian-gui/control` adds `python3-gi`, `gir1.2-gtk-4.0` (kept `python3-tk` for the
  still-shipped Tk GUI). `data/pijuice-gtk.desktop` launcher (direct exec).

### LED "red disables green" — root cause + fix
NOT a library bug: `SetLedState`/`SetLedConfiguration` send R/G/B as independent
bytes. Cause is the LED's *function* being CHARGE_STATUS (firmware re-drives R/G).
Fix: `PiJuiceService.set_led_color()` forces USER_LED before the live write. The GTK
LED view exposes it via "Test colour".

## How to run / verify on the Pi (echidna, has the HAT)
```bash
cd Software/Source
PYTHONPATH=.:src python3 src/pijuice_gtk.py        # GTK4 GUI (Wayland)
PYTHONPATH=.:src python3 - <<'PY'                  # service sanity
import pijuice_service as s; svc=s.PiJuiceService(); print("available", svc.available)
print("fw", svc.firmware_version)
for led in svc.leds: print(led, svc.get_led_config(led))
PY
```
Off-Pi here only `py_compile` + logic tests were possible (no smbus/gi/urwid).

### LED fix validation
1. `svc.get_led_config('D1'/'D2')` — note the function.
2. If not USER_LED, `svc.set_led_color('D2', [255,255,255])` should light white; confirm
   green isn't dropped. Check firmware version if it still misbehaves.

## Remaining work
1. **Validate on hardware**: GTK app under Wayfire/sway, LED fix, button/event mapping.
2. **Finish the 5 scaffolded GTK views** — port from `src/pijuice_gui.py`:
   - Battery -> needs service methods wrapping `config.GetBatteryProfile*/SetBatteryProfile`,
     charging config, temp-sense, RSOC.
   - IO -> `config.GetIoConfiguration`/`SetIoConfiguration` (+ digital/analog).
   - Wakeup -> `rtcAlarm.GetAlarm/SetAlarm/GetControlStatus/SetWakeupEnabled` and
     `power.Get/SetWakeUpOnCharge`.
   - System Task -> config-JSON params (watchdog, wakeup-charge, min-charge,
     min-voltage, ext-halt) like the Tk `PiJuiceSysTaskTab`; uses `service.config` +
     `service.save_and_notify()` (no I2C).
   - Firmware -> `config.GetFirmwareVersion` (have) + update flow (complex; lowest priority).
   Pattern: add a typed method to `pijuice_service.py`, build a `_View` subclass,
   read via `run_async`, write on Apply, register it in `PiJuiceWindow`'s `views` list.
3. **Wire the existing CLI/GUI onto the service** (optional, behaviour-neutral) to drop
   the duplicated helpers in `pijuice_cli.py`/`pijuice_gui.py`. Test carefully — CLI
   runs setuid as the `pijuice` user.
4. **Repoint the launcher**: `bin/pijuice_gui*` are compiled setuid C wrappers; making
   the GTK app the default needs the build toolchain on the Pi.

## Reference
Full plan: `~/.claude/plans/declarative-watching-sutton.md` (global, readable here).
