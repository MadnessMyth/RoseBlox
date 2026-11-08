# Distribution format assessment

## Flatpak

Flatpak is a poor fit for the current integration without additional host-side support. Roseblox invokes the host Rosemary executable with `subprocess.Popen`, reads `/proc` to verify that exact launch session, and needs filesystem access to imported bundles at user-selected paths. A sandboxed process generally cannot execute arbitrary host commands or inspect host process identities through its own `/proc` view. Portals can broker selected files, but a portal-provided document path is not a drop-in host path for Rosemary. Broad filesystem and host-command permissions would weaken the sandbox while process verification could still fail.

A viable Flatpak design would need a supported host bridge or a different, explicitly designed Rosemary integration. Bundling Rosemary or weakening process identity checks is not an acceptable workaround.

## AppImage

Since 0.6.0, every release includes `Roseblox-x86_64.AppImage`, built by `packaging/appimage/build_appimage.py` on Ubuntu 24.04 in the release workflow. It bundles Python, PyGObject, GTK 4, libadwaita and `bsdtar`, and leaves glibc, graphics drivers, display client libraries and fontconfig to the host, so it runs on glibc 2.39 or newer. It does not contain Rosemary, which still comes from Rosemary's own releases.

What makes it work with Rosemary and the host (see "How Roseblox itself is installed" in `docs/HOW-IT-WORKS.md`):

- `AppRun` saves every environment variable it changes, and Roseblox restores them after GTK starts, so Rosemary and other programs it starts see the host environment.
- Processes that outlive the window (the activity watcher, the update checker, Roblox links) start the AppImage file, because the mounted image disappears when Roseblox exits.
- Process verification reads the host `/proc` as before; nothing is sandboxed.
- The AppImage writes its own menu entry and icon to `~/.local/share` on start.
- It updates itself from GitHub releases after checking `SHA256SUMS`, and carries zsync update information for AppImageUpdate.

## Other distributions

Each release also includes `roseblox_<version>_all.deb` for Ubuntu 24.04 or newer and Debian 13, built by `packaging/debian/build-deb.sh`. It installs into the system Python and depends on Debian's PyGObject, GTK 4 and libadwaita packages. `packaging/generic/install.sh` supports Ubuntu 24.04 or newer, Debian 13, Fedora 40 or newer and openSUSE Tumbleweed (GTK 4.10 and libadwaita 1.4 or newer). Rosemary comes from its official x86-64 AppImage, which the user downloads from Rosemary's releases page and setup then offers, or from Nix or a source build. An `.rpm` (for example through a Copr repository or the Open Build Service) would be the next step if there is demand; like the others, it would not include Rosemary.

Butter, Rosemary's own manager, ships as a Flatpak and runs its own copy of Rosemary inside the sandbox. A Roseblox Flatpak would have to do the same, which means downloading Rosemary, and would also need portals or permissions for Roblox link registration, the login entry for update checks, Discord's IPC socket, GameMode and MangoHud.

## Publishing to the AUR

`packaging/arch` is the `roseblox` package (tagged releases) and `packaging/arch-git` is `roseblox-git` (latest `master`). Publishing needs an AUR account, so a maintainer does it by hand.

### First upload

1. Create an account at <https://aur.archlinux.org/register> and add your SSH public key under **My Account → SSH Public Key**.
2. Clone the empty package repository. Cloning a name that does not exist yet is how a new AUR package is created:

   ```sh
   git clone ssh://aur@aur.archlinux.org/roseblox.git aur-roseblox
   ```

3. Copy the recipe and its files, then check that it builds:

   ```sh
   cp packaging/arch/{PKGBUILD,.SRCINFO,io.github.roseblox.Roseblox.desktop,io.github.roseblox.Roseblox.svg} aur-roseblox/
   cd aur-roseblox
   makepkg -f && namcap PKGBUILD
   ```

4. Commit and push. The AUR only accepts a push whose `.SRCINFO` matches the `PKGBUILD`:

   ```sh
   git add PKGBUILD .SRCINFO io.github.roseblox.Roseblox.desktop io.github.roseblox.Roseblox.svg
   git commit -m "Initial upload: roseblox 0.4.1"
   git push origin master
   ```

5. For `roseblox-git`, do the same with `ssh://aur@aur.archlinux.org/roseblox-git.git` and the `PKGBUILD` and `.SRCINFO` from `packaging/arch-git`. It needs no other files.

### After each release

`packaging/arch/PKGBUILD` and `.SRCINFO` are updated in this repository when a version is tagged. Copy both into the AUR clone, commit them as `Update to X.Y.Z`, and push. `roseblox-git` only needs an update when its dependencies or install steps change; regenerate its `.SRCINFO` with `makepkg --printsrcinfo > .SRCINFO` whenever its `PKGBUILD` changes.
