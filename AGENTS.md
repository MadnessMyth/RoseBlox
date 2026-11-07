# Development notes

- Keep the GTK interface separate from backend application logic.
- Use GTK 4, libadwaita, and system PyGObject; do not introduce Electron or a bundled runtime.
- Use the established Roblox endpoints and URL patterns in the project specification.
- Never bundle Rosemary or download it directly. The only installation path is the `rosemary` AUR package (plus its `sdl3` and `vulkan-icd-loader` runtime packages) through the user's AUR helper, run in a visible terminal after an explicit click, followed by Rosemary's own `rosemary prefix create`.
- Preserve user-owned archives and external application bundles during import and library operations.
- Rosemary must keep running when Roseblox exits: launch it in its own session with output to a log file, never through pipes Roseblox reads.
- The update checker (`roseblox --update-service`) runs for the whole desktop session; keep it free of GTK imports.
