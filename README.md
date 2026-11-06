# Roseblox

Roseblox is a native Linux desktop launcher and installation manager for Roblox Player and Studio through an existing Rosemary installation. It does not include or install Rosemary.

Roseblox is licensed under the MIT License; see [LICENSE](LICENSE).

## Run from a checkout

Install the system GTK 4, libadwaita, and Python GObject bindings, then run:

```sh
PYTHONPATH=src python3 -m roseblox
```

On Arch Linux or CachyOS, the relevant packages are `gtk4`, `libadwaita`, and `python-gobject`.

## Install on Arch Linux or CachyOS

The repository includes a pinned PKGBUILD at `packaging/arch/PKGBUILD`. Build and install it from a checkout with `makepkg`:

```sh
git clone https://github.com/MadnessMyth/RoseBlox.git
cd RoseBlox/packaging/arch
makepkg -si
```

Run `makepkg` as a regular user; it will request system package installation through pacman as needed. The recipe installs Roseblox into the system Python package path, its MIT license under `/usr/share/licenses/roseblox/`, a scalable application icon, and the `roseblox` command and desktop menu entry. Package sources use the versioned `v0.1.0` Git tag. **The AUR package has not been published yet.**

Rosemary must already be installed separately and discoverable as `rosemary`, or configured through Roseblox's settings file. Roseblox does not download, bundle, or install Rosemary, and no Rosemary package name is assumed. ZIP archive import and installation use `bsdtar`, provided by Arch's `libarchive` package; install `libarchive` if this command is unavailable. Roblox bundles and downloaded archives are kept in the user's XDG data directory and are not removed when Roseblox is uninstalled.

Player and Studio pages can register existing `RobloxPlayer.app` and `RobloxStudio.app` bundles, launch them through Rosemary, check the current Roblox versions, and download and install versioned archives. The Library page can import existing application bundles or ZIP archives, list managed and external registrations, and remove registrations. Managed bundles can be deleted only after explicit confirmation; external bundles are never deleted by Roseblox. Completed downloads remain in the managed archives directory after installation.

The Home page provides Player and Studio launch cards. On startup, each card selects its preferred installation when that bundle is available. Choose **Set as preferred** on a Player or Studio installation row to save that application's default. You can still select another installation temporarily on Home; that selection does not change the saved preference. Preferences are stored separately at `$XDG_CONFIG_HOME/roseblox/preferences.json` (normally `~/.config/roseblox/preferences.json`). If a preferred installation is removed or its bundle is invalid, Home uses the first valid installation without silently changing the saved choice. If no valid installation exists, Home shows setup guidance. Its dark pink/purple background is a local decorative placeholder at `src/roseblox/resources/home-artwork-placeholder.svg`; replace that asset to update the artwork. Cards remain readable against the CSS fallback when the artwork is unavailable.

Archive extraction requires the system `bsdtar` command (provided by libarchive on Arch Linux). Roseblox does not install this dependency. If it is unavailable, archive extraction reports that `bsdtar` must be installed through the system package manager. Extraction runs in the background and validates archive paths and symbolic links before registering the bundle.

The JSON library is stored at `$XDG_DATA_HOME/roseblox/library.json` (normally `~/.local/share/roseblox/library.json`). Successful version checks are cached separately at `$XDG_DATA_HOME/roseblox/versions.json`. Recoverable download metadata is stored in `downloads.json`, with partial files under `.staging/downloads/`. Managed bundles are stored under `$XDG_DATA_HOME/roseblox/installations/`, and completed downloaded archives under `$XDG_DATA_HOME/roseblox/archives/`. Roseblox resumes partial downloads with HTTP Range when supported; otherwise it restarts that download safely. Rosemary settings are read from `$XDG_CONFIG_HOME/roseblox/settings.json` (normally `~/.config/roseblox/settings.json`); a `rosemary_executable` setting can override PATH detection.

Version source settings are stored separately in `$XDG_CONFIG_HOME/roseblox/version_sources.json`. Player and Studio can each use a custom HTTPS JSON endpoint and RFC 6901 JSON Pointer, including nested object keys and array indexes. Defaults remain Roblox's established `MacPlayer` and `MacStudio` endpoints with `/clientVersionUpload`. Test Configuration displays connection and HTTP status, JSON parsing, the extracted value, and strict version validation without saving the fields or updating the version cache; use Save to apply the configuration. Restore Defaults affects only the selected application. Archive URL patterns remain fixed to the established Roblox CDN URLs, and parsed values must match the validated Roblox version format. Version checks use a 15-second timeout and read at most 1 MiB of response data. The Settings page also displays Rosemary status.

Library records are loaded and bundle-validated in a shared background snapshot. The Library, Player, Studio, and Home views reuse that snapshot instead of each repeating the read and validation work. Recoverable download state is likewise scanned once during startup and shared with the Player and Studio pages.

See [the v0.1.0 release checklist and draft notes](packaging/RELEASE.md) before preparing a public release. [Flatpak and AppImage distribution constraints](packaging/DISTRIBUTION.md) are documented for future evaluation; neither format is currently provided.
