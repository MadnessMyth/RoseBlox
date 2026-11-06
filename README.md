# Roseblox

Roseblox is a native Linux desktop launcher and installation manager for Roblox Player and Studio through an existing Rosemary installation. It does not include or install Rosemary.

## Run from a checkout

Install the system GTK 4, libadwaita, and Python GObject bindings, then run:

```sh
PYTHONPATH=src python3 -m roseblox
```

On Arch Linux or CachyOS, the relevant packages are `gtk4`, `libadwaita`, and `python-gobject`.

Player and Studio pages can register existing `RobloxPlayer.app` and `RobloxStudio.app` bundles, launch them through Rosemary, check the current Roblox versions, and download and install versioned archives. The Library page can import existing application bundles or ZIP archives, list managed and external registrations, and remove registrations. Managed bundles can be deleted only after explicit confirmation; external bundles are never deleted by Roseblox. Completed downloads remain in the managed archives directory after installation.

The Home page provides Player and Studio launch cards. Each card shows the selected registered installation and its availability; when multiple installations are registered, choose one from the card's installation selector. Selection is retained while the installation remains registered during Library refreshes. Setup actions lead to the existing Player or Studio management page. Home uses the same process lifecycle and launch controller as those pages. Its dark pink/purple background is a local decorative placeholder at `src/roseblox/resources/home-artwork-placeholder.svg`; replace that asset to update the artwork. Cards remain readable against the CSS fallback when the artwork is unavailable.

Archive extraction requires the system `bsdtar` command (provided by libarchive on Arch Linux). Roseblox does not install this dependency. If it is unavailable, archive extraction reports that `bsdtar` must be installed through the system package manager. Extraction runs in the background and validates archive paths and symbolic links before registering the bundle.

The JSON library is stored at `$XDG_DATA_HOME/roseblox/library.json` (normally `~/.local/share/roseblox/library.json`). Successful version checks are cached separately at `$XDG_DATA_HOME/roseblox/versions.json`. Recoverable download metadata is stored in `downloads.json`, with partial files under `.staging/downloads/`. Managed bundles are stored under `$XDG_DATA_HOME/roseblox/installations/`, and completed downloaded archives under `$XDG_DATA_HOME/roseblox/archives/`. Roseblox resumes partial downloads with HTTP Range when supported; otherwise it restarts that download safely. Rosemary settings are read from `$XDG_CONFIG_HOME/roseblox/settings.json` (normally `~/.config/roseblox/settings.json`); a `rosemary_executable` setting can override PATH detection.

Version source settings are stored separately in `$XDG_CONFIG_HOME/roseblox/version_sources.json`. Player and Studio can each use a custom HTTPS JSON endpoint and RFC 6901 JSON Pointer, including nested object keys and array indexes. Defaults remain Roblox's established `MacPlayer` and `MacStudio` endpoints with `/clientVersionUpload`. Test Configuration displays connection and HTTP status, JSON parsing, the extracted value, and strict version validation without saving the fields or updating the version cache; use Save to apply the configuration. Restore Defaults affects only the selected application. Archive URL patterns remain fixed to the established Roblox CDN URLs, and parsed values must match the validated Roblox version format. Version checks use a 15-second timeout and read at most 1 MiB of response data. The Settings page also displays Rosemary status.

Library records are loaded and bundle-validated in a shared background snapshot. The Library, Player, Studio, and Home views reuse that snapshot instead of each repeating the read and validation work. Recoverable download state is likewise scanned once during startup and shared with the Player and Studio pages.
