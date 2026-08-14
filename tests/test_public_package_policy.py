from hashlib import sha256
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PublicPackagePolicyTests(unittest.TestCase):
    def test_public_readme_has_no_release_history(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("Kiosk 2.2 Public", readme)
        for excluded in (
            "Desktop v1.27",
            "Evolution from the original baseline",
            "Release notes",
            "Saved Bins USB Import Fix Test",
        ):
            self.assertNotIn(excluded, readme)
        self.assertFalse((ROOT / "INSTALLER_PATH_FIX.txt").exists())

    def test_install_guide_covers_users_builders_and_upgrades(self) -> None:
        guide = (ROOT / "INSTALL.md").read_text(encoding="utf-8")

        for required in (
            "ShibbyPrints-Kiosk-2.2-Public-Setup.exe",
            "ShibbyPrints-Kiosk-2.2-Public-Source.zip",
            "build_installer.bat",
            "build_windows.bat",
            "Compile Installer From Existing Build.bat",
            "Inno Setup 6",
            "Upgrade an existing installation",
            "build_report\\errors.log",
        ):
            self.assertIn(required, guide)
        self.assertNotIn("Release notes", guide)

    def test_public_notice_records_upstream_provenance(self) -> None:
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")

        self.assertIn("Kiosk 2.2 Public", notice)
        self.assertIn("https://github.com/sjseth/AI-Case-Sorter-Py", notice)
        self.assertIn(
            "62e879b0a57f3f857f59a8cc3e6e9ba701dc9bc9", notice
        )

    def test_public_safety_warning_is_in_readme_and_installer_notice(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        readme_text = " ".join(readme.split()).casefold()
        notice_text = " ".join(notice.split()).casefold()

        for required in (
            "Safety and Intended Use",
            "inert, already-fired brass cartridge cases",
            "pinch points",
            "disconnect power before clearing jams",
            "operator's own risk",
        ):
            self.assertIn(required.casefold(), readme_text)
            self.assertIn(required.casefold(), notice_text)

    def test_kiosk_security_policy_uses_private_reporting(self) -> None:
        policy = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        policy_text = " ".join(policy.split())

        for required in (
            "Security → Report a vulnerability",
            "shibbyprints@gmail.com",
            "Do not disclose suspected security vulnerabilities",
            "Diagnostic archives may contain camera images",
            "Only import or download model files from sources you trust",
            "use an HTTPS endpoint",
        ):
            self.assertIn(required, policy_text)
        self.assertNotIn("seth@sjseth.com", policy.casefold())

    def test_gpl_license_matches_upstream_reference(self) -> None:
        license_digest = sha256((ROOT / "LICENSE").read_bytes()).hexdigest()

        self.assertEqual(
            "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986",
            license_digest,
        )

    def test_desktop_release_excludes_pi_appliance_controls(self) -> None:
        kiosk = (ROOT / "sorter" / "ui" / "kiosk.py").read_text(
            encoding="utf-8"
        )
        spec = (ROOT / "ShibbyPrintsCaseSorter.spec").read_text(
            encoding="utf-8"
        )

        for excluded in (
            "show_power_menu",
            "_logo_long_press",
            "sudo",
            "systemctl",
            "poweroff",
        ):
            self.assertNotIn(excluded, kiosk)
        self.assertIn('self.logo_label.bind("<Button-1>", self._tap_logo)', kiosk)
        self.assertFalse((ROOT / "sorter" / "hardware_manager.py").exists())
        self.assertFalse((ROOT / "sorter" / "ui" / "tab_wifi.py").exists())
        self.assertIn('"sorter.hardware_manager"', spec)
        self.assertIn('"sorter.ui.tab_wifi"', spec)


if __name__ == "__main__":
    unittest.main()
