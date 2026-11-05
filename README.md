# Roseblox

Roseblox is a native Linux desktop launcher and installation manager for Roblox Player and Studio through an existing Rosemary installation. It does not include or install Rosemary.

## Run from a checkout

Install the system GTK 4, libadwaita, and Python GObject bindings, then run:

```sh
PYTHONPATH=src python3 -m roseblox
```

On Arch Linux or CachyOS, the relevant packages are `gtk4`, `libadwaita`, and `python-gobject`.

Milestone 1 provides the native application shell and navigation. Player, Studio, Library, and Settings are currently placeholder pages.
