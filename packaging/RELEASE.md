# Roseblox release preparation

## Release checklist — v0.2.2

- [x] Bump the version in `pyproject.toml`, `src/roseblox/__init__.py`, and `packaging/arch-git/PKGBUILD`.
- [x] Run the full unittest suite, Python compilation, and `git diff --check`.
- [x] Test close-after-launch and the update checker with the installed `roseblox-git` package on Arch Linux, against a desktop notification server.
- [ ] Create the annotated `v0.2.2` Git tag on the release commit.
- [ ] Point `packaging/arch/PKGBUILD` at the `v0.2.2` tag, record the archive checksum, and regenerate `.SRCINFO`.
- [ ] Build `packaging/arch/PKGBUILD` with `makepkg` on Arch Linux and install it.
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
