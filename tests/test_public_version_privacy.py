from pathlib import Path
import unittest

from generate_release_files import load_release_info


ROOT = Path(__file__).resolve().parents[1]


class PublicVersionPrivacyTests(unittest.TestCase):
    def test_generated_version_contains_only_public_identity(self) -> None:
        version_source = (ROOT / "sorter" / "version.py").read_text(
            encoding="utf-8"
        )
        release = load_release_info()

        self.assertEqual(
            version_source,
            '"""Generated public release metadata. Edit RELEASE.env and run '
            'generate_release_files.py."""\n'
            'PUBLIC_VERSION = "Kiosk 2.3"\n'
            'PUBLIC_VERSION_NUMBER = "2.3"\n'
            f'APP_VERSION = "{release["APP_VERSION"]}"\n'
            'RELEASE_DATE = "2026-10-02"\n',
        )

    def test_diagnostic_export_uses_public_application_identity(self) -> None:
        source = (ROOT / "sorter" / "ui" / "app.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('"public_version": PUBLIC_VERSION', source)
        self.assertIn('"app_version": APP_VERSION', source)


if __name__ == "__main__":
    unittest.main()
