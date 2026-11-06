# Roseblox v0.1.0 release preparation

## Release checklist

- [x] Add the project owner's chosen MIT License (`MadnessMyth`, 2026) and package it under `/usr/share/licenses/roseblox/`.
- [ ] Review the release notes and README installation instructions.
- [ ] Run the full unittest suite, Python compilation, and `git diff --check`.
- [ ] Build `packaging/arch/PKGBUILD` with `makepkg`; inspect its contents with `bsdtar -tf` and, where available, `namcap`.
- [ ] Manually launch the installed package from a desktop menu and verify Player and Studio through an existing Rosemary installation.
- [ ] Create the `v0.1.0` source tag referenced by the PKGBUILD and regenerate `.SRCINFO` after any recipe changes.
- [ ] Create the `v0.1.0` Git tag and GitHub release manually after review.
- [ ] Publish the AUR recipe manually after the tagged source is available and release metadata is complete.

No Git tag, GitHub release, AUR submission, or system package installation is performed by this project preparation.

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
