# Roseblox release preparation

## Release checklist — v0.7.0

- [x] Bump the version in `pyproject.toml`, `src/roseblox/__init__.py`, and `packaging/arch-git/PKGBUILD`.
- [x] Run the full unittest suite, Python compilation, and `git diff --check` on Ubuntu 24.04 (GTK 4.14, libadwaita 1.5).
- [x] Regenerate `packaging/arch-git/.SRCINFO`, which was missing `gtk4-layer-shell`, and test that both `.SRCINFO` files match their `PKGBUILD`.
- [x] Grey out the toast without the Python cairo bindings, which Arch's `python-gobject` and `gtk4-layer-shell` do not pull in, and list `python-cairo` in `packaging/arch-git`.
- [ ] Run the unittest suite, build `packaging/arch-git` and `packaging/arch` with `makepkg` on Arch Linux, and check both with `namcap`.
- [x] Create the annotated `v0.7.0` Git tag on the release commit.
- [ ] Point `packaging/arch/PKGBUILD` at the `v0.7.0` tag, record the archive checksum, add the `libpulse`, `gtk4-layer-shell` and `python-cairo` optional dependencies, and update `.SRCINFO`.
- [ ] The release workflow builds the `.deb` and the AppImage, and creates the GitHub release for `v0.7.0` from these notes with both attached, when the tag is pushed.
- [ ] Start the released AppImage under Xvfb, and install the released `.deb` on Ubuntu 24.04 and start it.
- [ ] Try the microphone toggle, the in-game banner, the ping row and the toast with the current Rosemary on KDE Plasma.

## Release notes — v0.7.0

### New

- **Microphone toggle** (Settings → While Playing, off by default). Roblox's microphone stays silent until you use a hotkey, like push to talk in other games. Roblox stays connected to voice chat and just receives silence; Discord and other apps keep your microphone as usual.
  - **Set Hotkey** lets you choose the key in the dialog your desktop shows (KDE Plasma does this). On desktops without global shortcuts, bind a key to `roseblox --mic toggle` in the keyboard settings instead.
  - **Toggle** or **Hold** mode. **Inverted** turns Hold into push to mute, and **Release delay** keeps the microphone on for a moment after you let go.
  - **Sound effects** when the microphone turns on and off, with your own WAV, Ogg or FLAC file if you like, and a volume.
  - `roseblox --mic status`, `toggle`, `on` and `off` work while a Player runs with the toggle on.
  - It works with PipeWire and PulseAudio and needs `pactl` (from `libpulse`), or PipeWire's `pw-dump` and `wpctl`. Settings shows a warning when they are missing.
- **Import from Another App** (Settings → Rosemary, and an optional fifth step in first-time setup). It brings FastFlags and settings over from Roséline, Quadra, Sober, Vinegar, Bloxstrap, Roblox FastFlag Manager and Roblox Manager, or from any `ClientAppSettings.json`-style file. A preview shows what carries over and what is invalid, unsupported or skipped, and nothing is saved until you click **Import**. The other app's files are only read, and sign-in data is never read.
- **The server location inside the game.** With **Show inside the game** (on by default), the location appears as "Server: Frankfurt am Main, Hessen, Germany" at the top of the Roblox window for 10 seconds, fullscreen or not, instead of a notification. MangoHud draws it, so it needs the `mangohud` package; without it you get the notification as before. Once a Player opens one of Roblox's web pages, such as a game's server list, MangoHud takes no more changes in that Player, so the location comes as a notification or toast until the Player restarts.
- **Server ping** adds the round trip to the server to the message: "The server is in Frankfurt am Main, Hessen, Germany (24 ms)".
  - **Constant ping overlay (every 30s)** keeps a `PING 24 ms` row on screen while you are in a game. It is the first row of your MangoHud overlay, or on its own at the top left when the **MangoHud overlay** switch in Performance is off.
  - **Yellow ping row** makes that row yellow inside your MangoHud overlay too.
