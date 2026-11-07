# Roseblox

Roseblox is a native Linux desktop launcher and installation manager for Roblox Player and Studio through Rosemary. It does not bundle Rosemary; on Arch-based systems its first-run setup can install the `rosemary` AUR package through your AUR helper.

Roseblox is licensed under the MIT License; see [LICENSE](LICENSE).

## Run from a checkout

Install the system GTK 4, libadwaita, and Python GObject bindings, then run:

```sh
PYTHONPATH=src python3 -m roseblox
```

On Arch Linux or CachyOS, the relevant packages are `gtk4`, `libadwaita`, and `python-gobject`.

## Install on Arch Linux or CachyOS

**See [docs/INSTALL.md](docs/INSTALL.md) for the step-by-step install, update, and uninstall guide.**

To get the latest `master`, build the `roseblox-git` package from `packaging/arch-git/PKGBUILD`:

```sh
git clone https://github.com/MadnessMyth/RoseBlox.git
cd RoseBlox/packaging/arch-git
makepkg -si
```

To update, run `git checkout -- packaging/arch-git/PKGBUILD && git pull` in the checkout (makepkg records the built version in the PKGBUILD), then `makepkg -si` in `packaging/arch-git` again.

To stay on tagged releases instead, use the pinned release PKGBUILD at `packaging/arch/PKGBUILD`, which builds the latest release tag; update it with `git pull` and `makepkg -si` in `packaging/arch`. Both packages provide `roseblox` and conflict with each other.

Run `makepkg` as a regular user; it will request system package installation through pacman as needed. Each recipe installs Roseblox into the system Python package path, its MIT license under `/usr/share/licenses/<package name>/`, a scalable application icon, and the `roseblox` command and desktop menu entry. **The AUR package has not been published yet.**

Rosemary must be discoverable as `rosemary` in `PATH`, or configured through Roseblox's Settings page. Roseblox never bundles Rosemary or downloads it itself. When Rosemary is missing, **Install Rosemary** (in setup and Settings) opens a terminal running `paru -S --needed rosemary sdl3 vulkan-icd-loader` (or `yay` when paru is absent), so you can review the [AUR package](https://aur.archlinux.org/packages/rosemary) and authenticate there. The AUR package lists SDL 3 and the Vulkan loader as optional, but Roblox cannot start without them. After a successful install the terminal runs `rosemary prefix create`, which downloads Rosemary's macOS base system into its default prefix once; Roseblox detects the new executable automatically when the build finishes. A Vulkan driver for your GPU (for example `vulkan-radeon`, `vulkan-intel` or `nvidia-utils`) is also required. The terminal is chosen from `$TERMINAL` when supported, then `xdg-terminal-exec`, GNOME Console, Ptyxis, GNOME Terminal, Konsole, Xfce Terminal, Alacritty, kitty, foot, WezTerm, Ghostty, and xterm. You can also choose any existing Rosemary executable or release AppImage. ZIP archive import and installation use `bsdtar`, provided by Arch's `libarchive` package; install `libarchive` if this command is unavailable. Roblox bundles and downloaded archives are kept in the user's XDG data directory and are not removed when Roseblox is uninstalled.

On first launch, Roseblox opens a full-window setup with four steps, each marked Required, Optional, Installing, or Ready: install or choose Rosemary, turn on Roblox link handling, install Player or Studio, and turn on update notifications. App installs run in place with progress and Cancel. You can skip and finish at any time, and reopen setup from Settings. URL handling is disabled until you explicitly enable it, and it can be turned on or off later with the **Open Roblox links with Roseblox** switch in Settings. Roseblox registers the `roblox`, `roblox-player`, `roblox-studio`, and `roblox-studio-auth` schemes for your user account using XDG desktop associations; Settings reports whether registration is active and can repair a missing registration. While it is on, Roseblox is also listed in "Open with" and "Choose application" dialogs next to other Roblox launchers, and Roseblox rebuilds the desktop caches that KDE Plasma (`kbuildsycoca6`) and other launchers (`update-desktop-database`) read. The packaged menu entry accepts a link as its argument (`roseblox URI`) but does not declare the schemes itself, so installing Roseblox never takes over Roblox links until you turn the switch on. Turning it off restores previous defaults when Roseblox still owns the current default and removes only Roseblox's own associations. The feature requires `xdg-utils` (`xdg-mime`).

