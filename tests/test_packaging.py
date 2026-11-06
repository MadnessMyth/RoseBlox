import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ArchPackagingTests(unittest.TestCase):
    def test_recipe_uses_a_version_tag_and_real_runtime_dependencies(self):
        recipe = (ROOT / "packaging" / "arch" / "PKGBUILD").read_text(encoding="utf-8")
        self.assertIn("git+https://github.com/MadnessMyth/RoseBlox.git#tag=v${pkgver}", recipe)
        self.assertIn("depends=('gtk4' 'libadwaita' 'python' 'python-gobject')", recipe)
        self.assertIn("libarchive", recipe)
        self.assertIn("python -m build --wheel --no-isolation", recipe)
        self.assertIn("python -m installer --destdir=", recipe)
        self.assertNotRegex(recipe, r"(?m)^(depends|optdepends)=.*rosemary")
        self.assertNotIn("pip install", recipe)

    def test_desktop_entry_references_the_packaged_hicolor_icon(self):
        desktop = (ROOT / "packaging" / "arch" / "io.github.roseblox.Roseblox.desktop").read_text(
            encoding="utf-8"
        )
        self.assertIn("Type=Application", desktop)
        self.assertIn("Exec=roseblox", desktop)
        self.assertIn("TryExec=roseblox", desktop)
        self.assertIn("Icon=io.github.roseblox.Roseblox", desktop)
        icon = ROOT / "packaging" / "arch" / "io.github.roseblox.Roseblox.svg"
        self.assertTrue(icon.is_file())
        self.assertIn("/usr/share/icons/hicolor/scalable/apps/", (ROOT / "packaging" / "arch" / "PKGBUILD").read_text(encoding="utf-8"))

    def test_release_docs_are_explicit_about_unpublished_and_license_state(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        checklist = (ROOT / "packaging" / "RELEASE.md").read_text(encoding="utf-8")
        self.assertIn("AUR package has not been published yet", readme)
        self.assertIn("MIT License", readme)
        self.assertIn("license=('MIT')", (ROOT / "packaging" / "arch" / "PKGBUILD").read_text(encoding="utf-8"))
        self.assertIn("Copyright (c) 2026 MadnessMyth", (ROOT / "LICENSE").read_text(encoding="utf-8"))
        self.assertIn("/usr/share/licenses/$pkgname/LICENSE", (ROOT / "packaging" / "arch" / "PKGBUILD").read_text(encoding="utf-8"))
        self.assertRegex(checklist, r"(?m)^## Release notes — v0\.1\.0$")


if __name__ == "__main__":
    unittest.main()
