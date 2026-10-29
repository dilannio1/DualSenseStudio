# DualSense Studio

Windows app to control a DualSense controller's adaptive triggers, with
per-game profiles, virtual-pad emulation (Xbox 360 / DualShock 4 / Solo
Triggers), HidHide cloak against double input, touchpad-as-mouse, and
in-game profile quick-switching with a modifier + D-pad chord.

> English summary: a Windows companion for the DualSense controller —
> adaptive trigger control, per-game profiles, virtual controller
> emulation, HidHide anti-double-input, and quick profile switching
> without leaving the game. Work in progress.

## Status

In active development. Known pending work:

- Touchpad gestures in some games (e.g. Spider-Man 2) still need a fix;
  raw-report logging (`debug_reportes.log`) is built in to diagnose
  Bluetooth adapters that deliver reports in other formats.
- Bluetooth report reading is being generalized for different adapters.

## Honesty note

This is Pedro's project (GitHub: dilannio1). He directs it, tests every
build on his own hardware, and hunts bugs with his own measurements. The
code was written with AI assistance. It is published free (MIT) for
community recognition; donations, if any, are a bonus, not the goal.

## Requirements (Windows)

- A DualSense controller (USB or Bluetooth).
- [ViGEmBus driver](https://github.com/nefarius/ViGEmBus/releases) —
  needed for virtual-pad emulation (Xbox 360 / DualShock 4). Without it
  the app falls back to Solo Triggers and warns you.
- [HidHide driver](https://github.com/nefarius/HidHide/releases) —
  optional, for hiding the physical pad from games/Steam (anti
  double-input). The app configures the cloak itself.
- The app runs elevated (the exe requests admin via UAC) so it can talk
  to the drivers.

## Build the .exe

1. Install Python 3.12 (64-bit) from python.org.
2. Put `valdomero.ico` next to `dualsense_studio.py` (it ships in the
   release ZIPs; it is the window/exe icon).
3. Double-click `build_studio_exe.bat`. It installs the pinned
   dependency versions (see `requirements.txt`) and produces
   `dist\DualSenseStudio.exe`.

Do not run the app from the `.py` file for daily use; the compiled exe
is the supported path.

## Run from source (development only)

```
py -3.12 -m pip install -r requirements.txt
py -3.12 dualsense_studio.py
```

## Profiles and anti-cheat posture

- Factory profiles always start with PS4 emulation + cloak ON, except
  the `Anti-cheat` profiles, which use Solo Triggers + cloak OFF so
  games with kernel anti-cheat (e.g. EA Javelin) open normally.
- Quick-switching inside a game (modifier + D-pad arrow) changes only
  the trigger feel, never emulation or cloak.
- Saving a factory profile never rewrites its emulation/cloak posture;
  user-created profiles save everything.
- "Disparo Automático" is haptic trigger resistance only. The app has
  no macros and no mouse-to-controller mapping. Never run it together
  with DS4Windows.

## Updates

The app checks GitHub Releases and shows the update in Settings, with a
button. The user decides when to update; nothing is pushed or installed
automatically, and nothing interrupts a game.

## License

MIT — see `LICENSE`. Copyright (c) 2026 dilannio1 (Pedro).
