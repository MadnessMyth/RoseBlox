# How Roseblox works

This page explains what Roseblox does behind its windows: how it starts Roblox, where it gets Roblox from, what it stores and where, and how each feature works. To install and use Roseblox, see [the install guide](INSTALL.md) instead.

## The big picture

Roblox publishes Player and Studio for Windows and macOS, but not for Linux. Roseblox uses the macOS versions:

1. **Rosemary** runs macOS apps on Linux. It provides a macOS base system (its *prefix*) and translates the Metal graphics the macOS Roblox apps draw with into Vulkan, which Linux graphics drivers understand. Rosemary is a separate project; Roseblox never bundles it or downloads it itself.
2. **Roblox's macOS apps** (`RobloxPlayer.app` and `RobloxStudio.app`) are downloaded unchanged from Roblox's own servers.
3. **Roseblox** is the desktop app around them, written in Python with GTK 4 and libadwaita. It installs and updates the Roblox apps, starts them through Rosemary with your settings, opens Roblox links from the browser, and adds features such as mods and Discord Rich Presence.

```
roblox.com "Play" link ──▶ Roseblox ──▶ rosemary RobloxPlayer.app/ -- <link> ──▶ Roblox (macOS app) ──▶ Vulkan ──▶ your GPU
```

Roseblox never changes Roblox's program code. The only things it writes inside a Roblox app are FastFlags you chose (Roblox's own settings file) and media files you chose as mods, and it can undo both.

## Getting Rosemary

On first launch, setup checks for a `rosemary` executable in `PATH`, or the path saved in Settings → Advanced. When it is missing, what setup offers depends on the system. Roseblox reads `/etc/os-release` (`ID` and `ID_LIKE`) to tell Arch-based systems from others; a system without that file counts as Arch-based when `pacman` exists.

### On Arch-based systems

**Install Rosemary** opens a visible terminal that runs:

```sh
paru -S --needed rosemary sdl3 vulkan-icd-loader    # or yay when paru is absent
```