- **Show as a toast above the game** (off by default) shows the server location in a small banner at the top of the screen for 8 seconds, above fullscreen games, on KDE Plasma and on wlroots desktops such as Hyprland and sway. Clicks go through it. It needs a Wayland session, `gtk4-layer-shell` and the Python cairo bindings, and is not available in the AppImage. Where it cannot work, the switch is greyed out and hovering over it says why.
- **Roblox updates for each app.** Settings → Updates has a **Notify me about … updates** switch for Roblox Player and one for Roblox Studio. Under each is **Automatically update … in the background**, which installs new versions without asking and tells you when it is done.
- **Playtime**: the Player and Studio pages show how long you have played that app from Roseblox, in how many sessions and when you last played, with **Reset**.
- **Search settings** at the top of Settings (Ctrl+F) lists the settings whose name, description or section match what you type, including ones that only appear once another setting is on.

### Fixed

- The server location notification was often hidden during fullscreen games. It is now a critical notification, so KDE Plasma and GNOME show it over a fullscreen game and in Do Not Disturb mode. Roseblox closes it after 8 seconds, and a newer location replaces it instead of adding another.
- Some joins got no Discord presence or server location: after a teleport or a join from inside a game, when a Player was started right after another one closed, and for the rest of a launch after an unexpected error. `~/.local/state/roseblox/logs/activity.log` now has the time on each line and says which Roblox log it follows, when each join starts and finishes, and when no log was found.

### Not changed

- Settings, installations and Roblox links carry over from 0.6.0.
- Nothing new needs installing. The new optional packages are `libpulse` for the microphone toggle, and `gtk4-layer-shell` and `python-cairo` for the toast; on Debian and Ubuntu they are `pulseaudio-utils`, `gir1.2-gtk4layershell-1.0` and `python3-gi-cairo`.

## Release checklist — v0.6.0

- [x] Bump the version in `pyproject.toml`, `src/roseblox/__init__.py`, and `packaging/arch-git/PKGBUILD`.
- [x] Run the full unittest suite, Python compilation, and `git diff --check` on Ubuntu 24.04 (GTK 4.14, libadwaita 1.5).
- [x] Build the AppImage on Ubuntu 24.04, start it under Xvfb, check that only system-matched libraries (glibc, graphics drivers, X11/Wayland clients, fontconfig) come from the host, that the menu entry and icon are written, and that programs it starts get the host environment back.
- [x] Replace a test AppImage through the self-update download with a matching and a mismatching checksum.
- [x] Build the `.deb`, install it on Ubuntu 24.04, start the installed app, and remove it.
- [ ] Start the AppImage on Fedora and openSUSE.
- [ ] Run two Players at once with **Allow more than one Roblox Player** and the current Rosemary.
- [ ] Run the unittest suite and build `packaging/arch-git` with `makepkg` on Arch Linux.
- [x] Create the annotated `v0.6.0` Git tag on the release commit.
- [x] Point `packaging/arch/PKGBUILD` at the `v0.6.0` tag, record the archive checksum, and update `.SRCINFO`.
- [x] The release workflow builds the `.deb` and the AppImage, and creates the GitHub release for `v0.6.0` from these notes with both attached, when the tag is pushed.

## Release notes — v0.6.0

### New

