# Configuration and performance work: report

This summarizes the Player and Studio configuration work in Roseblox 0.3.0 to 0.4.1. How to use each setting is in [INSTALL.md](INSTALL.md); release notes are in [packaging/RELEASE.md](../packaging/RELEASE.md).

## What was tested, and what was not

- 349 unit and GTK tests run on Ubuntu (GTK 4.14, libadwaita 1.5) and Arch Linux (GTK 4.24, libadwaita 1.10). They cover loading, saving, invalid and malformed configuration, backups and rollback, Player and Studio separation, environment variables, launch arguments, FastFlag import and export, presets, mods, While Playing, and the pages themselves.
- The `roseblox` and `roseblox-git` packages were built with `makepkg` and installed on Arch Linux. The installed application starts, and the commands it gives Rosemary 0.5.0 were checked against the real `rosemary` executable: arguments after `--`, environment variables, and `rosemary prefix info`.
- **Roblox was not run.** The test machines have no GPU and no Rosemary base system, so no Player or Studio session, frame rate or benchmark result was observed. Measure & compare was checked against ordinary processes only.

## Implemented features

| Spec | Feature | Where |
| --- | --- | --- |
| A | Renderer (Metal through Rosemary's Vulkan translation, shown as fixed), GPU choice, presentation mode, refresh rate | Performance → Graphics & frame pacing |
| B | FastFlag editor with categories, Roblox's Player allowlist, import and export, copying an installation's flags | Performance → FastFlags |
| C | Separate Player and Studio configuration | Performance, Player/Studio switch |
| D | Environment variables for Rosemary and for Roblox (`ROSEMARY_ENVIRON`) | Performance → Environment |
| E | Launch arguments, passed after `--`, without a shell | Performance → Launch arguments |
| F | Roblox's `GlobalBasicSettings_13.xml` read for the frame rate cap; `ClientAppSettings.json` written at launch | Performance, Diagnostics |
| G | Balanced, Performance and Visual Quality presets, with review, backup and rollback | Performance → Preset |
| H | Presentation mode, refresh rate (which sets Roblox's Maximum Frame Rate choices), GameMode, MangoHud | Performance → Graphics & frame pacing |
| I | Rosemary executable choice and status, Command key, titlebar, prefix info, launch logs | Settings → Rosemary, Performance |
| J | Rosemary shader cache and Roblox caches, sizes shown, moved to the trash | Performance → Caches |
| K | Studio's own settings and FastFlags, Open Place…, Don't load my plugins | Performance (Studio), Studio page |
| L | Diagnostics report; Measure & compare (CPU, busiest thread, memory, GPU load where reported) | Performance → Diagnostics, Measure & compare |
| M | Automatic backups before every change, Back Up Now, Restore, Reset to Defaults, Export and Import | Performance → Backups |
| N | Custom variables, arguments and flags, all validated; unknown flags marked | Performance |

Also added at the group's request: While Playing (Discord Rich Presence, server location, close on leave), Mods, and the release workflow.

## Configuration locations

| What | Path |
| --- | --- |
| Launch configuration (both apps) | `~/.config/roseblox/launch-config.json` |
| Its backups (30 newest) | `~/.config/roseblox/backups/` |
| FastFlags at launch | `<bundle>/Contents/MacOS/ClientSettings/ClientAppSettings.json` |
| Mods settings and chosen files | `~/.config/roseblox/mods.json`, `~/.local/share/roseblox/mod-assets/` |
| Mods folders | `~/.local/share/roseblox/mods/player`, `…/studio` |
| Measurements (50 newest) | `~/.local/state/roseblox/measurements.json` |
| Rosemary output | `~/.local/state/roseblox/logs/player.log`, `studio.log` (and `.log.1`) |
| Roblox's own settings | `<prefix>/root/Users/<you>/Library/Roblox/GlobalBasicSettings_13.xml` |

`XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_STATE_HOME` are respected. Every file is written atomically.

## Rosemary options used

Each was checked in Rosemary 0.5.0's source or command line:

- `ROSEMARY_METAL_PRESENTATION_MODE`, `ROSEMARY_SCREEN_REFRESH_RATE`, `ROSEMARY_ENVIRON`, `ROSEMARY_COMMAND_KEY`, `ROSEMARY_DECORATIONS`, `ROSEMARY_PLAIN_CTRL_CLICK`, `ROSEMARY_PREFIX`, `ROSEMARY_SHADER_CACHE`.
- `rosemary BUNDLE -- ARGUMENTS`, `rosemary prefix info`, `rosemary prefix create`.
- Host files appear to Roblox under `/Volumes/Host`, and the prefix's `root` as `/`.

Studio options, checked in Studio's binary: `-task EditFile -localPlaceFile PATH`, and `-disableLoadUserPlugins` (a switch without a value).

## Not available, and why

- **Other renderers.** Roblox for macOS only uses Metal, so there is nothing to choose.
- **Quadra-only variables** (`QUADRA_REFRESH_RATE`, `MACOBLOX_METAL_PRESENT_MODE`). Only standard Rosemary is supported, so they are not offered.
- **A custom Rosemary configuration path.** Rosemary has none beyond `ROSEMARY_PREFIX`, which can be set in the Rosemary environment.
- **Frame rate in Measure & compare.** Roblox does not expose it; the MangoHud overlay shows FPS and frame times.
- **GPU load on Intel graphics.** No reliable per-GPU busy figure is available without extra tools, so it is shown as not reported.
- **FPS unlockers, DLL or library injection, memory changes, and network, physics or security flags.** These are excluded by design.

## Experimental

- Studio FastFlags. Studio has no allowlist, so its flags are marked as experimental.
- Player FastFlags outside Roblox's allowlist are kept, but marked as ignored by the Player.

## Known limitations

- A change applies the next time the application starts. The page says so before you apply it.
- GPU figures in Measure & compare cover the whole GPU, including other applications.
- Mods apply to managed installations; a registered external bundle is left unchanged and the notification center says so.
- While Playing depends on Roblox's log lines. If Roblox changes them, presence and close on leave stop until Roseblox is updated.
