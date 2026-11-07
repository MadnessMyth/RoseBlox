# Distribution format assessment

## Flatpak

Flatpak is a poor fit for the current integration without additional host-side support. Roseblox invokes the host Rosemary executable with `subprocess.Popen`, reads `/proc` to verify that exact launch session, and needs filesystem access to imported bundles at user-selected paths. A sandboxed process generally cannot execute arbitrary host commands or inspect host process identities through its own `/proc` view. Portals can broker selected files, but a portal-provided document path is not a drop-in host path for Rosemary. Broad filesystem and host-command permissions would weaken the sandbox while process verification could still fail.

A viable Flatpak design would need a supported host bridge or a different, explicitly designed Rosemary integration. Bundling Rosemary or weakening process identity checks is not an acceptable workaround.

## AppImage

AppImage could make the Python application files portable, but would not remove the host requirements for GTK4/libadwaita integration or Rosemary. Bundling the GTK/PyGObject stack and its introspection data adds size and compatibility work across Arch/CachyOS system libraries. It also does not solve the need for Rosemary to access external Roblox bundles and for Roseblox to safely verify the host process.

For the Arch/CachyOS audience, a native package using system GTK and Python is simpler to maintain. AppImage could be reconsidered if there is demonstrated demand for distributions outside Arch, after testing host Rosemary and process identity behavior there.

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
