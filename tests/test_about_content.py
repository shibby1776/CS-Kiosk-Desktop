from pathlib import Path
import unittest


class AboutContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).resolve().parents[1] / "sorter" / "ui" / "app.py"
        ).read_text(encoding="utf-8")

    def test_window_and_about_names_match_kiosk_product(self) -> None:
        self.assertIn('self.root.title("ShibbyPrints Kiosk Sorter — Desktop")', self.source)
        self.assertIn('window.title("About ShibbyPrints Kiosk Sorter")', self.source)
        self.assertIn('text="ShibbyPrints Kiosk Sorter Software"', self.source)
        self.assertIn('text=PUBLIC_VERSION', self.source)
        self.assertNotIn(
            'text=f"Kiosk {PUBLIC_VERSION_NUMBER} Desktop"', self.source
        )

    def test_about_attribution_and_license_wording(self) -> None:
        for wording in (
            "based on software",
            "originally created by SJSeth Solutions",
            "GPL-3.0-or-later",
            "This software is provided without warranty.",
            "corresponding source code is provided with each official",
            "ShibbyPrints independently maintains this Kiosk Edition",
            "not an official SJSeth Solutions product",
            "sponsorship or endorsement by SJSeth Solutions is claimed or",
        ):
            self.assertIn(wording, self.source)
        self.assertNotIn(
            "This application is based on open-source software", self.source
        )


if __name__ == "__main__":
    unittest.main()
