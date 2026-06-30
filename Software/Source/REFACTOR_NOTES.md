# PiJuice refactor — handoff / status

Self-contained context for continuing the GTK4 / service-layer refactor in a fresh
Claude Code session. Fork `dpezto/PiJuice`, branch `master`, code under `Software/Source/`.

## Goal
Wayland-compatible GUI: off TkInter onto **plain GTK4** (no libadwaita), **I2C
decoupled from the UI**. CLI (urwid) gets usability improvements, not a toolkit swap.

## Architecture
- `pijuice.py` — UI-free I2C library (unchanged).
- `pijuice_service.py` (Source root, py_module) — UI-agnostic facade. Both UIs go
  through it; nothing else touches I2C or the JSON config. Serialises I2C on one
  worker thread (`submit()` → `Future`), unwraps `{data,error}` into values /
  `PiJuiceError`, imports off-Pi (`available is False` when smbus/HAT absent).
- GTK views marshal worker results back with `GLib.idle_add` (`_View.run_async`).
- Runtime files standardised on **`/run/pijuice/`** (pid, lock, halt flag) across the
  daemon, CLI, GUI and service. The unit needs `RuntimeDirectory=pijuice`.

## Status — refactor essentially complete
- **GTK4 GUI**: all 10 tabs ported (Status, Buttons, LEDs, System Events, User
  Scripts, Battery, IO, Wakeup, System Task, Firmware). Hardware-validated over VNC.
- **Tray**: `pijuice_tray.py` on GTK3 + libayatana-appindicator (GTK4 has no tray
  API) → StatusNotifierItem for the Wayfire panel. Runs as the desktop user.
- **CLI**: urwid refactor (modal vim opt-in, directional nav, header `← back`,
  file picker, Settings tab) done + installed.
- **Installed** to `/usr/bin` (`pijuice_gtk.py`, `pijuice_service.py`,
  `pijuice_tray.py`, `pijuice_sys.py`, `pijuice_cli.py`; `.bak` backups kept),
  menu entry `/usr/share/applications/pijuice-gtk.desktop`, autostart fixed to run
  the tray as the desktop user.
- **save-notify fixed**: the old installed daemon wrote `/tmp/pijuice_sys.pid` while
  the refactored readers use `/run/pijuice/pijuice_sys.pid` → "Failed to communicate
  with PiJuice service" on save. Installing the repo daemon (which uses `/run`)
  reconciled it. Keep all four runtime-path constants on `/run/pijuice`.

## Remaining work
1. **Make GTK the default launcher**: the desktop `pijuice_gui` is a setuid C wrapper
   (`bin/pijuice_gui*`) still pointing at the Tk GUI. `data/pijuice-gtk.desktop` +
   `setup.py`/`control` ship the GTK app; remaining is repointing/replacing the
   wrapper (or making the .desktop the menu default) so users get GTK4 by default.
2. **Retire `pijuice_gui.py`** (Tk, ~2.4k lines — the last old-toolkit GUI, fully
   superseded by `pijuice_gtk.py`). Delete once item 1 lands; then `python3-tk` drops
   from the GUI deps. Biggest dead-code win.
3. **Packaging**: ensure the deb ships `RuntimeDirectory=pijuice` in the unit and
   installs `pijuice_gtk.py`/`pijuice_service.py`/the new tray + AppIndicator dep
   (`gir1.2-ayatanaappindicator3-0.1`, already in `debian-gui/control`).

## CLI improvement backlog (later, not now)
1. **Apply-less settings / warning rework.** Some controls apply immediately (e.g.
   the vim-keybindings checkbox toggles live), so the "unsaved changes" warning on
   back is wrong for them. Decide: (a) suppress the dirty-warning for live-apply
   settings, or (b) rework toward immediate-apply and drop Apply buttons where it's
   safe (config-JSON tabs are cheap; I2C tabs are not — per-keystroke writes are bad).
2. **Button glyph consistency.** `( )` = urwid `RadioButton` (pick-one), `[ ]` =
   `CheckBox` / our button chrome (toggle / action). Decide whether the radio-vs-check
   distinction earns its keep or should be unified.
3. **Refine looks & colours** throughout the CLI (palette, spacing, focus styling).
- Also still open: two deferred warts — intra-tab back doesn't restore focus
  (lands on top); dirty warning over-fires on intra-tab Edit sub-views.

## Run / verify on the Pi (echidna, has the HAT)
```bash
cd Software/Source
PYTHONPATH=.:src /usr/bin/python3 src/pijuice_gtk.py   # GTK4 GUI (needs a Wayland display)
pijuice_cli                                            # installed CLI (setuid pijuice)
```
Note: bare `python3` is linuxbrew (no gi/smbus/urwid) — always use `/usr/bin/python3`.

## Reference
Full plan: `~/.claude/plans/declarative-watching-sutton.md` (global, readable here).
