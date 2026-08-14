from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PublicVersionPrivacyTests(unittest.TestCase):
    def test_generated_public_version_contains_only_kiosk_version(self) -> None:
        version_source = (ROOT / "sorter" / "version.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('PUBLIC_VERSION = "Kiosk 2.2"', version_source)
        self.assertIn('DESKTOP_RELEASE_VERSION = "1.27.15"', version_source)
        self.assertIn('INTERNAL_VERSION = "v30"', version_source)
        self.assertIn('RELEASE_CHANNEL = "public"', version_source)

    def test_diagnostic_export_retains_complete_build_identity(self) -> None:
        source = (ROOT / "sorter" / "ui" / "tab_diagnostics.py").read_text(
            encoding="utf-8"
        )

        for field in (
            '"public_version": PUBLIC_VERSION',
            '"desktop_release_version": DESKTOP_RELEASE_VERSION',
            '"internal_version": INTERNAL_VERSION',
            '"release_channel": RELEASE_CHANNEL',
            '"release_label": RELEASE_LABEL',
        ):
            self.assertIn(field, source)


if __name__ == "__main__":
    unittest.main()