**After Launching** in Settings has a separate **Close after opening Roblox Player** and **Close after opening Roblox Studio** switch, each with an **Only when opened from a link** option, so Roseblox does not stay open behind Roblox. The window closes five seconds after the app starts, unless the app has already exited (so its error stays visible) or a download or install is still running. Rosemary runs in its own session and writes its output to `$XDG_STATE_HOME/roseblox/logs/player.log` or `studio.log` (normally under `~/.local/state`; the previous run is kept as `.log.1`), so Roblox keeps running after Roseblox closes. **Allow more than one Roblox Player** (also in After Launching, off by default) adds **Launch Another** while Player runs and opens Roblox links in a new Player; each extra Player logs to `player-2.log`, `player-3.log`, and so on, and **Stop** becomes **Stop All**.

**Updates** (in Settings, and step 4 of setup) turns on a small background checker, `roseblox --update-service`. It starts with your desktop session through `~/.config/autostart/io.github.roseblox.Roseblox.Updater.desktop`, checks the installed Player and Studio versions one minute after login and every three hours, and sends a desktop notification when a newer version is available. **Update** opens Roseblox, installs the latest version with progress, and makes it the preferred installation; **Skip This Version** stays quiet until a newer version comes out. An ignored notification is repeated after a day. **Check Now** asks for a notification right away, and **Ask Again** forgets skipped versions. Older versions stay installed until you remove them. Turning the switch off stops the checker and removes its login entry. Output from a window opened by a notification goes to `$XDG_STATE_HOME/roseblox/logs/roseblox.log`.

**Settings** is split into sections that open and close with a click on their heading: Appearance, Rosemary, After Launching, Roblox Links, Updates, and Advanced. Advanced (the Rosemary executable path and the Player and Studio version sources) starts closed because wrong values there stop Roseblox from finding Rosemary or new Roblox versions. Roseblox remembers which sections you left open.

**Appearance** has three choices. **Colors** picks Roseblox's own pink and purple tones or your desktop's standard window colors. **Style** picks Dark, Light, or Follow system. **Accent color** picks Roseblox pink, your desktop's accent color, or one of nine fixed colors (blue, teal, green, yellow, orange, red, pink, purple, slate). The desktop accent color needs libadwaita 1.6 or later and a desktop that shares its accent color with apps through the settings portal (for example GNOME 47 or later, or a recent KDE Plasma); otherwise blue is used, and Settings says so.

The **notification center** is the bell at the top right of the window. A badge counts the items in it. It lists:

- Downloads in progress, with progress, speed, time left, and Cancel.
- Available Player and Studio updates, with **Update** and **Skip**, as in the desktop notification.
- Recently finished or failed installs, with **Clear**.

While **Notify me about Roblox updates** is on, it also checks for updates when Roseblox opens and every three hours while it stays open. **Check for Updates** in the center always works.

Download bars show the percentage, the current speed, and the time left, for example `37% · 12.4 MiB/s · 1 min 20 s left`. Speed is averaged over the last five seconds, and a resumed download counts only newly received data.

The setup choices are stored at `$XDG_CONFIG_HOME/roseblox/setup.json`. URL registrations are per-user XDG desktop data; disabling URL handling does not remove other applications' handlers or Roseblox itself.

**Ctrl + left-click compatibility** (Settings → Rosemary, on by default) passes `ROSEMARY_PLAIN_CTRL_CLICK=1` to Player and Studio launches, so supported Rosemary builds deliver Ctrl + left-click as a normal left-click. Turn it off to keep Rosemary's default behavior. The switch is greyed out when the selected Rosemary executable does not contain the supported option marker; such builds always keep their default behavior. Roseblox does not alter global keyboard or desktop settings.

