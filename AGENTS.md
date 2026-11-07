# Development notes

- Keep the GTK interface separate from backend application logic.
- Use GTK 4, libadwaita, and system PyGObject; do not introduce Electron or a bundled runtime.
- Use the established Roblox endpoints and URL patterns in the project specification.
- Never bundle Rosemary or download it directly. The only installation path is the `rosemary` AUR package (plus its `sdl3` and `vulkan-icd-loader` runtime packages) through the user's AUR helper, run in a visible terminal after an explicit click, followed by Rosemary's own `rosemary prefix create`.
- Preserve user-owned archives and external application bundles during import and library operations.
- Rosemary must keep running when Roseblox exits: launch it in its own session with output to a log file, never through pipes Roseblox reads.
- The update checker (`roseblox --update-service`) runs for the whole desktop session; keep it free of GTK imports.
- Colors come from `roseblox/theme.py`, which prepends `@define-color` (and, on libadwaita 1.6+, CSS variable) definitions to `resources/style.css`. Use the `@rb_*` and libadwaita named colors in the stylesheet instead of hard-coded colors, so every Appearance choice keeps working.
- Performance settings (`backend/launch_config.py`) must stay safe: refuse credential-like and library injection variables and login arguments, never add FastFlags the user did not choose, keep networking, physics and security flags out of presets, and write files atomically with a backup of the previous state. Guest arguments always follow `--`, because Rosemary parses its own options anywhere after the bundle path.
- Only state facts in Diagnostics and the Performance page that Roseblox read or verified; do not estimate metrics.
- Use icon names that current Adwaita still ships (for example `object-select-symbolic`, not `emblem-ok-symbolic`).
