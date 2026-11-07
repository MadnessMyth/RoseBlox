# Roseblox release preparation

## Release checklist — v0.4.0

- [x] Bump the version in `pyproject.toml`, `src/roseblox/__init__.py`, and `packaging/arch-git/PKGBUILD`.
- [x] Run the full unittest suite, Python compilation, and `git diff --check`, on the host and on Arch Linux.
- [ ] Build and install `roseblox-git` on Arch Linux and check the activity watcher entry point and Diagnostics against the real Rosemary 0.5.0.
- [ ] Create the annotated `v0.4.0` Git tag on the release commit.
- [ ] Point `packaging/arch/PKGBUILD` at the `v0.4.0` tag, record the archive checksum, and regenerate `.SRCINFO`.
- [ ] Build `packaging/arch/PKGBUILD` with `makepkg` on Arch Linux and install it.
- [ ] Create the GitHub release for `v0.4.0` from these notes.

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
- [ ] Create the GitHub release for `v0.3.0` from these notes.

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