The **Performance** page keeps separate launch settings for Player and Studio, saved in `$XDG_CONFIG_HOME/roseblox/launch-config.json`: presets, GPU choice, Rosemary's presentation mode and refresh rate, Feral GameMode, the MangoHud overlay, allowlisted Roblox graphics flags, a FastFlag editor that checks Player flags against Roblox's published allowlist, the Command key and titlebar, Studio's `-disableLoadUserPlugins` switch, environment variables for Rosemary and for Roblox, and extra launch arguments. Every change is listed for review before it is saved, and the previous settings are kept as a backup that can be restored. FastFlags are written to the bundle's `Contents/MacOS/ClientSettings/ClientAppSettings.json` at launch; a file Roseblox did not write is backed up first. The page also lists Roblox and Rosemary caches, which it moves to the trash, a diagnostics report, and **Measure & compare**, which records CPU, memory and (where the driver reports it) GPU load of the running app from `/proc`, `amdgpu` sysfs or `nvidia-smi`, saves it with the settings in use, and compares two runs side by side. Credential-like variables, library injection variables and login arguments are refused, and nothing is set for the whole desktop. See [Performance in the install guide](docs/INSTALL.md#performance).

**While Playing** in Settings adds Discord Rich Presence (the game's name, creator and icon from Roblox's public APIs, with View Game and Join Server buttons), a notification with the location of the Roblox server you joined (looked up with ipwho.is; your own address is never sent), and closing Roblox when you leave a game. They are off by default. When one is on, each Player launch starts `roseblox --activity-watch`, a small process without a window that reads Roblox's log output, talks to Discord's local IPC socket, and exits when that Rosemary process does, so these keep working after Roseblox closes.

The **Mods** page replaces fonts, the mouse cursor, the death sound, the avatar editor background, and any images, textures, sounds, fonts or meshes you put in `$XDG_DATA_HOME/roseblox/mods/<player|studio>/` (mirroring the app's `Contents/Resources`). Mods are applied right before launch, only to installations Roseblox installed; Roblox's originals are kept in `.roseblox-originals` next to the bundle and restored when a mod is turned off. Scripts, models, places and code are never replaced.

Studio installations have **Open Place…**, which opens a `.rbxl` or `.rbxlx` file with Studio's `-task EditFile -localPlaceFile` arguments, translating the path into the one Rosemary's guest sees (`/Volumes/Host/...` for files outside the prefix).

Player and Studio pages can register existing `RobloxPlayer.app` and `RobloxStudio.app` bundles, launch them through Rosemary, check the current Roblox versions, and download and install versioned archives. The Library page can import existing application bundles or ZIP archives, list managed and external registrations, and remove registrations. Managed bundles can be deleted only after explicit confirmation; external bundles are never deleted by Roseblox. Completed downloads remain in the managed archives directory after installation.

The Home page provides Player and Studio launch cards. On startup, each card selects its preferred installation when that bundle is available. Choose **Set as preferred** on a Player or Studio installation row to save that application's default. You can still select another installation temporarily on Home; that selection does not change the saved preference. Preferences are stored separately at `$XDG_CONFIG_HOME/roseblox/preferences.json` (normally `~/.config/roseblox/preferences.json`). If a preferred installation is removed or its bundle is invalid, Home uses the first valid installation without silently changing the saved choice. If no valid installation exists, Home shows setup guidance. Its dark pink/purple background is a local decorative placeholder at `src/roseblox/resources/home-artwork-placeholder.svg`; replace that asset to update the artwork. Cards remain readable against the CSS fallback when the artwork is unavailable.

Archive extraction requires the system `bsdtar` command (provided by libarchive on Arch Linux). Roseblox does not install this dependency. If it is unavailable, archive extraction reports that `bsdtar` must be installed through the system package manager. Extraction runs in the background and validates archive paths and symbolic links before registering the bundle.

The JSON library is stored at `$XDG_DATA_HOME/roseblox/library.json` (normally `~/.local/share/roseblox/library.json`). Successful version checks are cached separately at `$XDG_DATA_HOME/roseblox/versions.json`. Recoverable download metadata is stored in `downloads.json`, with partial files under `.staging/downloads/`. Managed bundles are stored under `$XDG_DATA_HOME/roseblox/installations/`, and completed downloaded archives under `$XDG_DATA_HOME/roseblox/archives/`. Roseblox resumes partial downloads with HTTP Range when supported; otherwise it restarts that download safely. Rosemary settings are read from `$XDG_CONFIG_HOME/roseblox/settings.json` (normally `~/.config/roseblox/settings.json`); a `rosemary_executable` setting can override PATH detection.

Version source settings are stored separately in `$XDG_CONFIG_HOME/roseblox/version_sources.json`. Player and Studio can each use a custom HTTPS JSON endpoint and RFC 6901 JSON Pointer, including nested object keys and array indexes. Defaults remain Roblox's established `MacPlayer` and `MacStudio` endpoints with `/clientVersionUpload`. Test Configuration displays connection and HTTP status, JSON parsing, the extracted value, and strict version validation without saving the fields or updating the version cache; use Save to apply the configuration. Restore Defaults affects only the selected application. Archive URL patterns remain fixed to the established Roblox CDN URLs, and parsed values must match the validated Roblox version format. Version checks use a 15-second timeout and read at most 1 MiB of response data. The Settings page also displays Rosemary status.

Library records are loaded and bundle-validated in a shared background snapshot. The Library, Player, Studio, and Home views reuse that snapshot instead of each repeating the read and validation work. Recoverable download state is likewise scanned once during startup and shared with the Player and Studio pages.

See [the release checklist and notes](packaging/RELEASE.md) before preparing a public release. [Flatpak and AppImage distribution constraints](packaging/DISTRIBUTION.md) are documented for future evaluation; neither format is currently provided.
