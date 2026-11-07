# Installing and updating Roseblox

This guide is for Arch Linux and Arch-based distributions such as CachyOS and EndeavourOS. It installs the latest Roseblox from the `master` branch as the `roseblox-git` package. Updates then take a single command.

## Install

### 1. Install the build tools

```sh
sudo pacman -S --needed git base-devel
```

### 2. Install an AUR helper (skip if you already have `paru` or `yay`)

Roseblox installs Rosemary through an AUR helper. To install `paru`:

```sh
git clone https://aur.archlinux.org/paru-bin.git /tmp/paru-bin
cd /tmp/paru-bin
makepkg -si
```

### 3. Download Roseblox

```sh
git clone https://github.com/MadnessMyth/RoseBlox.git ~/RoseBlox
```

Keep this folder; you will use it to update.

### 4. Build and install

```sh
cd ~/RoseBlox/packaging/arch-git
makepkg -si
```

Run this as your normal user, not with `sudo`. `makepkg` asks for your password when it installs the package. If you previously installed the older `roseblox` package from `packaging/arch`, pacman asks whether to replace it. Answer `y`.

Check that it worked:

```sh
pacman -Q roseblox-git
```

### 5. Run first-time setup

Open **Roseblox** from your application menu, or run `roseblox` in a terminal. The setup page has three steps:

![First-time setup on a fresh system](screenshots/01-setup-fresh.png)

1. **Rosemary** – click **Install Rosemary**. A terminal opens and runs `paru -S --needed rosemary` (or `yay`). Review the package, confirm, and enter your password. Rosemary compiles from source, so this takes a few minutes. Press Enter to close the terminal when it finishes. Roseblox detects Rosemary on its own and marks the step **Ready**. If you already have a Rosemary build or AppImage, use **Choose…** instead.
2. **Roblox Links** (optional) – turn on **Open Roblox links with Roseblox** so that **Play** buttons on roblox.com and Studio sign-in links open through Roseblox.
3. **Player and Studio** – click **Install** for Roblox Player, Roblox Studio, or both. The download runs on the page with a progress bar.

![Setup complete](screenshots/04-setup-links-enabled.png)

Click **Finish Setup**. You can reopen setup at any time from **Settings → First-time setup → Open Setup**.

## Update

### Roseblox

```sh
cd ~/RoseBlox
git pull
cd packaging/arch-git
makepkg -si
```

`git pull` updates the packaging recipe, and `makepkg` fetches and builds the latest `master`. Your settings, installations and link choice are kept. `pacman -Q roseblox-git` shows the new version, for example `0.1.0.r14.g1a2b3c4-1`, where the last part is the commit it was built from.

### Rosemary

Rosemary updates with your other AUR packages:

```sh
paru -Syu    # or: yay -Syu
```

Click **Check Again** under Rosemary in Settings if Roseblox is already open.

### Roblox Player and Studio

In Roseblox, open the **Player** or **Studio** page. Click **Refresh Version** to see the latest Roblox version, then click **Download and Install Latest**. To make the new version the default on Home, click **Set as preferred** on its row. Older versions stay installed until you click **Remove**.

## Uninstall

1. In **Settings → Roblox Links**, turn off **Open Roblox links with Roseblox**. This restores the previous link handler.
2. Remove the package:

   ```sh
   sudo pacman -R roseblox-git
   ```

3. Optional: delete downloaded Roblox apps and settings:

   ```sh
   rm -rf ~/.local/share/roseblox ~/.config/roseblox
   ```

4. Optional: remove Rosemary with `sudo pacman -R rosemary`.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| "No AUR helper found" | Install `paru` (step 2) or `yay`, then click **Try Again**. |
| "No supported terminal found" | Install a terminal such as `kitty`, `konsole` or `gnome-console`, or set the `TERMINAL` environment variable. |
| Rosemary installed, but Roseblox says it is missing | Click **Check Again**. If Rosemary is outside `PATH`, use **Settings → Executable → Choose…**. |
| Roblox links open the wrong app | In **Settings → Roblox Links**, click **Repair**, or turn the switch off and on again. |
| ZIP import fails with "bsdtar" | `sudo pacman -S --needed libarchive` |