The [AUR package](https://aur.archlinux.org/packages/rosemary) lists SDL 3 and the Vulkan loader as optional, but Roblox cannot start without them, so they are installed together. You review the package and type your password in that terminal; Roseblox never runs `sudo` itself. After a successful install the terminal runs `rosemary prefix create`, which downloads Rosemary's macOS base system once. Roseblox notices the new executable when the build finishes.

The terminal is `$TERMINAL` when supported, otherwise the first one found of `xdg-terminal-exec`, GNOME Console, Ptyxis, GNOME Terminal, Konsole, Xfce Terminal, Alacritty, kitty, foot, WezTerm, Ghostty and xterm.

### On other systems

Rosemary's official builds for any distribution are the x86-64 AppImages attached to each [Rosemary release](https://github.com/microlift/rosemary/releases). Roseblox never downloads Rosemary itself. Instead:

1. **Open Releases Page** opens `https://github.com/microlift/rosemary/releases/latest` in your browser, and Roseblox starts watching for the download for up to 30 minutes.
2. It looks for files named `Rosemary*.AppImage` in your Downloads folder (`XDG_DOWNLOAD_DIR` from `~/.config/user-dirs.dirs`, so translated folder names work), `~/Applications`, `~/.local/bin` and `~/bin`. When several are found, it offers the highest version.
3. **Use This File** adds the user execute permission to that file (downloads are not executable) and saves its path as the Rosemary executable. The file is used where it is; nothing is copied.

On computers that are not x86-64 there is no AppImage, so setup only explains the other options: Nix (`nix profile install github:microlift/rosemary`) or building from source.

### Either way

You can always choose any Rosemary executable or AppImage with **Choose…**; an AppImage chosen this way is made executable too.

A Vulkan driver for your GPU (for example Mesa's `vulkan-radeon` or `vulkan-intel`, or `nvidia-utils`) is also required.

When a tool Roseblox uses is missing (`bsdtar`, `xdg-mime`, GameMode or MangoHud), the message names the install command for the system's package manager: `pacman`, `apt`, `dnf` or `zypper`.

## Getting Roblox

### Versions

Roseblox asks Roblox's client settings service for the current macOS versions:

- Player: `https://clientsettingscdn.roblox.com/v2/client-version/MacPlayer`
- Studio: `https://clientsettingscdn.roblox.com/v2/client-version/MacStudio`

It reads `/clientVersionUpload` from the reply and accepts it only if it matches Roblox's version format. Checks time out after 15 seconds and read at most 1 MiB. Successful results are cached in `~/.local/share/roseblox/versions.json`.

Settings → Advanced can point Player or Studio at a different HTTPS JSON endpoint and an RFC 6901 JSON Pointer (nested keys and array indexes work). **Test Configuration** shows the connection, HTTP status, parsed value and validation result without saving anything; **Save** applies it, and **Restore Defaults** resets only the selected app.

### Downloads

The download addresses are fixed to Roblox's CDN and cannot be changed:

- Player: `https://setup.rbxcdn.com/mac/<version>-RobloxPlayer.zip`
- Studio: `https://setup.rbxcdn.com/mac/<version>-RobloxStudioApp.zip`

Downloads run in the background and show progress, speed (averaged over five seconds) and time left. Partial downloads are kept under `~/.local/share/roseblox/.staging/downloads/` with their state in `downloads.json`, and resume with HTTP Range requests when the server supports them; otherwise they restart safely. Finished archives are kept in `~/.local/share/roseblox/archives/`.

### Installing

Archives are unpacked with the system `bsdtar` command (from Arch's `libarchive` package), in the background. Every path and symbolic link in the archive is checked before anything is unpacked, and the app bundle is validated before it is registered. Installed apps live in `~/.local/share/roseblox/installations/`.

### The Library

Roseblox keeps a list of installations in `~/.local/share/roseblox/library.json`. There are two kinds:

- **Managed** installations, which Roseblox downloaded or copied. Roseblox can delete these, but only after you confirm.
- **External** installations, which are app bundles you added from elsewhere (an existing `.app` folder or ZIP archive). Roseblox launches them but never deletes or modifies them, and mods never touch them.

The Library, Player, Studio and Home pages share one snapshot of this list, loaded and validated once in the background, instead of each reading it again.

Each app has a *preferred* installation, saved in `~/.config/roseblox/preferences.json`, which Home and Roblox links use. Picking another installation on Home is temporary and does not change the saved choice. If the preferred one is removed or broken, Home uses the first valid installation without overwriting your preference.

## Starting Roblox

When you press **Launch** or open a Roblox link, Roseblox:

1. Checks that Rosemary is available and that the app bundle is valid.
2. Applies your [mods](#mods) to managed installations.
3. Writes your FastFlags to the bundle's `Contents/MacOS/ClientSettings/ClientAppSettings.json`. A file there that Roseblox did not write is backed up first.
4. Builds the environment: your Performance settings become Rosemary variables (for example `ROSEMARY_METAL_PRESENTATION_MODE`, `ROSEMARY_SCREEN_REFRESH_RATE`, `ROSEMARY_COMMAND_KEY`, `MANGOHUD=1`), and variables meant for Roblox itself go through `ROSEMARY_ENVIRON`.
5. Runs Rosemary directly, without a shell:

   ```
   rosemary /path/to/RobloxPlayer.app/ -- <Roblox link> <your extra arguments>
   ```

   Everything meant for Roblox goes after `--`, because Rosemary reads its own options anywhere after the bundle path.
6. Registers the process with Feral GameMode over D-Bus when GameMode is on. If that fails, Roblox keeps running and the notification center says why.

Rosemary is started in its own session, with its output going to a log file rather than back to Roseblox. That is why Roblox keeps running when Roseblox closes. Logs are in `~/.local/state/roseblox/logs/`: `player.log` and `studio.log`, with the previous run kept as `.log.1`. A Player started while `player.log` is in use writes `player-2.log`, `player-3.log`, and so on.

**Ctrl + left-click compatibility** (on by default) adds `ROSEMARY_PLAIN_CTRL_CLICK=1`, so Rosemary builds that support it deliver Ctrl + left-click as a normal left-click. The switch is greyed out when the selected Rosemary does not contain the supported option.

Studio's **Open Place…** passes `-task EditFile -localPlaceFile <path>`, with the path translated into the one Rosemary's macOS sees (`/Volumes/Host/...` for files outside the prefix).

### More than one Player

Roblox Player's `Info.plist` sets `LSMultipleInstancesProhibited`, and Rosemary follows the Mac rule for it: opening a Player bundle while any Player runs hands the launch to the running one, even with Rosemary's `--new-instance` and even from a copy of the bundle in another folder. Running the bundle's executable is never refused. So with **Allow more than one Roblox Player** on, Roseblox starts Player as

```
rosemary /path/to/RobloxPlayer.app/Contents/MacOS/RobloxPlayer -- <Roblox link> <your extra arguments>
```

with the executable name taken from `CFBundleExecutable`. Each extra Player still runs from its own installation, so Roseblox can track and stop each one: a copy made with **Duplicate…**, or another version. A Roblox link opened while a Player runs goes to an installation that is not running yet.

### Closing Roseblox after launching

With **Close after opening Roblox Player** or **Studio** on, the Roseblox window closes five seconds after the app starts, unless the app has already exited (so its error stays visible) or a download or install is still running.

## Roblox links

Roblox's website starts games with `roblox://` and `roblox-player://` links, and Studio signs in with `roblox-studio://` and `roblox-studio-auth://`. Roseblox handles them only after you turn on **Open Roblox links with Roseblox**.

Turning it on registers those schemes for your user account with XDG desktop associations (`xdg-mime`, from `xdg-utils`), and rebuilds the desktop caches that KDE Plasma (`kbuildsycoca6`) and other launchers (`update-desktop-database`) read. Roseblox then also appears in "Open with" dialogs. The packaged menu entry accepts a link (`roseblox URI`) but does not claim the schemes itself, so installing Roseblox never takes over Roblox links on its own.

Turning it off restores the previous default when Roseblox still owns it, and removes only Roseblox's own associations. Settings shows whether the registration is active and can repair it.

A link is checked before use and then passed to Roblox as described in [Starting Roblox](#starting-roblox).

## Update checks

The first **Notify me about … updates** switch turned on (there is one for Player and one for Studio) installs a small background checker, `roseblox --update-service`, which starts with your desktop session through `~/.config/autostart/io.github.roseblox.Roseblox.Updater.desktop`. It has no window and does not load GTK. The last one turned off removes it. It checks the installed versions of the apps whose switch is on (`update_apps` in `setup.json`; an app not listed there is checked) one minute after login and every three hours, and sends a desktop notification when a newer one is out.

**Update** opens Roseblox, installs the new version and makes it the preferred installation; older versions stay until you remove them. **Skip This Version** stays quiet until an even newer version appears, and an ignored notification comes back after a day. With **Auto updates** on for an app (`auto_updates` in `setup.json`), the checker itself downloads the new version through the same installer, sets it as preferred and sends an "updated" notification; one install per app runs at a time, and a version that failed falls back to the regular notification without being retried. Turning the switch off stops the checker and removes its login entry. Output from a window opened by a notification goes to `~/.local/state/roseblox/logs/roseblox.log`.

While the switch is on, the Roseblox window also checks when it opens and every three hours while it stays open.

## Performance settings

Player and Studio each have their own settings in `~/.config/roseblox/launch-config.json`. Every change is listed for review before it is saved, and the previous settings are backed up to `~/.config/roseblox/backups/` (the 30 newest are kept).

Safety rules:

- Variables that look like passwords, tokens or cookies are refused, as are library injection variables such as `LD_PRELOAD` and `DYLD_*`.
- Login ticket and authentication arguments are refused.
- Presets never touch networking, physics or security flags, and Roseblox never adds a FastFlag you did not choose.
- Nothing is set for your whole desktop; settings apply only to the Roblox process Roseblox starts.

The Player only obeys FastFlags on Roblox's allowlist, which Roseblox downloads from `https://clientsettingscdn.roblox.com/v2/settings/application/MacDesktopClient` and uses to mark each flag. Studio obeys every flag.

**Measure & compare** samples the running app's CPU and memory from `/proc`, and GPU load from `amdgpu` sysfs or `nvidia-smi` where available. Results are saved with the settings in use in `~/.local/state/roseblox/measurements.json` (the 50 newest). Roblox does not report its frame rate, so Roseblox does not estimate one; use the MangoHud overlay for FPS.

Diagnostics and the Performance page only show facts Roseblox read or checked.

## Mods

Mods replace media files inside `Contents/Resources` of managed installations, right before launch:

- Quick mods: a font, the arrow cursor, the classic death sound, and the classic avatar editor background.
- Your own files from `~/.local/share/roseblox/mods/player/` or `.../studio/`, placed at the same relative path as in `Contents/Resources`.

Only images, textures, sounds, fonts and meshes are used. Scripts, models, places and code are never replaced, and anything else in the folder is listed with the reason it was skipped. The first time a file is replaced, Roblox's original is saved in `.roseblox-originals` next to the bundle, and it is put back when the mod is turned off.

The avatar editor background comes from [Bloxstrap](https://github.com/bloxstraplabs/bloxstrap) (MIT). Roseblox downloads it from a fixed commit and uses it only if its SHA-256 matches the reviewed copy. Mods never download anything else.

## While Playing

Discord Rich Presence, the server location notification, closing Roblox when you leave a game and the microphone toggle are off by default. When one is on, each Player launch also starts `roseblox --activity-watch`, a small process without a window. It:

1. Follows the log file that this Player has open in the prefix's `~/Library/Logs/Roblox`, so several Players never mix up their games.
2. Looks up the game's name, creator and icon from Roblox's public APIs (`apis.roblox.com`, `games.roblox.com`, `thumbnails.roblox.com`).
3. Talks to the Discord desktop app over its local IPC socket.
4. Looks up the Roblox server's address (never yours) with [ipwho.is](https://ipwho.is) for the location notification. With **Show inside the game**, the launch sets `MANGOHUD=1` and `MANGOHUD_CONFIGFILE` to a Roseblox file in `$XDG_RUNTIME_DIR/roseblox/overlay/` (`backend/game_overlay.py`). MangoHud re-reads that file when it changes, so the watcher writes a one-line `custom_text` banner into it and, 10 seconds later, the resting config again: your own MangoHud config (the per-program file or `MangoHud.conf`, or your `MANGOHUD_CONFIGFILE`) when MangoHud was on for the launch, otherwise a hidden HUD. MangoHud's default layout ignores `custom_text`, which is why the banner briefly replaces your HUD instead of joining it. The file is deleted once the Player has exited, never earlier: without it MangoHud would fall back to its full default HUD.
5. Runs the microphone toggle, described below.
6. Exits when that Rosemary process exits.

Because it runs on its own, these features keep working after Roseblox closes. It logs to `~/.local/state/roseblox/logs/activity.log`.

### Microphone toggle

Roblox records through its own capture stream: a "source output" in PulseAudio terms, a `Stream/Input/Audio` node in PipeWire's. Every capture stream has its own mute, separate from the microphone itself. Roseblox mutes Roblox's stream, so Roblox gets digital silence while the device, Roblox's voice connection and every other app's microphone keep running.

- **Finding Roblox's stream.** The Mac Player names its stream with `application.id = "com.roblox.RobloxPlayer"`. Streams without an application ID are matched by `application.name = "Roblox"`. Discord and other apps are never touched.
- **Muting.** With `pactl` (PulseAudio, or PipeWire through pipewire-pulse): `pactl set-source-output-mute`. Without it: `pw-dump` to find the stream and `wpctl set-mute`.
- **New streams.** Roblox opens a new stream every time its microphone is switched on or off, often before closing the old one. Roseblox listens to `pactl subscribe` (or `pw-dump --monitor`) and gives every new stream the current state within milliseconds. Nothing is polled.
- **Leaving no mute behind.** PipeWire remembers a stream's mute for the app's next stream. When the Player closes, Roseblox unmutes Roblox's streams so the next session does not start silent.
- **The hotkey.** Roseblox asks the desktop for a global shortcut through the xdg-desktop-portal GlobalShortcuts interface (KDE Plasma, GNOME 48 and later, Hyprland). The desktop stores the key and reports both press and release, which Hold mode needs. The window and the watcher each register as `io.github.roseblox.Roseblox` first, so the desktop files the shortcut under Roseblox even when Roseblox was started from a terminal.
- **Sounds.** When the microphone opens or mutes, the gate plays a sound with `pw-play` (or `paplay`) without waiting for it (`backend/microphone_sounds.py`). Roseblox's own sounds are generated as small WAV files in `~/.local/share/roseblox/sounds/`, so none ship with it; a chosen sound is checked by its signature and copied there.
- **Several Players.** Each watcher queues for the session-bus name `io.github.roseblox.Roseblox.Microphone`. Only the owner mutes; when its Player closes, the next watcher takes over. `roseblox --mic` calls the owner over D-Bus.

## Importing from another app

`backend/importers/` has one module per app (`roseline.py`, `quadra.py`, `sober.py`, `vinegar.py`, `bloxstrap.py`, `fastflag_manager.py`, `roblox_manager.py`, and `client_settings.py` for a plain FastFlags file). Each one only knows its app's files, checked against that app's source code or documentation, and does two things: `detect` finds the files (the Windows apps through `wine.py`, which lists Wine, Bottles, Lutris, Steam and Grapejuice prefixes and the Windows users in each), and `read` turns them into an `ImportResult`. Files are opened read-only, at most 4 MB each, and nothing is started or installed.

An `ImportResult` holds:

- **Items**: settings already in Roseblox's form, for the Player, Studio or Roseblox itself. They go through the same checks as the rest of Roseblox (`normalize_flag_value`, the environment and GPU checks) before they become items.
- **Problems**: everything else, marked skipped, unsupported or invalid with a reason. Unknown keys are listed by name only; their values are never shown.

`planner.py` compares the items with the saved settings. A setting is new when Roseblox still has its default, the same when the values match, and a replacement otherwise; replacements start unticked. `apply` builds every changed configuration in memory and validates all of them (including the 500-flag limit and the font file), then writes through `LaunchConfigStore` (which keeps a backup), `ModsStore` and `SetupStore`. Running the same import again finds everything the same and writes nothing.

To support another app, add a module with an `IMPORTER` and list it in `importers/__init__.py`.

## Where Roseblox keeps things

The paths below use the usual defaults; Roseblox follows `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME` and `XDG_CACHE_HOME` when they are set.

| Path | What |
| --- | --- |
| `~/.config/roseblox/settings.json` | Rosemary executable chosen in Settings |
| `~/.config/roseblox/setup.json` | First-time setup and Settings choices, such as Appearance and After Launching |
| `~/.config/roseblox/preferences.json` | Preferred Player and Studio installation |
| `~/.config/roseblox/launch-config.json` | Performance settings |
| `~/.config/roseblox/backups/` | Performance settings backups |
| `~/.config/roseblox/mods.json` | Mods choices |
| `~/.config/roseblox/version_sources.json` | Custom version sources |
| `~/.config/autostart/io.github.roseblox.Roseblox.Updater.desktop` | Update checker login entry, while it is on |
| `~/.local/share/roseblox/library.json` | Installed Player and Studio apps |
| `~/.local/share/roseblox/versions.json` | Cached version checks |
| `~/.local/share/roseblox/installations/` | Managed Roblox apps |
| `~/.local/share/roseblox/archives/` | Downloaded Roblox archives |
| `~/.local/share/roseblox/.staging/downloads/` and `downloads.json` | Partial downloads |
| `~/.local/share/roseblox/mods/` | Your mods folder |
| `~/.local/share/roseblox/sounds/` | Microphone sounds, Roseblox's and the ones you chose |
| `~/.local/state/roseblox/logs/` | Roblox, Roseblox and activity logs |
| `~/.local/state/roseblox/measurements.json` | Measure & compare results |
| `~/.local/state/roseblox/update-notifications.json` | Skipped and notified Roblox versions |
| `~/.cache/roseblox/` | Roblox's FastFlag allowlist and the reviewed avatar editor background |

Uninstalling the package leaves all of these, including downloaded Roblox apps, in place. [Uninstall](INSTALL.md#uninstall) shows how to remove them.

## How Roseblox itself is installed

- **Arch packages** (`packaging/arch` for tagged releases, `packaging/arch-git` for `master`) build a Python wheel and install it into the system Python, with the menu entry, icon and license under `/usr/share`.
- **The install script** (`packaging/generic/install.sh`) is for other distributions. It copies `src/roseblox` to `~/.local/lib/roseblox` and writes a small `~/.local/bin/roseblox` launcher. The launcher runs the system `python3`, so the system's PyGObject, GTK and libadwaita are used, and it puts the app folder on `PYTHONPATH`, so the background processes started with `python3 -m roseblox` find it too. The menu entry points at the launcher by its full path. A marker file in `~/.local/lib/roseblox` lets `--uninstall` and later updates replace only what the script installed. Nothing is installed system-wide and `sudo` is never used.

- **The .deb** (`packaging/debian/build-deb.sh`) puts the same files into the system Python's `dist-packages` with a `/usr/bin/roseblox` launcher, and depends on Debian's PyGObject, GTK 4 and libadwaita packages.
- **The AppImage** (`packaging/appimage/build_appimage.py`) is built on Ubuntu 24.04 and carries that system's Python, PyGObject, GTK 4, libadwaita, `bsdtar` and every library they need, except the ones that must match the computer it runs on: glibc, the graphics drivers and what they load, X11 and Wayland client libraries, and fontconfig. Its `AppRun` points the dynamic loader, Python and GLib at the bundled files and saves the previous values. Once GTK has started, Roseblox puts those values back (`backend/appimage.py`), so the programs it starts, such as Rosemary, `xdg-open` and your browser, never load the bundled libraries. An AppImage's files disappear when the process that mounted it exits, so the activity watcher, the update checker's login entry and Roblox link registration start the AppImage file itself (`$APPIMAGE`), not the Python inside it. On each start it writes its menu entry and icon to `~/.local/share` with the AppImage's current path, unless another installation's menu entry is already there.

Roblox link registration and the update checker's login entry use the `roseblox` command found in `PATH` (the AppImage file for the AppImage), so `~/.local/bin` must be in `PATH` for those two features with the install script.

### Updating Roseblox itself

Once a day while the window is open (and when you click the refresh button in the bell), Roseblox reads `https://api.github.com/repos/MadnessMyth/RoseBlox/releases/latest` and compares its version tag with its own (`backend/self_update.py`). If the release is newer, the bell offers it. How it updates depends on how Roseblox was installed, which it works out from where its own code is: inside an AppImage, next to the install script's marker file, in `/usr` with dpkg's file list for `roseblox`, or elsewhere in `/usr` (the Arch packages).

Only the AppImage updates itself. Roseblox downloads `Roseblox-x86_64.AppImage` and `SHA256SUMS` from the release, writes the download to a temporary file next to the current AppImage, and replaces the current file only if the SHA-256 checksum matches. The running copy keeps working from its mounted image until **Restart**. The release workflow builds both files, plus the `.deb` and a `.zsync` file for AppImage tools such as AppImageUpdate.

## Code layout

- `src/roseblox/backend/`: everything that is not the interface, such as versions, downloads, installing, the library, launching Rosemary, launch settings, mods, URL handlers and Discord. It does not depend on GTK pages.
- `src/roseblox/pages/` and `window.py`: the GTK 4 and libadwaita interface.
- `src/roseblox/update_service.py` and `activity_service.py`: the two background processes. The update checker stays free of GTK imports.
- `src/roseblox/theme.py` and `resources/style.css`: colors and styling.
- `tests/`: unit and GTK tests.

[AGENTS.md](../AGENTS.md) lists the rules every change must follow.
