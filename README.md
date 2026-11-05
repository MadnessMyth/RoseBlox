# Roseblox

Roseblox is a native Linux desktop launcher and installation manager for Roblox Player and Studio through an existing Rosemary installation. It does not include or install Rosemary.

## Run from a checkout

Install the system GTK 4, libadwaita, and Python GObject bindings, then run:

```sh
PYTHONPATH=src python3 -m roseblox
```

On Arch Linux or CachyOS, the relevant packages are `gtk4`, `libadwaita`, and `python-gobject`.

Player and Studio pages can register existing `RobloxPlayer.app` and `RobloxStudio.app` bundles, launch them through Rosemary, and remove their library registrations without deleting the bundles. The Library page lists registrations, and Settings shows Rosemary's detected executable. Roseblox does not copy, download, extract, or modify Roblox bundles or Rosemary.

The JSON library is stored at `$XDG_DATA_HOME/roseblox/library.json` (normally `~/.local/share/roseblox/library.json`). Rosemary settings are read from `$XDG_CONFIG_HOME/roseblox/settings.json` (normally `~/.config/roseblox/settings.json`); a `rosemary_executable` setting can override PATH detection. The settings page currently displays status only.
