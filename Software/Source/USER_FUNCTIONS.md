# PiJuice user functions & custom button actions

How to make the PiJuice HAT run your own commands when a hardware button is
pressed (or a system event fires). This covers the two pieces that confuse people:
**user functions** (the commands) and **button mapping** (the triggers).

## The model

| Piece                      | Where                                                     | What it is                                                                     |
| -------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `USER_FUNC1`…`USER_FUNC15` | `/var/lib/pijuice/pijuice_config.JSON` → `user_functions` | A shell command string per slot.                                               |
| Button event → function    | firmware (set over I2C)                                   | Maps `SW1/2/3` × `PRESS/RELEASE/SINGLE/DOUBLE/LONG` to a function name.        |
| `pijuice_sys.py`           | systemd `pijuice.service`                                 | The daemon that actually runs the command when the firmware reports the event. |

Flow: **press button → firmware records the configured function → `pijuice_sys`
sees `USER_FUNCx` → runs the matching command from `user_functions`.**

Important: `pijuice_sys` runs as the **`pijuice`** user, not root and not your
login user. Commands that need your Wayland session or root must go through a
small wrapper (see _Permissions_ below).

## 1. Define the command (User Scripts)

GUI (`pijuice_gtk.py`) → **User Scripts**, or CLI (`pijuice_cli.py`) → **User
Scripts**. Put an **absolute path** in a slot, e.g.:

```
USER_FUNC1 = /usr/local/bin/pijuice-poweroff.sh
USER_FUNC2 = /usr/local/bin/pijuice-osk.sh
USER_FUNC3 = /usr/local/bin/visor-open.sh
USER_FUNC4 = /usr/local/bin/visor-close.sh
```

Apply — this writes `pijuice_config.JSON` and `SIGHUP`s the service.

## 2. Map a button to it (Buttons)

GUI/CLI → **Buttons**. For the button + event you want, set **Function** to the
matching `USER_FUNCx` and leave **Parameter** at `0`. Apply.

Built-in (firmware) functions you can pick instead of a user function:

- `HARD_FUNC_POWER_ON`, `HARD_FUNC_POWER_OFF`, `HARD_FUNC_RESET`
- `SYS_FUNC_HALT`, `SYS_FUNC_HALT_POW_OFF`, `SYS_FUNC_SYS_OFF_HALT`, `SYS_FUNC_REBOOT`

So a plain **off** can use `SYS_FUNC_HALT_POW_OFF` directly (no script needed),
and **on** is `HARD_FUNC_POWER_ON`. Use user functions when you need custom
behaviour (open/close visor, toggle OSK, run a camera script, …).

## 3. Permissions (the part that bites)

`pijuice_sys` runs as `pijuice`. To touch your Wayland session or run privileged
commands, install a wrapper in `/usr/local/bin` and (if needed) grant the
`pijuice` user passwordless sudo for just that command.

Example `visor-open.sh` driving a GPIO/servo as root:

```bash
#!/usr/bin/env bash
# /usr/local/bin/visor-open.sh — runs as the pijuice user
exec sudo /usr/local/sbin/visor.py open
```

`/etc/sudoers.d/pijuice-visor` (validate with `visudo -c`):

```
pijuice ALL=(root) NOPASSWD: /usr/local/sbin/visor.py
```

Example talking to the logged-in Wayland session (toggle on-screen keyboard),
following the wrapper pattern already documented in the dotfiles README:

```bash
#!/usr/bin/env bash
# /usr/local/bin/pijuice-osk.sh
export WAYLAND_DISPLAY=wayland-1
export XDG_RUNTIME_DIR=/run/user/1000
pkill wvkbd-mobintl || wvkbd-mobintl -L 280 &
```

## Worked example: on / off / open visor / close visor

| Button event | Function                | Command (USER_FUNC)             |
| ------------ | ----------------------- | ------------------------------- |
| SW1 SINGLE   | `HARD_FUNC_POWER_ON`    | — (firmware)                    |
| SW1 LONG     | `SYS_FUNC_HALT_POW_OFF` | — (firmware)                    |
| SW2 SINGLE   | `USER_FUNC3`            | `/usr/local/bin/visor-open.sh`  |
| SW3 SINGLE   | `USER_FUNC4`            | `/usr/local/bin/visor-close.sh` |

After editing, restart cleanly so the daemon re-reads everything:

```bash
sudo chown -R pijuice:pijuice /var/lib/pijuice   # keep service-owned (see dotfiles README)
sudo systemctl restart pijuice.service
```

## Troubleshooting

- Nothing happens on press → check the event is mapped to the right `USER_FUNCx`
  and the slot is non-empty; `journalctl -u pijuice.service -f` while you press.
- "Permission denied" → the command needs a sudo wrapper (runs as `pijuice`).
- Edited the JSON by hand → never `chmod 700` `/var/lib/pijuice`; the service
  crashes. Restore `770` (dir) / `660` (JSON) + `pijuice:pijuice` ownership.
