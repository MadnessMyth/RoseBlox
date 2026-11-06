# Distribution format assessment

## Flatpak

Flatpak is a poor fit for the current integration without additional host-side support. Roseblox invokes the host Rosemary executable with `subprocess.Popen`, reads `/proc` to verify that exact launch session, and needs filesystem access to imported bundles at user-selected paths. A sandboxed process generally cannot execute arbitrary host commands or inspect host process identities through its own `/proc` view. Portals can broker selected files, but a portal-provided document path is not a drop-in host path for Rosemary. Broad filesystem and host-command permissions would weaken the sandbox while process verification could still fail.

A viable Flatpak design would need a supported host bridge or a different, explicitly designed Rosemary integration. Bundling Rosemary or weakening process identity checks is not an acceptable workaround.

## AppImage

AppImage could make the Python application files portable, but would not remove the host requirements for GTK4/libadwaita integration or Rosemary. Bundling the GTK/PyGObject stack and its introspection data adds size and compatibility work across Arch/CachyOS system libraries. It also does not solve the need for Rosemary to access external Roblox bundles and for Roseblox to safely verify the host process.

For the Arch/CachyOS audience, a native package using system GTK and Python is simpler to maintain. AppImage could be reconsidered if there is demonstrated demand for distributions outside Arch, after testing host Rosemary and process identity behavior there.