- **Download Roseblox from the release page.** Each release now includes:
  - `Roseblox-x86_64.AppImage`, which runs on Ubuntu 24.04 or newer, Debian 13, Fedora 40 or newer, openSUSE Tumbleweed and other systems at least as new. It contains Python, GTK 4 and libadwaita, so nothing else needs installing. Make it executable and open it. On first start it adds Roseblox to your application menu with its icon.
  - `roseblox_0.6.0_all.deb` for Ubuntu 24.04 or newer and Debian 13. Install it with `sudo apt install ./roseblox_0.6.0_all.deb`.
  - `SHA256SUMS` with the checksums of both.

  See [Other distributions](https://github.com/MadnessMyth/RoseBlox/blob/master/docs/INSTALL.md#other-distributions). Neither one includes Rosemary; setup still gets it from Rosemary's own releases.
- **Roseblox tells you about new versions of itself.** Once a day while it is open, the bell shows **Roseblox X.Y.Z is available**:
  - The AppImage updates itself: **Update** downloads the new AppImage, checks it against `SHA256SUMS`, replaces the file and offers **Restart**.
  - Other installs get a link to the release page or the update command.
  - Turn this off in **Settings → Updates → Check for new Roseblox versions**.
- Roseblox shows its own icon in docks and taskbars, and its menu entry is found by searching for "Roblox", "Player", "Studio" or "Launcher".

### Fixed

- **Allow more than one Roblox Player** works again with the current Rosemary. Rosemary now refuses a second instance of any app whose `Info.plist` sets `LSMultipleInstancesProhibited`, as Roblox Player does. That applies even with `--new-instance` and even to a duplicated copy. With the setting on, Roseblox now starts Player through the executable inside the app bundle, which Rosemary always allows.

### Not changed

- Settings, installations and Roblox links carry over from 0.5.0. Arch packages and the install script work as before.

## Release checklist — v0.5.0

- [x] Bump the version in `pyproject.toml`, `src/roseblox/__init__.py`, and `packaging/arch-git/PKGBUILD`.
- [x] Run the full unittest suite, Python compilation, and `git diff --check` on Ubuntu 24.04 (GTK 4.14, libadwaita 1.5).
- [x] Install with `packaging/generic/install.sh` on Ubuntu 24.04, start the installed app, and check the AppImage setup flow with a placeholder AppImage.
- [ ] Run the unittest suite and build `packaging/arch-git` with `makepkg` on Arch Linux.
- [x] Create the annotated `v0.5.0` Git tag on the release commit.
- [x] Point `packaging/arch/PKGBUILD` at the `v0.5.0` tag, record the archive checksum, and update `.SRCINFO`.
- [ ] Build `packaging/arch/PKGBUILD` with `makepkg` on Arch Linux and install it.
- [x] The release workflow creates the GitHub release for `v0.5.0` from these notes when the tag is pushed.

## Release notes — v0.5.0

### New

- **Roseblox now runs on more Linux distributions**: Ubuntu 24.04 or newer, Debian 13, Fedora 40 or newer, and openSUSE Tumbleweed, alongside Arch Linux and Arch-based systems. These are new and less tested than Arch; please open an issue if something does not work.
  - `packaging/generic/install.sh` installs Roseblox for your user in `~/.local`, using your system's Python, GTK 4 and libadwaita, with the menu entry and icon. It checks what is missing and prints the exact install command for your distribution, never uses `sudo`, and removes itself with `--uninstall`. See [Other distributions](https://github.com/MadnessMyth/RoseBlox/blob/master/docs/INSTALL.md#other-distributions).
  - **Setup gets Rosemary from its official AppImage** on systems other than Arch. **Open Releases Page** opens Rosemary's releases page in your browser, and Roseblox finds the `Rosemary-…-x86_64.AppImage` you download in your Downloads folder, `~/Applications`, `~/.local/bin` or `~/bin`. **Use This File** makes it executable and selects it. Roseblox still never downloads Rosemary itself. On Arch, setup installs Rosemary from the AUR as before.
  - Messages about a missing tool (`bsdtar`, `xdg-mime`, GameMode, MangoHud) name the install command for your package manager: pacman, apt, dnf or zypper.
  - **Diagnostics** shows your distribution and CPU architecture.
- **Choose…** for Rosemary accepts a downloaded AppImage that is not executable yet, and makes it executable.
- New documentation: [How Roseblox works](https://github.com/MadnessMyth/RoseBlox/blob/master/docs/HOW-IT-WORKS.md) explains the launch flow, where Roblox and Rosemary come from, every file Roseblox stores, and how each feature works. The README is rewritten for people new to Roseblox, with a sneak peek video.

### Not changed

- Arch Linux packages, settings, installations and the AUR-based Rosemary setup work as in 0.4.2.
- Flatpak is still not provided; [DISTRIBUTION.md](https://github.com/MadnessMyth/RoseBlox/blob/master/packaging/DISTRIBUTION.md) explains why.

## Release checklist — v0.4.2

- [x] Bump the version in `pyproject.toml`, `src/roseblox/__init__.py`, and `packaging/arch-git/PKGBUILD`.
- [x] Run the full unittest suite, Python compilation, and `git diff --check`, on the host and on Arch Linux.
- [x] Create the annotated `v0.4.2` Git tag on the release commit.
- [x] Point `packaging/arch/PKGBUILD` at the `v0.4.2` tag, record the archive checksum, and regenerate `.SRCINFO`.
- [x] Build `packaging/arch/PKGBUILD` with `makepkg` on Arch Linux and install it.
- [x] Create the GitHub release for `v0.4.2` from these notes (the release workflow does this when the tag is pushed).

## Release notes — v0.4.2

### Fixed

- **Discord Rich Presence and the server location notification now work.** The Mac Player writes the lines that say you joined a game to its own log in the prefix (`~/Library/Logs/Roblox` in the guest), not to Rosemary's output, so Roseblox never saw a game start. Roseblox now follows the log file the Player has open, and `~/.local/state/roseblox/logs/activity.log` records which log it follows and each game you join.
- Starting a Player while another Player installation was running moved the first Player's `player.log` aside, so its presence stopped updating. Each running Player now keeps its own log (`player.log`, `player-2.log`, …), even across Roseblox restarts.

### New

- **Settings → After Launching → Allow more than one Roblox Player** (off by default) adds **Duplicate…** to Player installations. Roblox shows the window that is already open when the same installation is launched twice, so a second Player runs from a copy: Duplicate copies the app in the background with progress and **Cancel Copy**, checks free space first, and adds nothing if the copy fails. Each copy has its own **Launch** and **Stop** and can sign in to its own account. With the setting on, a Roblox link opened while Player runs goes to an installation that is not running. This is new and lightly tested; please open an issue with `activity.log` and the `player*.log` files if a copy does not start its own Player.

## Release checklist — v0.4.1

- [x] Bump the version in `pyproject.toml`, `src/roseblox/__init__.py`, and `packaging/arch-git/PKGBUILD`.
- [x] Run the full unittest suite, Python compilation, and `git diff --check`, on the host and on Arch Linux.
- [x] Create the annotated `v0.4.1` Git tag on the release commit.
- [x] Point `packaging/arch/PKGBUILD` at the `v0.4.1` tag, record the archive checksum, and regenerate `.SRCINFO`.
- [x] Build `packaging/arch/PKGBUILD` with `makepkg` on Arch Linux and install it.
- [x] Create the GitHub release for `v0.4.1` from these notes.

## Release notes — v0.4.1

### New

- **Performance → Measure & compare**: measure the running Player or Studio for 30 seconds to 5 minutes. It records CPU use, the busiest thread and memory, and GPU load and memory where the driver reports them (AMD `amdgpu`, or NVIDIA `nvidia-smi`). Each measurement is saved with the settings in use and your notes, and two can be compared side by side. Roblox does not report its frame rate, so use the MangoHud overlay for FPS.
- **Performance → Studio startup → Don't load my plugins** starts Studio with `-disableLoadUserPlugins`.
- **Performance → Backups**: **Back Up Now**, and **Export…** and **Import…** for the whole Player and Studio configuration. An imported file is loaded as unapplied changes for you to review.
- **Diagnostics → Open Logs** opens the folder with Rosemary's output from the last two runs.

## Release checklist — v0.4.0

- [x] Bump the version in `pyproject.toml`, `src/roseblox/__init__.py`, and `packaging/arch-git/PKGBUILD`.
- [x] Run the full unittest suite, Python compilation, and `git diff --check`, on the host and on Arch Linux.
- [x] Build and install `roseblox-git` on Arch Linux and check the activity watcher entry point and Diagnostics against the real Rosemary 0.5.0.
- [x] Create the annotated `v0.4.0` Git tag on the release commit.
- [x] Point `packaging/arch/PKGBUILD` at the `v0.4.0` tag, record the archive checksum, and regenerate `.SRCINFO`.
- [x] Build `packaging/arch/PKGBUILD` with `makepkg` on Arch Linux and install it.
- [x] Create the GitHub release for `v0.4.0` from these notes.

## Release notes — v0.4.0

### New

- **Settings → While Playing** (Player), all off by default:
  - **Discord Rich Presence**: the game's name, creator and icon, and how long you have been playing, with **Join Server** and **View Game** buttons. Join Server is hidden in private servers and can be turned off. It uses Roseblox's own Discord application; Settings → Advanced can use a different one.
  - **Server location**: a notification with the server's city, region and country when you join a game, looked up with ipwho.is.
  - **Close Roblox when leaving a game**: Roblox closes when you go back to its home screen.
- **Mods** page, with separate mods for Player and Studio:
  - A **custom font** for all of Roblox's interface text, and a **custom mouse cursor**.
  - The **classic "oof" death sound** and the **classic avatar editor background** (Player).
  - A **mods folder** whose files replace the matching files in Roblox's content. Refused files are listed with the reason.
  - Mods are applied when Roblox launches, and the original files are kept and put back when a mod is removed.
- **Open Place…** on Studio installations opens a `.rbxl` or `.rbxlx` file directly in Studio.
- **Diagnostics** includes `rosemary prefix info`: the Rosemary version that made the prefix and its base system.

### Safety

- While Playing reads only the Roblox output Roseblox already saves in `player.log`. It never reads Roblox's memory, cookies or login, and uses only public Roblox web APIs that need no account.
- Close on leave signals only the Roblox process Roseblox started, after checking it is still the same process.
- Mods replace only images, sounds, fonts, meshes and font family files inside the installation. Scripts and other files are refused, and no Roblox files outside the installation are changed. The avatar editor background is downloaded from a pinned commit and checked against a fixed SHA-256.

## Release checklist — v0.3.0

- [x] Bump the version in `pyproject.toml`, `src/roseblox/__init__.py`, and `packaging/arch-git/PKGBUILD`.
- [x] Run the full unittest suite, Python compilation, and `git diff --check`.
- [x] Launch Player through the installed `roseblox-git` package and the real Rosemary 0.5.0 on Arch Linux, and check that FastFlags, environment variables, MangoHud and launch arguments reach Rosemary.
- [x] Create the annotated `v0.3.0` Git tag on the release commit.
- [x] Point `packaging/arch/PKGBUILD` at the `v0.3.0` tag, record the archive checksum, and regenerate `.SRCINFO`.
- [x] Build `packaging/arch/PKGBUILD` with `makepkg` on Arch Linux and install it.
- [x] Create the GitHub release for `v0.3.0` from these notes.

## Release notes — v0.3.0

### New

- **Performance** page, with separate settings for Player and Studio. Every change is listed for review before it is saved, and the previous settings are kept as a backup you can restore.
  - **Presets**: Balanced, Performance and Visual Quality. A preset changes only the settings it lists.
  - **GPU** choice on computers with more than one graphics card.
  - **Presentation mode** (Mailbox, FIFO, FIFO relaxed, Immediate) and **refresh rate**. Roblox's in-game Maximum Frame Rate menu follows the refresh rate, and the page shows what that menu is set to.
  - **GameMode** (Feral `gamemoded`) and the **MangoHud overlay**, when they are installed.
  - **Roblox graphics**: anti-aliasing, texture quality, graphics quality level, hide grass, gray sky and DPI scaling, using flags from Roblox's allowlist.
  - **FastFlags** editor. Player flags are checked against the allowlist Roblox publishes, with import, export, and copying the flags already in an installation.
  - **Keyboard & window**: which key acts as Command (including Ctrl for copy, paste and undo in Studio), and Rosemary's or your desktop's titlebar.
  - **Environment** variables for Rosemary and for Roblox, and extra **launch arguments**, with a preview of the command.
  - **Caches**: Rosemary's shader cache and Roblox's caches with their sizes, moved to the trash when cleared.
  - **Diagnostics**: Rosemary version, GPUs, Vulkan drivers, GameMode, MangoHud and system details, with **Copy Report**.
- The Arch packages list `gamemode` and `mangohud` as optional dependencies.

### Fixed

- Launch arguments such as `-n` were read by Rosemary itself instead of reaching Roblox. Arguments for Roblox now always follow `--`.

### Safety

- Variables that look like passwords, tokens or cookies, library injection variables (`LD_PRELOAD`, `DYLD_*` and similar), and login arguments are refused.
- Roseblox never adds FastFlags you did not choose, and presets never touch networking, physics or security flags. A `ClientAppSettings.json` that Roseblox did not write is backed up before it is replaced.
- Cache cleanup never touches your Roblox login, cookies or settings.

## Release checklist — v0.2.3

- [x] Bump the version in `pyproject.toml`, `src/roseblox/__init__.py`, and `packaging/arch-git/PKGBUILD`.
- [x] Run the full unittest suite, Python compilation, and `git diff --check`.
- [x] Check every Appearance choice, the notification center, and a real download on Arch Linux.
- [x] Create the annotated `v0.2.3` Git tag on the release commit.
- [x] Point `packaging/arch/PKGBUILD` at the `v0.2.3` tag, record the archive checksum, and regenerate `.SRCINFO`.
- [x] Build `packaging/arch/PKGBUILD` with `makepkg` on Arch Linux and install it.
- [ ] Create the GitHub release for `v0.2.3` from these notes.

## Release notes — v0.2.3

### New

- **Settings** is grouped into sections that open and close: Appearance, Rosemary, After Launching, Roblox Links, Updates, and Advanced. Advanced (Rosemary executable path and version sources) starts closed. Roseblox remembers which sections are open.
- **Appearance**: choose Roseblox or System colors, a Dark, Light, or Follow system style, and an accent color (Roseblox pink, your desktop's accent color, or one of nine fixed colors).
- **Ctrl + left-click compatibility** is now a switch you can turn off. It is on by default, and greyed out when your Rosemary build does not support it.
- **Notification center**: a bell at the top right, with a badge that counts its items. It shows downloads in progress (with Cancel), available Player and Studio updates (with Update and Skip), and recent finished or failed installs.
- Download bars show speed and time left, for example `37% · 12.4 MiB/s · 1 min 20 s left`.

### Fixed

- The "ready" tick next to Rosemary showed as a broken icon with current Adwaita icon themes.

## Release checklist — v0.2.2

- [x] Bump the version in `pyproject.toml`, `src/roseblox/__init__.py`, and `packaging/arch-git/PKGBUILD`.
- [x] Run the full unittest suite, Python compilation, and `git diff --check`.
- [x] Test close-after-launch and the update checker with the installed `roseblox-git` package on Arch Linux, against a desktop notification server.
- [x] Create the annotated `v0.2.2` Git tag on the release commit.
- [x] Point `packaging/arch/PKGBUILD` at the `v0.2.2` tag, record the archive checksum, and regenerate `.SRCINFO`.
- [x] Build `packaging/arch/PKGBUILD` with `makepkg` on Arch Linux and install it.
- [ ] Create the GitHub release for `v0.2.2` from these notes.

## Release notes — v0.2.2

### New

- **Settings → After Launching** has separate **Close after opening Roblox Player** and **Close after opening Roblox Studio** switches, each with **Only when opened from a link**. Roseblox closes five seconds after the app starts, and stays open if the app exits in that time or a download is running. Roblox keeps running after Roseblox closes.
- **Roblox update notifications** (Settings → Updates, and the new optional step 4 of setup). A background checker starts when you log in, checks one minute later and every three hours, and notifies you about new Player and Studio versions. **Update** installs the new version with progress and makes it the preferred installation; **Skip This Version** stays quiet until the next version. Settings also has **Check Now** and **Ask Again** for skipped versions.
- Rosemary's output is saved to `~/.local/state/roseblox/logs/player.log` and `studio.log`, keeping the previous run as `.log.1`.

### Fixed

- After installing a new Roblox version, Home kept launching the first installation unless you had set a preferred one. Updating from a notification now makes the new version preferred.
- The **Open Roblox links with Roseblox** switches showed a grey track after being turned on.

## Release checklist — v0.2.1

- [x] Bump the version in `pyproject.toml`, `src/roseblox/__init__.py`, and `packaging/arch-git/PKGBUILD`.
- [x] Run the full unittest suite, Python compilation, and `git diff --check`.
- [x] Create the annotated `v0.2.1` Git tag on the release commit.
- [x] Point `packaging/arch/PKGBUILD` at the `v0.2.1` tag, record the archive and desktop entry checksums, and regenerate `.SRCINFO`.
- [x] Build `packaging/arch/PKGBUILD` with `makepkg` on Arch Linux and install it.
- [ ] Create the GitHub release for `v0.2.1` from these notes.

## Release notes — v0.2.1

### Fixed

- Choosing Roseblox in a browser's or KDE Plasma's "Choose application" window opened only the launcher instead of joining the game. The menu entry now passes the link, and `roseblox URI` is accepted as well as `roseblox --roblox-url URI`.
- Roseblox was missing from those windows. While **Open Roblox links with Roseblox** is on, Roseblox is now listed next to other Roblox launchers; while it is off, installing Roseblox still never takes over Roblox links.
- Turning link handling on or off now rebuilds the desktop caches that KDE Plasma and other launchers read, so the change applies immediately.
- The link handler entry is named "Roseblox" and uses the Roseblox icon.

### Upgrading

If link handling was on in v0.2.0, Settings shows that it needs attention after the update. Click **Repair** once.

## Release checklist — v0.2.0

- [x] Bump the version in `pyproject.toml`, `src/roseblox/__init__.py`, and `packaging/arch-git/PKGBUILD`.
- [x] Review the release notes and the installation guide in `docs/INSTALL.md`.
- [x] Run the full unittest suite, Python compilation, and `git diff --check`.
- [x] Create the annotated `v0.2.0` Git tag on the release commit.
- [x] Point `packaging/arch/PKGBUILD` at the `v0.2.0` tag, record the archive checksum, and regenerate `.SRCINFO` with `makepkg --printsrcinfo`. The checksum is committed after the tag because the tagged archive cannot contain its own checksum.
- [x] Build `packaging/arch/PKGBUILD` and `packaging/arch-git/PKGBUILD` with `makepkg` on Arch Linux and install each package.
- [ ] Create the GitHub release for `v0.2.0` from these notes.
- [ ] Manually launch the installed package from a desktop menu and verify Player and Studio through Rosemary on hardware with a Vulkan driver.
- [ ] Publish the AUR recipe manually after the release metadata is complete.

## Release notes — v0.2.0

### New

- First-time setup opens on first launch with three steps: Rosemary, Roblox links, and Player and Studio. Each step shows whether it is required, optional, installing, or ready, and setup can be reopened from Settings.
- **Install Rosemary** opens a terminal that runs `paru` or `yay` with `-S --needed rosemary sdl3 vulkan-icd-loader`, then `rosemary prefix create`. Roseblox detects the new installation automatically. An existing Rosemary executable or AppImage can be chosen instead.
- Roblox link handling (`roblox`, `roblox-player`, `roblox-studio`, `roblox-studio-auth`) can be turned on or off in Settings and during setup, with a Repair action. Turning it off restores the previous handler.
- Player and Studio can be installed directly from setup, with progress and Cancel.
- Settings has a single Rosemary section for status, installation, the executable, and Ctrl + left-click compatibility.
- Home shows a banner with a shortcut to setup when Rosemary is missing.
- The `roseblox-git` recipe in `packaging/arch-git/` builds the latest `master`, and `docs/INSTALL.md` covers installing, updating, and uninstalling.

### Fixed

- Roblox links opened only the main window: command-line arguments were not passed to the application, so `--roblox-url` was ignored.
- Launching through a Rosemary release build (AppImage or extracted tree) was reported as failed because those builds re-execute through the ELF loader.
- Ctrl + left-click support was not detected in Rosemary launcher builds.
- A startup error could leave every page on "Loading registered installations…".
- A Roblox link received while the library was still loading was dropped.
- Turning off link handling failed when no other application had handled Roblox links before.
- Invalid command-line arguments caused an exception instead of a usage message.
- Lists and dialogs rendered with light colors on light desktop themes.

### Requirements and limitations

- Automatic Rosemary installation needs an Arch-based system with `paru` or `yay` and a terminal emulator. On other systems, install Rosemary from its documentation and choose the executable in Settings.
- Roblox needs a Vulkan driver for the GPU, such as `vulkan-radeon`, `vulkan-intel`, or `nvidia-utils`.
- Archive extraction uses the system `bsdtar` command from `libarchive`.
- Roseblox is licensed under the MIT License; see `LICENSE` in the source repository.

## Release notes — v0.1.0

Roseblox is a native GTK4/libadwaita desktop manager for Roblox Player and Roblox Studio on Linux. It launches both applications through an existing Rosemary installation; Rosemary is not included.

### Included

- Register existing Player and Studio application bundles without copying them.
- Import ZIP archives or download and install the current Player or Studio archive.
- Preserve managed installations, archives, and external bundles according to their ownership.
- Keep multiple installations in a persistent Library and choose preferred Player and Studio launch targets.
- Resume eligible interrupted downloads and validate archive paths and symbolic links before extraction.
- Configure independent version endpoints and JSON Pointer fields for Player and Studio.

### Requirements and limitations

- Rosemary must already be installed and available to the user. Roseblox does not install or bundle it.
- Archive extraction uses the system `bsdtar` command from `libarchive`.
- Player and Studio bundles are obtained through the established Roblox macOS archive endpoints and launched through Rosemary.
- Roseblox is licensed under the MIT License; see `LICENSE` in the source repository.
