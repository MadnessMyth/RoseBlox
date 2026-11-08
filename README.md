# Roseblox

**Play Roblox on Linux.** Roseblox is a desktop app that installs, updates and launches Roblox Player and Roblox Studio on Linux, with a normal app window and menu entry, not a terminal script.

[![Roseblox sneak peek: launching Roblox, performance options, mods and side-by-side Players](marketing/video/roseblox-sneak-peek-preview.gif)](marketing/video/roseblox-sneak-peek.mp4)

Click the preview to watch the full 30-second video with sound.

## How it works, in short

Roblox does not make a Linux version, but it does make a macOS one. Roseblox downloads the official macOS Roblox apps straight from Roblox's own servers and runs them through [Rosemary](https://github.com/microlift/rosemary), a separate open-source program that runs macOS apps on Linux, translating Roblox's Metal graphics to Vulkan.

Roseblox does the rest for you: it sets up Rosemary, installs and updates Player and Studio, opens **Play** buttons on roblox.com, and keeps your settings and mods in one place. It never changes Roblox's code.

Want the details? See [How Roseblox works](docs/HOW-IT-WORKS.md).

## Can I use it?

| | |
| --- | --- |
| **Arch Linux and Arch-based systems** such as CachyOS, EndeavourOS and Manjaro | Fully supported. Roseblox and Rosemary install as packages. |
| **Ubuntu 24.04 or newer, Debian 13, Fedora 40 or newer, openSUSE Tumbleweed** | Download the Roseblox AppImage (any of these) or the .deb (Ubuntu and Debian) from [Releases](https://github.com/MadnessMyth/RoseBlox/releases/latest). Rosemary comes from its official AppImage. Less tested than Arch, so please report problems. |
| **You need** | A graphics driver with Vulkan (AMD, Intel or NVIDIA). Outside Arch, a 64-bit Intel or AMD (x86-64) computer, because Rosemary's AppImage is made only for those. |
| **Not yet** | Flatpak, older releases such as Ubuntu 22.04 or Debian 12 (their GTK libraries are too old), and ARM computers unless you build Rosemary yourself |

## Install

The full walkthrough, with screenshots, is in **[the install guide](docs/INSTALL.md)**.

### Arch Linux and Arch-based systems

The AUR package has not been published yet, so `paru -S roseblox` does not find it; you build the package from this repository instead. If you already have `git`, `base-devel` and an AUR helper such as `paru` or `yay`, it comes down to:

```sh
git clone https://github.com/MadnessMyth/RoseBlox.git ~/RoseBlox
cd ~/RoseBlox/packaging/arch-git
makepkg -si
```

### Ubuntu, Debian, Fedora and openSUSE

Get Roseblox from the [latest release](https://github.com/MadnessMyth/RoseBlox/releases/latest). Pick one:

- **AppImage** (`Roseblox-x86_64.AppImage`), for any of these systems. It contains everything Roseblox needs. Make it executable (right-click → Properties → "Allow executing as program", or `chmod +x Roseblox-x86_64.AppImage`) and open it. On first start it adds Roseblox to your application menu, and when a new version comes out, the bell in Roseblox offers to update it in place.
- **.deb** (`roseblox_<version>_all.deb`), for Ubuntu and Debian. Install it with `sudo apt install ./roseblox_*_all.deb` in the folder you downloaded it to; apt also installs the GTK libraries it needs.

Prefer to install from source? The [install script](docs/INSTALL.md#install-script) puts Roseblox in `~/.local` for your user only.

### First launch

Open **Roseblox** from your application menu. A short setup walks you through four steps:

1. **Get Rosemary.** On Arch, one click opens a terminal that installs it from the AUR; it builds from source, so this takes a while the first time. Elsewhere, Roseblox opens Rosemary's download page and picks up the AppImage you download.
2. **Open Roblox links with Roseblox** (optional), so **Play** on roblox.com starts the game.
3. **Install Roblox Player, Studio, or both**, with a progress bar.
4. **Update notifications** (optional), so you hear when Roblox releases a new version.

To update Roseblox later, see [Update](docs/INSTALL.md#update). To remove it, see [Uninstall](docs/INSTALL.md#uninstall).

## What you get

- **Join games from the website.** Play buttons and Studio sign-in links open through Roseblox.
- **Automatic Roblox updates.** A desktop notification with **Update** and **Skip This Version** when a new version is out, or turn on auto updates per app to install new Player or Studio versions without asking.
- **Performance settings** for Player and Studio separately: GPU choice, frame pacing, Feral GameMode, the MangoHud overlay, FastFlags checked against Roblox's allowlist, and a tool to measure and compare runs.
- **Mods:** your own fonts, mouse cursor, skyboxes, sounds and textures. Roblox's original files are kept and put back when you turn a mod off.
- **While Playing:** Discord Rich Presence with Join Server, a notification with the location of the server you joined, and a microphone toggle (push to talk or toggle on a desktop hotkey) that silences only Roblox's microphone.
- **Import from another app:** bring FastFlags and settings over from Roséline, Quadra, Sober, Vinegar, Bloxstrap and FastFlag tools, with a preview before anything is saved.
- **More than one Roblox Player** at the same time, side by side.
- **Studio:** open `.rbxl` and `.rbxlx` place files directly.
- **Your look:** dark or light, Roseblox's pink theme or your desktop's colors, and a choice of accent colors.

## Help

Something not working? Start with [Troubleshooting](docs/INSTALL.md#troubleshooting). Logs are in `~/.local/state/roseblox/logs/` (`player.log`, `studio.log`, `roseblox.log`); include them when you report a problem.

## For developers

To run Roseblox from a checkout without installing it, install GTK 4, libadwaita and PyGObject (on Arch, `gtk4`, `libadwaita` and `python-gobject`), then run:

```sh
PYTHONPATH=src python3 -m roseblox
```

Run the tests with `PYTHONPATH=src python3 -m unittest discover -s tests`.

[How Roseblox works](docs/HOW-IT-WORKS.md) covers the launch flow, where files are stored and how each feature works. See also the [release checklist](packaging/RELEASE.md) and [Flatpak and AppImage notes](packaging/DISTRIBUTION.md).

## License

Roseblox is licensed under the MIT License; see [LICENSE](LICENSE). It does not include Rosemary or Roblox: Rosemary comes from the AUR or from Rosemary's own releases, and Roblox is downloaded from Roblox. Roseblox is not affiliated with Roblox Corporation.
